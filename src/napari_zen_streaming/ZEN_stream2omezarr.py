#################################################################
# File        : ZEN_stream2omezarr.py
# Author      : SRh
# Institution : Carl Zeiss Microscopy GmbH
#
# Stream pixel data from ZEN via the ZEN-API gateway directly
# into an OME-ZARR file using the ome-writers library.
#
# Two modes of operation:
#   1) CLI mode (--experiment): Dimensions are discovered from the
#      pixel stream. All frames are buffered in memory, then written.
#   2) Config mode (--experiment-config): Dimensions and well positions
#      are exported from ZEN. Frames are written on-the-fly.
#
# In both modes the experiment can be started from the script
# (--start-experiment) or from the ZEN UI (--no-start-experiment).
# In config mode, --start-experiment / --no-start-experiment
# overrides the INI 'start_from_script' setting.
#
# Use --viewer ndv or --viewer napari to launch a viewer after acquisition.
#
# Usage:
#   python ZEN_stream2omezarr.py --help
#
# Examples:
#   # Config mode, start experiment from script (INI default):
#   python ZEN_stream2omezarr.py --experiment-config experiment_streaming_config.ini
#
#   # Config mode, wait for user to start from ZEN UI:
#   python ZEN_stream2omezarr.py --experiment-config experiment_streaming_config.ini --no-start-experiment
#
#   # CLI mode, start from UI:
#   python ZEN_stream2omezarr.py --experiment MyExp --output-dir ./output
#
# Copyright(c) 2026 Carl Zeiss AG, Germany. All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

import argparse
import asyncio
import contextlib
import itertools
import logging
import sys
from collections.abc import AsyncIterable, AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import dotenv
import numpy as np
from grpclib.const import Cardinality
from grpclib.exceptions import ProtocolError, StreamTerminatedError

# ome-writers
import zarr

# napari.utils.progress subclasses tqdm: shows in the napari activity dock
# when a viewer is open; falls back to a tqdm terminal bar in CLI mode.
# Used by stream_to_omezarr() (buffered CLI mode).
from napari.utils import progress as napari_progress
from ome_writers import (
    AcquisitionSettings,
    Dimension,
    Plate,
    Position,
    create_stream,
)

# ZEN API auto-generated stubs
from zen_api.acquisition.v1beta import (
    ExperimentServiceGetStatusRequest,
    ExperimentServiceStub,
    ExperimentStreamingServiceMonitorAllExperimentsRequest,
    ExperimentStreamingServiceMonitorAllExperimentsResponse,
    ExperimentStreamingServiceStub,
    PixelType,
)

from napari_zen_streaming._logging import configure_logging
from napari_zen_streaming._scene_geometry import (
    SceneGeometry,
    TileGeometry,
    calculate_scene_geometry,
)
from napari_zen_streaming.misc import initialize_zenapi
from napari_zen_streaming.ZEN_omezarr import (
    ExperimentConfig,
    load_experiment_acquisition,
    load_experiment_config,
    open_in_napari_viewer,
    open_in_ndv_viewer,
    start_experiment,
)

logger = logging.getLogger(__name__)

# Default data type for pixel streaming
DEFAULT_DTYPE = np.dtype(np.uint16)

_GRAYSCALE_PIXEL_DTYPES: dict[PixelType, np.dtype] = {
    PixelType.GRAY8: np.dtype(np.uint8),
    PixelType.GRAY16: np.dtype(np.uint16),
}

# Inactivity timeout (seconds) for pixel-stream completion.
_STATUS_POLL_TIMEOUT: float = 30.0


def _runtime_grayscale_dtype(
    pixel_type: PixelType,
    payload_size: int,
    width: int,
    height: int,
) -> np.dtype:
    """Return and validate the dtype reported by a streamed frame.

    Args:
        pixel_type: ZEN runtime pixel type.
        payload_size: Number of bytes in the frame payload.
        width: Frame width in pixels.
        height: Frame height in pixels.

    Returns:
        NumPy dtype for a supported grayscale frame.

    Raises:
        ValueError: If the pixel type is unsupported or payload size differs
            from the dimensions and reported pixel type.
    """
    try:
        dtype = _GRAYSCALE_PIXEL_DTYPES[pixel_type]
    except KeyError as exc:
        name = getattr(pixel_type, "name", str(pixel_type))
        raise ValueError(f"Unsupported streamed pixel type for OME-ZARR: {name}") from exc

    expected_size = width * height * dtype.itemsize
    if payload_size != expected_size:
        raise ValueError(
            "Streamed pixel payload size does not match its runtime "
            f"metadata: got {payload_size} bytes, expected "
            f"{expected_size} for {width}x{height} {pixel_type.name}."
        )
    return dtype


def _decode_grayscale_frame(
    raw_data: bytes,
    dtype: np.dtype,
    width: int,
    height: int,
) -> np.ndarray:
    """Decode a ZEN grayscale payload in native YX array order.

    ZEN reports ``width`` as X and ``height`` as Y. NumPy image arrays use
    ``(Y, X)``, so the flat payload must be reshaped to ``(height, width)``
    without an additional rotation or transpose.

    Args:
        raw_data: Flat grayscale pixel payload.
        dtype: Runtime NumPy pixel dtype.
        width: Frame width along X.
        height: Frame height along Y.

    Returns:
        C-contiguous image array with shape ``(height, width)``.
    """
    frame = np.frombuffer(raw_data, dtype=dtype).reshape((height, width))
    return np.ascontiguousarray(frame)


def _plate_row_name(row: int) -> str:
    """Convert a one-based plate row number to its alphabetic name."""
    name = ""
    while row > 0:
        row, remainder = divmod(row - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def _build_position_layout(
    ecfg: ExperimentConfig,
    num_s: int,
    num_m: int,
) -> tuple[list[Position], Plate | None]:
    """Build generic positions or an HCS plate layout from experiment metadata."""
    if not ecfg.use_hcs_layout:
        return (
            [
                Position(
                    name=f"S{scene}_M{tile}",
                    grid_row=scene,
                    grid_column=tile,
                )
                for scene in range(num_s)
                for tile in range(num_m)
            ],
            None,
        )

    positions_by_scene = {
        position.get("scene_index"): position
        for position in ecfg.positions
        if isinstance(position.get("scene_index"), int)
    }
    if set(positions_by_scene) != set(range(num_s)):
        if ecfg.positions:
            logger.warning(
                "ZEN scene-to-well metadata does not match the live scene "
                "count; using a generic multiposition OME-ZARR layout."
            )
        return (
            [
                Position(name=f"S{scene}_M{tile}", grid_row=scene, grid_column=tile)
                for scene in range(num_s)
                for tile in range(num_m)
            ],
            None,
        )

    hcs_positions: list[Position] = []
    max_row = 0
    max_column = 0
    for scene in range(num_s):
        metadata = positions_by_scene[scene]
        row = metadata.get("well_row")
        column = metadata.get("well_column")
        field_index = metadata.get("field_index")
        if not isinstance(row, int) or row < 1 or not isinstance(column, int) or column < 1:
            logger.warning("ZEN scene-to-well metadata is invalid; using a generic " "multiposition OME-ZARR layout.")
            return (
                [Position(name=f"S{s}_M{m}", grid_row=s, grid_column=m) for s in range(num_s) for m in range(num_m)],
                None,
            )

        max_row = max(max_row, row)
        max_column = max(max_column, column)
        field = field_index - 1 if isinstance(field_index, int) and field_index > 0 else scene
        hcs_positions.append(
            Position(
                name=str(field),
                plate_row=_plate_row_name(row),
                plate_column=f"{column:02d}",
            )
        )

    return (
        hcs_positions,
        Plate(
            name=ecfg.experiment_name,
            row_names=[_plate_row_name(row) for row in range(1, max_row + 1)],
            column_names=[f"{column:02d}" for column in range(1, max_column + 1)],
        ),
    )


def _materialize_hcs_row_groups(zarr_path: Path) -> None:
    """Create explicit Zarr v3 groups for rows referenced by plate wells."""
    root = zarr.open_group(zarr_path, mode="a")
    ome_metadata = root.attrs.get("ome", {})
    plate_metadata = ome_metadata.get("plate", {}) if isinstance(ome_metadata, dict) else {}
    wells = plate_metadata.get("wells", []) if isinstance(plate_metadata, dict) else []
    row_names = {
        well["path"].split("/", maxsplit=1)[0]
        for well in wells
        if isinstance(well, dict) and isinstance(well.get("path"), str) and "/" in well["path"]
    }
    for row_name in row_names:
        root.require_group(row_name)


def _get_multiscale_metadata(
    image_group: Any,
) -> tuple[list[dict[str, Any]], list[float]]:
    """Read axis and scale metadata from an ome-writers image group."""
    ome_metadata = image_group.attrs.get("ome", {})
    multiscales = ome_metadata.get("multiscales", [])
    if not multiscales:
        raise ValueError("Raw tile image has no multiscales metadata")

    multiscale = multiscales[0]
    axes = multiscale.get("axes", [])
    datasets = multiscale.get("datasets", [])
    if not axes or not datasets:
        raise ValueError("Raw tile multiscales metadata is incomplete")

    transformations = datasets[0].get("coordinateTransformations", [])
    scale = next(
        (transformation.get("scale") for transformation in transformations if transformation.get("type") == "scale"),
        None,
    )
    if not isinstance(scale, list) or len(scale) != len(axes):
        raise ValueError("Raw tile image has no valid axis scale")
    return [dict(axis) for axis in axes], [float(value) for value in scale]


def _assemble_scene_plane(
    tile_frames: dict[int, np.ndarray],
    geometry: Any,
    output_height: int,
    output_width: int,
) -> np.ndarray:
    """Merge one scene plane's M tiles into a fixed-size YX canvas.

    Args:
        tile_frames: Images keyed by streamed M index.
        geometry: Coordinate-resolved scene geometry.
        output_height: Shared output canvas height across all scenes.
        output_width: Shared output canvas width across all scenes.

    Returns:
        Merged image. Tiles are applied in ascending M order, so higher M
        indices overwrite lower M indices in overlap regions.

    Raises:
        ValueError: If no tile images are supplied or a frame shape differs
            from its captured geometry.
    """
    if not tile_frames:
        raise ValueError("Cannot assemble a scene plane without tile frames")
    first_frame = next(iter(tile_frames.values()))
    mosaic = np.zeros(
        (output_height, output_width),
        dtype=first_frame.dtype,
    )
    for placed_tile in sorted(
        geometry.tiles,
        key=lambda placed: placed.tile.tile_index,
    ):
        image = tile_frames.get(placed_tile.tile.tile_index)
        if image is None:
            continue
        expected_shape = (
            placed_tile.tile.height,
            placed_tile.tile.width,
        )
        if image.shape != expected_shape:
            raise ValueError(
                f"Tile {placed_tile.tile.tile_index} shape " f"{image.shape} differs from {expected_shape}"
            )
        y_slice = slice(
            placed_tile.y_offset,
            placed_tile.y_offset + image.shape[0],
        )
        x_slice = slice(
            placed_tile.x_offset,
            placed_tile.x_offset + image.shape[1],
        )
        mosaic[y_slice, x_slice] = image
    return mosaic


def _assemble_scene_mosaics(
    zarr_path: Path,
    tile_geometries: list[TileGeometry],
    keep_source_tiles: bool = False,
    hcs_positions: list[dict[str, object]] | None = None,
) -> None:
    """Create one viewer-compatible TCZYX mosaic for each scene.

    Generic mosaics are advertised through ``OME/series``. HCS mosaics are
    created inside their existing well and replace the raw tile entries in
    that well's advertised ``images`` list. Raw HCS tile groups are retained
    for provenance. Tiles are placed from streamed stage coordinates in
    ascending M-index order, so the highest overlapping M index wins.

    Args:
        zarr_path: Completed ome-writers Zarr store.
        tile_geometries: First-frame geometry for each observed ``(S, M)``.
        keep_source_tiles: Retain raw tile groups after mosaic assembly.
            HCS source tiles are always retained.
        hcs_positions: Optional XML-derived scene-to-well metadata. When
            provided, assemble and advertise mosaics inside HCS wells.
    """
    if not tile_geometries:
        logger.warning("No tile positions were captured; scene assembly skipped.")
        return

    root = zarr.open_group(zarr_path, mode="a")
    hcs_mode = hcs_positions is not None
    if hcs_mode:
        ome_group = None
        ome_metadata: dict[str, Any] = {}
        raw_series: list[str] = []
        metadata_by_scene = {
            position.get("scene_index"): position
            for position in hcs_positions
            if isinstance(position.get("scene_index"), int)
        }
    else:
        ome_group = root["OME"]
        ome_metadata = dict(ome_group.attrs.get("ome", {}))
        raw_series = ome_metadata.get("series", [])
        if not isinstance(raw_series, list):
            raise ValueError("OME series metadata is not a path list")
        metadata_by_scene = {}

    scenes: dict[int, list[TileGeometry]] = {}
    for tile in tile_geometries:
        scenes.setdefault(tile.scene_index, []).append(tile)
    num_m = max(tile.tile_index for tile in tile_geometries) + 1

    scene_paths: list[str] = []
    assembled_source_paths: set[str] = set()
    scene_moves: list[tuple[str, str]] = []
    for scene_index in sorted(scenes):
        scene_tiles = sorted(
            scenes[scene_index],
            key=lambda tile: tile.tile_index,
        )
        single_tile = len(scene_tiles) == 1
        geometry = calculate_scene_geometry(scene_tiles)
        source_paths: list[str] = []
        source_arrays: list[Any] = []
        if hcs_mode:
            position = metadata_by_scene.get(scene_index)
            if position is None:
                raise ValueError(f"HCS metadata missing for scene {scene_index}")
            row = position.get("well_row")
            column = position.get("well_column")
            field_index = position.get("field_index")
            if not isinstance(row, int) or not isinstance(column, int):
                raise ValueError("HCS scene has no valid well coordinate")
            field = field_index - 1 if isinstance(field_index, int) and field_index > 0 else scene_index
            well_path = f"{_plate_row_name(row)}/{column:02d}"
            source_names = [str(field * num_m + placed.tile.tile_index) for placed in geometry.tiles]
            source_paths = [f"{well_path}/{source_name}" for source_name in source_names]
            scene_name = f"scene_{scene_index}"
            scene_path = f"{well_path}/{scene_name}"
        else:
            for placed_tile in geometry.tiles:
                position_index = placed_tile.tile.position_index
                if position_index >= len(raw_series):
                    raise ValueError("Tile position index exceeds the OME series metadata")
                source_paths.append(raw_series[position_index])
            source_names = []
            well_path = ""
            scene_name = f"scene_{scene_index}"
            scene_path = scene_name
        source_arrays = [root[path]["0"] for path in source_paths]

        first_array = source_arrays[0]
        source_shape = tuple(first_array.shape)
        source_dtype = np.dtype(first_array.dtype)
        for source_array in source_arrays[1:]:
            if tuple(source_array.shape) != source_shape:
                raise ValueError("All tiles in a scene must have one shape")
            if np.dtype(source_array.dtype) != source_dtype:
                raise ValueError("All tiles in a scene must have one dtype")

        source_group = root[source_paths[0]]
        axes, scale = _get_multiscale_metadata(source_group)
        axis_names = tuple(axis["name"] for axis in axes)
        if axis_names[-2:] != ("y", "x"):
            raise ValueError("Scene tile arrays must end in Y and X axes")

        reuse_source_group = single_tile and not keep_source_tiles
        mosaic_shape_values = list(source_shape)
        mosaic_shape_values[-2:] = [geometry.height, geometry.width]
        z_offsets = [0] * len(geometry.tiles)
        if "z" in axis_names:
            z_axis = axis_names.index("z")
            z_scale = scale[z_axis]
            if z_scale <= 0:
                raise ValueError("Scene Z scale must be positive")
            z_origin = min(placed_tile.tile.position_z for placed_tile in geometry.tiles)
            z_offsets = [
                int(round((placed_tile.tile.position_z - z_origin) / z_scale)) for placed_tile in geometry.tiles
            ]
            mosaic_shape_values[z_axis] = max(z_offset + source_shape[z_axis] for z_offset in z_offsets)
        else:
            z_axis = None
            z_origin = min(placed_tile.tile.position_z for placed_tile in geometry.tiles)
        mosaic_shape = tuple(mosaic_shape_values)
        if reuse_source_group:
            scene_group = source_group
            if source_paths[0] != scene_path:
                scene_moves.append((source_paths[0], scene_path))
        else:
            if scene_path in root:
                del root[scene_path]
            scene_group = root.create_group(scene_path)
            chunks = tuple(1 for _ in source_shape[:-2]) + (
                min(512, geometry.height),
                min(512, geometry.width),
            )
            mosaic_array = scene_group.create_array(
                "0",
                shape=mosaic_shape,
                dtype=source_dtype,
                chunks=chunks,
                fill_value=0,
                dimension_names=axis_names,
            )

            for plane_index in np.ndindex(mosaic_shape[:-2]):
                mosaic_plane = np.zeros(
                    (geometry.height, geometry.width),
                    dtype=source_dtype,
                )
                for placed_tile, source_array, z_offset in zip(
                    geometry.tiles,
                    source_arrays,
                    z_offsets,
                    strict=True,
                ):
                    source_plane_index = list(plane_index)
                    if z_axis is not None:
                        source_z = plane_index[z_axis] - z_offset
                        if source_z < 0 or source_z >= source_shape[z_axis]:
                            continue
                        source_plane_index[z_axis] = source_z
                    y_slice = slice(
                        placed_tile.y_offset,
                        placed_tile.y_offset + placed_tile.tile.height,
                    )
                    x_slice = slice(
                        placed_tile.x_offset,
                        placed_tile.x_offset + placed_tile.tile.width,
                    )
                    tile_plane = source_array[tuple(source_plane_index) + (slice(None), slice(None))]
                    mosaic_plane[y_slice, x_slice] = tile_plane
                mosaic_array[plane_index + (slice(None), slice(None))] = mosaic_plane

        translation = [0.0] * len(axes)
        if z_axis is not None:
            translation[z_axis] = z_origin
        translation[axis_names.index("y")] = geometry.origin_y
        translation[axis_names.index("x")] = geometry.origin_x
        scene_group.attrs["ome"] = {
            "version": "0.5",
            "multiscales": [
                {
                    "name": f"Scene {scene_index}",
                    "axes": axes,
                    "datasets": [
                        {
                            "path": "0",
                            "coordinateTransformations": [
                                {"type": "scale", "scale": scale},
                                {
                                    "type": "translation",
                                    "translation": translation,
                                },
                            ],
                        }
                    ],
                }
            ],
        }
        scene_group.attrs["zen_scene"] = {
            "scene_index": scene_index,
            "source_tiles_retained": keep_source_tiles,
            "source_tiles": [
                {
                    "path": source_path,
                    "tile_index": placed_tile.tile.tile_index,
                    "stage_position_xyz": [
                        placed_tile.tile.center_x,
                        placed_tile.tile.center_y,
                        placed_tile.tile.position_z,
                    ],
                    "translation_yx": [
                        placed_tile.translation_y,
                        placed_tile.translation_x,
                    ],
                }
                for source_path, placed_tile in zip(
                    source_paths,
                    geometry.tiles,
                    strict=True,
                )
            ],
            "overlap_policy": "higher_m_index",
        }
        if not reuse_source_group:
            assembled_source_paths.update(source_paths)
        if hcs_mode:
            well_group = root[well_path]
            well_ome = dict(well_group.attrs.get("ome", {}))
            well_metadata = dict(well_ome.get("well", {}))
            images = well_metadata.get("images", [])
            source_name_set = set(source_names)
            well_metadata["images"] = [image for image in images if image.get("path") not in source_name_set] + [
                {"path": scene_name}
            ]
            well_ome["well"] = well_metadata
            well_group.attrs["ome"] = well_ome
        else:
            scene_paths.append(scene_path)
        if reuse_source_group:
            logger.info(
                f"Finalized scene {scene_index} from 1 spatial tile " f"without pixel copy: shape={mosaic_shape}"
            )
        elif single_tile:
            logger.info(
                f"Copied scene {scene_index} from 1 spatial tile to preserve " f"the source group: shape={mosaic_shape}"
            )
        else:
            logger.info(
                f"Assembled scene {scene_index} from {len(scene_tiles)} " f"spatial tiles: shape={mosaic_shape}"
            )

    if not hcs_mode:
        ome_metadata["series"] = scene_paths
        assert ome_group is not None
        ome_group.attrs["ome"] = ome_metadata
    if not hcs_mode and not keep_source_tiles:
        for source_path in assembled_source_paths:
            if source_path in root:
                del root[source_path]
        if assembled_source_paths:
            logger.info(f"Removed {len(assembled_source_paths)} source tile groups " "after scene assembly.")
    for source_path, final_path in scene_moves:
        if final_path in root:
            del root[final_path]
        (zarr_path / source_path).replace(zarr_path / final_path)


def _find_omezarr_image_paths(zarr_path: Path) -> list[Path]:
    """Return every image group carrying OME multiscales metadata."""
    root = zarr.open_group(str(zarr_path), mode="r")
    image_paths: list[Path] = []

    def visit(group: zarr.Group, group_path: Path) -> None:
        ome_metadata = group.attrs.get("ome", {})
        if isinstance(ome_metadata, dict) and isinstance(ome_metadata.get("multiscales"), list):
            image_paths.append(group_path)
            return
        for name, subgroup in group.groups():
            visit(subgroup, group_path / name)

    visit(root, zarr_path)
    return image_paths


def generate_omezarr_pyramids(
    zarr_path: str | Path,
    pyramid_levels: int,
    spatial_shard_size_chunks: int | None = 2,
) -> None:
    """Append Y/X pyramid levels after acquisition without rewriting level 0."""
    if pyramid_levels < 1:
        raise ValueError("pyramid_levels must be at least 1")

    import ngff_zarr as nz

    zarr_path = Path(zarr_path)
    image_paths = _find_omezarr_image_paths(zarr_path)
    if not image_paths:
        raise RuntimeError(f"No OME-Zarr image groups found in: {zarr_path}")

    chunks_per_shard = (
        {
            "y": spatial_shard_size_chunks,
            "x": spatial_shard_size_chunks,
        }
        if spatial_shard_size_chunks is not None
        else None
    )

    logger.info(f"Generating {pyramid_levels} pyramid level(s) for " f"{len(image_paths)} image(s) ...")
    for image_index, image_path in enumerate(image_paths, start=1):
        existing = nz.from_ngff_zarr(image_path, validate=True)
        base_image = existing.images[0]
        if not {"y", "x"}.issubset(base_image.dims):
            raise RuntimeError(f"OME-Zarr image has no Y/X axes: {image_path}")

        spatial_dims = [dim for dim in base_image.dims if dim in {"z", "y", "x"}]
        scale_factors = [
            {dim: 2**level if dim in {"y", "x"} else 1 for dim in spatial_dims}
            for level in range(1, pyramid_levels + 1)
        ]
        chunks = tuple(int(chunk) for chunk in base_image.data.chunksize)
        multiscales = nz.to_multiscales(
            base_image,
            scale_factors=scale_factors,
            method=nz.Methods.ITKWASM_GAUSSIAN,
            chunks=chunks,
            cache=True,
        )
        for level, dataset in enumerate(multiscales.metadata.datasets):
            dataset.path = str(level)

        nz.to_ome_zarr(
            image_path,
            multiscales,
            version="0.5",
            overwrite=False,
            start_level=1,
            chunks_per_shard=chunks_per_shard,
        )
        logger.info(f"Pyramid image {image_index}/{len(image_paths)} complete: " f"{image_path}")

    logger.info(f"OME-Zarr pyramid generation completed: {zarr_path}")


async def _merge_channel_streams(
    streams: list[tuple[int, AsyncIterable[Any]]],
) -> AsyncIterator[tuple[int, Any]]:
    """Merge explicit channel subscriptions into one asynchronous stream."""
    iterators = [(output_channel, stream.__aiter__()) for output_channel, stream in streams]
    tasks = {
        asyncio.create_task(stream_iter.__anext__()): (output_channel, stream_iter)
        for output_channel, stream_iter in iterators
    }
    try:
        while tasks:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                output_channel, stream_iter = tasks.pop(task)
                try:
                    response = task.result()
                except StopAsyncIteration:
                    continue
                tasks[asyncio.create_task(stream_iter.__anext__())] = (
                    output_channel,
                    stream_iter,
                )
                yield output_channel, response
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def _close_async_iterator(iterator: AsyncIterator[Any]) -> None:
    """Close an asynchronous iterator when its implementation supports it."""
    close = getattr(iterator, "aclose", None)
    if close is not None:
        await close()


async def _cancel_channel_stream(stream: Any) -> None:
    """Reset a persistent monitor before grpclib waits for server trailers."""
    try:
        await stream.cancel()
    except (ProtocolError, StreamTerminatedError):
        pass


async def _open_channel_streams(
    channel: Any,
    metadata: Any,
    source_channels: list[int],
    resources: contextlib.AsyncExitStack,
) -> list[tuple[int, AsyncIterable[Any]]]:
    """Send channel subscriptions before acquisition begins."""
    streams = []
    for source_channel in source_channels:
        request = ExperimentStreamingServiceMonitorAllExperimentsRequest(
            channel_index=source_channel,
            enable_raw_data=False,
        )
        stream = await resources.enter_async_context(
            channel.request(
                "/zen_api.acquisition.v1beta.ExperimentStreamingService/MonitorAllExperiments",
                Cardinality.UNARY_STREAM,
                type(request),
                ExperimentStreamingServiceMonitorAllExperimentsResponse,
                metadata=metadata,
            )
        )
        await stream.send_message(request, end=True)
        resources.push_async_callback(_cancel_channel_stream, stream)
        streams.append(stream)

    return list(enumerate(streams))


async def stream_to_omezarr(
    zenapi_config: str | Path,
    experiment_name: str,
    output_dir: str | Path,
    dtype: np.dtype | None = None,
    start_experiment_from_script: bool = False,
    czi_name: str = "zenapi_stream",
    overwrite_czi: bool = True,
    channel_index: int | None = None,
    overwrite_zarr: bool = True,
    compression: str | None = "blosc-zstd",
    spatial_shard_size_chunks: int | None = 2,
    inactivity_timeout: float = _STATUS_POLL_TIMEOUT,
) -> Path:
    """Stream ZEN pixel data into an OME-ZARR file.

    Args:
        zenapi_config: Path to the ZEN-API gateway config.ini.
        experiment_name: ZEN experiment name (without .czexp).
        output_dir: Folder where the OME-ZARR will be created.
        dtype: Pixel data type (must match the experiment output).
        start_experiment_from_script: If True, start the experiment via API.
            If False, the user starts the experiment from the ZEN UI.
        czi_name: CZI output name when starting experiment from script.
        overwrite_czi: Allow overwriting an existing CZI.
        channel_index: Optional channel filter index (None = all channels).
        overwrite_zarr: Overwrite an existing OME-ZARR at the output path.
        compression: Compression for ZARR
            ('blosc-zstd', 'blosc-lz4', 'zstd', 'none', or None).
        spatial_shard_size_chunks: Number of chunks per Y/X shard, or None
            to disable Zarr v3 sharding.
        inactivity_timeout: Seconds without a new frame before the stream
            is considered finished.

    Returns:
        Path to the created OME-ZARR directory.
    """
    channel, metadata = initialize_zenapi(zenapi_config)
    try:
        async with contextlib.AsyncExitStack() as resources:
            return await _stream_to_omezarr_connected(
                zenapi_config=zenapi_config,
                experiment_name=experiment_name,
                output_dir=output_dir,
                dtype=dtype,
                start_experiment_from_script=start_experiment_from_script,
                czi_name=czi_name,
                overwrite_czi=overwrite_czi,
                channel_index=channel_index,
                overwrite_zarr=overwrite_zarr,
                compression=compression,
                spatial_shard_size_chunks=spatial_shard_size_chunks,
                inactivity_timeout=inactivity_timeout,
                channel=channel,
                metadata=metadata,
                resources=resources,
            )
    finally:
        channel.close()


async def _stream_to_omezarr_connected(
    zenapi_config: str | Path,
    experiment_name: str,
    output_dir: str | Path,
    dtype: np.dtype | None,
    start_experiment_from_script: bool,
    czi_name: str,
    overwrite_czi: bool,
    channel_index: int | None,
    overwrite_zarr: bool,
    compression: str | None,
    spatial_shard_size_chunks: int | None,
    inactivity_timeout: float,
    channel: Any,
    metadata: Any,
    resources: contextlib.AsyncExitStack,
) -> Path:
    """Implement buffered streaming with exception-safe resources."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # derive OME-ZARR name from experiment + timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zarr_name = f"{experiment_name}_{timestamp}.ome.zarr"
    zarr_path = output_dir / zarr_name
    logger.info(f"OME-ZARR output path: {zarr_path}")

    streaming_service = ExperimentStreamingServiceStub(channel=channel, metadata=metadata)

    # ----- open the pixel stream FIRST (before starting the experiment) -----
    # Opening before triggering the experiment ensures no early frames are
    # dropped.  monitor_all_experiments is a perpetual gRPC server-streaming
    # call – the server pushes frames as soon as they are produced.
    async_iterable = streaming_service.monitor_all_experiments(
        ExperimentStreamingServiceMonitorAllExperimentsRequest(
            channel_index=channel_index,
            enable_raw_data=False,
        )
    )
    logger.info("Pixel stream opened (monitoring all experiments).")

    # ----- now optionally start the experiment -----
    if start_experiment_from_script:
        logger.info(f"Starting experiment '{experiment_name}' via ZEN-API ...")
        exp_id, czi_path = await start_experiment(
            exp_name=experiment_name,
            czi_name=czi_name,
            overwrite=overwrite_czi,
            zenapi_config=zenapi_config,
        )
        logger.info(f"Experiment ID: {exp_id}")
        logger.info(f"CZI file will be saved to: {czi_path}")
    else:
        logger.info(
            "Waiting for experiment to be started from ZEN UI. "
            f"Stream stops after {inactivity_timeout:.0f}s of inactivity."
        )

    # ----- collect frames & metadata in first pass to build dimensions -----
    # We need to know the full extent of the acquisition before we can
    # create the AcquisitionSettings.  We accumulate all frames in memory,
    # track the coordinate ranges, then write them out.
    #
    # For very large acquisitions a two-pass approach (peek first frame for
    # size, assume bounded dims from experiment metadata) would be better,
    # but ZEN API does not currently expose the full experiment shape upfront.

    frames: list[np.ndarray] = []
    frame_coords: list[dict] = []  # list of {t, z, c, m, s} per frame
    frame_metadata_list: list[dict] = []

    # track dimension extents
    max_t = 0
    max_z = 0
    max_c = 0
    max_m = 0  # tiles
    max_s = 0  # scenes

    frame_height: int | None = None
    frame_width: int | None = None
    scale_x: float | None = None
    scale_y: float | None = None

    frame_count = 0

    logger.info("Receiving pixel stream ...")
    recv_bar = napari_progress(desc="Receiving frames")
    resources.callback(recv_bar.close)

    # We iterate the gRPC stream manually (via __aiter__ + __anext__)
    # instead of `async for` so we can wrap each step in wait_for() to
    # detect inactivity (no frame for `inactivity_timeout` seconds).
    # ZEN can report the experiment finished before all pixel payloads have
    # arrived, so status is intentionally not used as a stop condition.
    async_iter = async_iterable.__aiter__()
    resources.push_async_callback(_close_async_iterator, async_iter)
    while True:
        try:
            response = await asyncio.wait_for(async_iter.__anext__(), timeout=inactivity_timeout)
        except asyncio.TimeoutError:
            logger.info(f"No frames for {inactivity_timeout:.0f}s" " - assuming experiment finished.")
            break
        except StopAsyncIteration:
            break

        fd = response.frame_data
        fp = fd.frame_position
        frame_expID = fd.experiment_id
        logger.info(f"Received frame from experiment ID: {frame_expID}")

        # Extract 5-D acquisition coordinate of this frame.
        t = fp.t
        z = fp.z
        c = fp.c
        m = fp.m  # tile index
        s = fp.s  # scene index

        full_size = fd.frame_size
        frame_dtype = _runtime_grayscale_dtype(
            fd.pixel_data.pixel_type,
            len(fd.pixel_data.raw_data),
            full_size.width,
            full_size.height,
        )
        if dtype is None:
            dtype = frame_dtype
            logger.info(
                "Using runtime stream pixel type %s (%s).",
                fd.pixel_data.pixel_type.name,
                dtype,
            )
        elif np.dtype(dtype) != frame_dtype:
            raise ValueError(
                "Configured dtype does not match the streamed pixel type: " f"{np.dtype(dtype)} != {frame_dtype}."
            )
        # ZEN API reports scaling in metres; convert to µm for OME metadata.
        sx = fd.scaling.x * 1e6  # m -> µm
        sy = fd.scaling.y * 1e6

        # Stage positions also in metres -> µm.
        # type: ignore[operator]: stubs declare float | None but values are
        # always numeric during an active acquisition.
        stage_x = fd.frame_stage_position.x * 1e6  # type: ignore[operator]
        stage_y = fd.frame_stage_position.y * 1e6  # type: ignore[operator]
        stage_z = fd.frame_stage_position.z * 1e6  # type: ignore[operator]

        frame = _decode_grayscale_frame(
            fd.pixel_data.raw_data,
            dtype,
            full_size.width,
            full_size.height,
        )
        if frame_height is not None and frame.shape != (frame_height, frame_width):
            raise ValueError(
                "Streamed frame shape changed during acquisition: " f"{(frame_height, frame_width)} to {frame.shape}."
            )

        # Accumulate frames and coordinates for the second-pass write.
        frames.append(frame)
        frame_coords.append({"t": t, "z": z, "c": c, "m": m, "s": s})
        frame_metadata_list.append(
            {
                "position_x": stage_x,
                "position_y": stage_y,
                "position_z": stage_z,
            }
        )

        # Track the highest seen index in each dimension so we can infer
        # the full shape once the stream ends (max_index + 1 == count).
        max_t = max(max_t, t)
        max_z = max(max_z, z)
        max_c = max(max_c, c)
        max_m = max(max_m, m)
        max_s = max(max_s, s)

        # Capture pixel dimensions and physical scale from the first frame.
        if frame_height is None:
            frame_height = frame.shape[0]
            frame_width = frame.shape[1]
            scale_x = sx
            scale_y = sy

        frame_count += 1
        recv_bar.update(1)

    recv_bar.close()
    logger.info(f"Stream finished. Total frames received: {frame_count}")

    if frame_count == 0:
        logger.warning("No frames received. Nothing to write.")
        return zarr_path

    # ----- build OME-ZARR dimensions from observed extents -----
    # ZEN uses 0-based indices; the count in each dimension is max_idx + 1.
    num_t = max_t + 1
    num_z = max_z + 1
    num_c = max_c + 1
    num_m = max_m + 1  # tile count
    num_s = max_s + 1  # scene count

    # ome-writers models scenes × tiles as a flat “positions” dimension.
    num_positions = num_s * num_m

    # Build the dimension list in the order ome-writers expects:
    #   optional T → optional P → optional C → optional Z → Y → X.
    # Singleton dimensions (count == 1) are omitted to keep the ZARR
    # shape as compact as possible.
    dimensions: list[Dimension] = []

    if num_t > 1:
        dimensions.append(Dimension(name="t", count=num_t, chunk_size=1, type="time"))

    # Flatten scene × tile into a named position list.  ome-writers uses
    # the grid_row / grid_column values to build a multi-position mosaic.
    if num_positions > 1:
        pos_coords = [
            Position(
                name=f"S{s_idx}_M{m_idx}",
                grid_row=s_idx,
                grid_column=m_idx,
            )
            for s_idx, m_idx in itertools.product(range(num_s), range(num_m))
        ]
        dimensions.append(Dimension(name="p", type="position", coords=pos_coords))  # type: ignore[arg-type]

    if num_c > 1:
        dimensions.append(Dimension(name="c", count=num_c, chunk_size=1, type="channel"))

    if num_z > 1:
        # Derive Z spacing from the physical stage-Z positions recorded
        # per frame.  Scan buffered frames until we have one reading per
        # Z-index, then take the difference between adjacent slices.
        # frame_coords and frame_metadata_list are always the same length
        # (populated together in the same loop).  # noqa: B905
        z_stage_positions: dict[int, float] = {}
        for fc, fmeta in zip(frame_coords, frame_metadata_list):  # noqa: B905
            z_idx = fc["z"]
            if z_idx not in z_stage_positions:
                z_stage_positions[z_idx] = fmeta["position_z"]
            # Stop early once we have one reading per Z plane.
            if len(z_stage_positions) == num_z:
                break
        if len(z_stage_positions) >= 2:
            sorted_z = sorted(z_stage_positions.items())
            z_spacing = abs(sorted_z[1][1] - sorted_z[0][1])
            # Guard against widefield experiments mis-reported as Z > 1
            # where all planes share the same stage position.
            if z_spacing == 0:
                z_spacing = 1.0
        else:
            z_spacing = 1.0
        logger.info(f"Z spacing from stage positions: {z_spacing:.4f} µm")

        dimensions.append(
            Dimension(
                name="z",
                count=num_z,
                chunk_size=max(1, num_z),
                type="space",
                scale=z_spacing,
                unit="um",
            )
        )

    # Y and X (always present, must be last two).
    # frame_height/width are guaranteed non-None since frame_count > 0.
    assert frame_height is not None and frame_width is not None
    dimensions.append(
        Dimension(
            name="y",
            count=frame_height,
            chunk_size=min(512, frame_height),
            shard_size_chunks=spatial_shard_size_chunks,
            type="space",
            scale=scale_y,
            unit="um",
        )
    )
    dimensions.append(
        Dimension(
            name="x",
            count=frame_width,
            chunk_size=min(512, frame_width),
            shard_size_chunks=spatial_shard_size_chunks,
            type="space",
            scale=scale_x,
            unit="um",
        )
    )

    settings = AcquisitionSettings(
        root_path=str(zarr_path),
        dimensions=tuple(dimensions),
        dtype=str(dtype),
        format="ome-zarr",  # type: ignore[arg-type]  # ome-writers accepts str
        compression=compression,  # type: ignore[arg-type]  # ome-writers accepts str
        overwrite=overwrite_zarr,
    )

    logger.info(f"AcquisitionSettings created: shape={settings.shape}")
    total_expected = settings.num_frames
    logger.info(f"Total expected frames: {total_expected}")

    # ----- reorder frames into ome-writers' expected write order -----
    # ZEN delivers frames in channel-major order (all C=0 first, then C=1,
    # etc.) which differs from the T→P→C→Z order ome-writers requires.
    # Build a lookup (5-D coord → buffer index) so we can feed frames in
    # the correct sequence during the write pass.
    coord_to_idx: dict[tuple, int] = {}
    for idx, fc in enumerate(frame_coords):
        key = (fc["t"], fc["s"], fc["m"], fc["c"], fc["z"])
        coord_to_idx[key] = idx

    # ----- write to OME-ZARR -----
    # itertools.product generates (t, s, m, c, z) tuples in the same
    # T→S→M→C→Z order as the dimension list, so the stream receives
    # frames in the expected sequence.  For any missing frame we call
    # stream.skip() to keep the ZARR layout fully rectangular.
    logger.info(f"Writing {frame_count} frames to OME-ZARR ...")

    with create_stream(settings) as stream:
        coords = itertools.product(range(num_t), range(num_s), range(num_m), range(num_c), range(num_z))
        for t_idx, s_idx, m_idx, c_idx, z_idx in napari_progress(coords, total=total_expected, desc="Writing frames"):
            key = (t_idx, s_idx, m_idx, c_idx, z_idx)
            if key in coord_to_idx:
                fidx = coord_to_idx[key]
                stream.append(
                    frames[fidx],
                    frame_metadata=frame_metadata_list[fidx],
                )
            else:
                # Frame was not received (dropped or filtered by
                # channel_index).  Emit a placeholder so the ZARR shape
                # remains rectangular.
                stream.skip(frames=1)
                logger.warning(f"Missing frame at " f"T={t_idx} S={s_idx} M={m_idx} C={c_idx} Z={z_idx}")

    logger.info(f"OME-ZARR written successfully: {zarr_path}")
    return zarr_path


async def stream_to_omezarr_with_config(
    ecfg: ExperimentConfig,
    inactivity_timeout: float = _STATUS_POLL_TIMEOUT,
    progress_callback: Callable[[int, int], None] | None = None,
    on_experiment_started: Callable[[str], None] | None = None,
) -> Path:
    """Stream ZEN pixel data into OME-ZARR using known dimensions from config.

    Generic multiposition output writes source frames on-the-fly. HCS output
    buffers the M tiles belonging to one ``(T, S, C, Z)`` plane, assembles
    the scene mosaic in memory, and writes that final plane directly. It does
    not create source-tile arrays or rewrite the completed ZARR store.

    Args:
        ecfg: Parsed ExperimentConfig.
        inactivity_timeout: Seconds to wait for the next frame before
            treating the acquisition as finished.
        progress_callback: Optional callable ``(current, total) -> None``
            for progress reporting.  Sentinel values for ``current``:
            ``-1`` = writing started (total is now known);
            ``-2`` = acquisition complete.
            When *None* a tqdm bar is displayed in the terminal instead.
        on_experiment_started: Optional callback receiving the experiment ID
            when an acquisition is started via ZEN API.

    Returns:
        Path to the created OME-ZARR directory.
    """
    channel, metadata = initialize_zenapi(ecfg.zenapi_config)
    try:
        async with contextlib.AsyncExitStack() as resources:
            zarr_path = await _stream_to_omezarr_with_config_connected(
                ecfg,
                inactivity_timeout,
                progress_callback,
                on_experiment_started,
                channel,
                metadata,
                resources,
            )
    finally:
        channel.close()

    if ecfg.pyramid_levels > 0:
        generate_omezarr_pyramids(
            zarr_path,
            ecfg.pyramid_levels,
            ecfg.spatial_shard_size_chunks,
        )
    return zarr_path


async def _stream_to_omezarr_with_config_connected(
    ecfg: ExperimentConfig,
    inactivity_timeout: float,
    progress_callback: Callable[[int, int], None] | None,
    on_experiment_started: Callable[[str], None] | None,
    channel: Any,
    metadata: Any,
    resources: contextlib.AsyncExitStack,
) -> Path:
    """Implement configured streaming with exception-safe resources."""
    dtype: np.dtype | None = None
    configured_dtype = np.dtype(ecfg.dtype)
    output_dir = Path(ecfg.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zarr_name = f"{ecfg.experiment_name}_{timestamp}.ome.zarr"
    zarr_path = output_dir / zarr_name
    logger.info(f"OME-ZARR output path: {zarr_path}")

    num_t = ecfg.time_points
    num_c = ecfg.channels
    num_z = ecfg.z_planes
    num_m = ecfg.tiles
    num_s = ecfg.scenes
    experiment_service = ExperimentServiceStub(channel=channel, metadata=metadata)
    streaming_service = ExperimentStreamingServiceStub(channel=channel, metadata=metadata)

    # ----- open the pixel stream FIRST (before starting the experiment) -----
    # This ensures no early frames are missed. We use
    # monitor_all_experiments because the experiment_id does not exist yet.
    source_channels = [ecfg.channel_index] if ecfg.channel_index is not None else list(range(max(1, num_c)))
    streams: list[tuple[int, AsyncIterable[Any]]]
    if ecfg.start_from_script:
        streams = await _open_channel_streams(channel, metadata, source_channels, resources)
    else:
        streams = [
            (
                output_channel,
                streaming_service.monitor_all_experiments(
                    ExperimentStreamingServiceMonitorAllExperimentsRequest(
                        channel_index=source_channel,
                        enable_raw_data=False,
                    )
                ),
            )
            for output_channel, source_channel in enumerate(source_channels)
        ]
    async_iterable = _merge_channel_streams(streams)
    logger.info(
        "Pixel streams opened for "
        + (
            f"selected channel {ecfg.channel_index}."
            if ecfg.channel_index is not None
            else f"all {len(source_channels)} channels."
        )
    )

    # ----- now optionally start the experiment -----
    if ecfg.start_from_script:
        logger.info(f"Starting experiment '{ecfg.experiment_name}' via ZEN-API ...")
        exp_id, czi_path = await start_experiment(
            exp_name=ecfg.experiment_name,
            czi_name=ecfg.czi_name,
            overwrite=ecfg.overwrite_czi,
            zenapi_config=ecfg.zenapi_config,
        )
        logger.info(f"Experiment ID: {exp_id}")
        logger.info(f"CZI file will be saved to: {czi_path}")
        if on_experiment_started is not None:
            on_experiment_started(exp_id)
        await asyncio.wait_for(
            asyncio.gather(*(stream.recv_initial_metadata() for _, stream in streams)),
            timeout=inactivity_timeout,
        )
    else:
        logger.info(
            "Waiting for experiment to be started from ZEN UI. "
            f"Stream stops after {inactivity_timeout:.0f}s of inactivity."
        )

    # ----- pre-compute write order for every (t, s, m, c, z) combination -----
    # Track raw input in T→S→M→C→Z order. Generic output uses the same order;
    # direct HCS output maps each complete M-tile set to T→S→C→Z instead.
    # ZEN delivers frames in channel-major order, so pending planes are
    # flushed only when their next required linear index is available.
    coord_to_linear: dict[tuple[int, int, int, int, int], int] = {}
    output_coord_to_linear: dict[tuple[int, int, int, int], int] = {}

    # `pending` buffers frames that arrived out of order.  Each entry maps
    # linear_index → (frame_array, frame_metadata).  We flush it greedily:
    # whenever a new frame arrives we pop consecutive entries starting at
    # next_write and append them to the ZARR stream immediately, keeping
    # memory proportional to the max out-of-order gap rather than the full
    # acquisition size.
    pending: dict[int, tuple[np.ndarray, dict]] = {}
    hcs_plane_tiles: dict[
        tuple[int, int, int, int],
        dict[int, tuple[np.ndarray, dict]],
    ] = {}
    scene_geometries: dict[int, SceneGeometry] = {}
    received_indices: set[int] = set()
    tile_geometries: dict[tuple[int, int], TileGeometry] = {}
    # next_write: the linear index that ome-writers is waiting for next.
    next_write = 0
    # frame_count: number of valid frames received (used for break condition).
    frame_count = 0

    # The OME-ZARR stream is opened lazily on the first frame because
    # AcquisitionSettings needs pixel dimensions (H × W) and physical pixel
    # scale (µm/px), which come from the frame data itself.
    zarr_stream = None
    zarr_resources = contextlib.ExitStack()
    resources.callback(zarr_resources.close)
    settings = None
    plate = None
    direct_hcs = False
    layout_initialized = False
    output_height = 0
    output_width = 0
    total_expected: int | None = None
    expected_frame_shape: tuple[int, int] | None = None

    logger.info("Waiting for first frame to determine frame size ...")

    # Progress reporting: when a callback is provided (plugin mode) delegate
    # to it so the napari activity dock is updated on the main thread via a
    # Qt signal.  In CLI mode (no callback) use a tqdm terminal bar instead.
    _tqdm_bar: Any = None  # only used in CLI mode
    progress_closed = False

    def _progress_init(total: int) -> None:
        """Open the progress bar once the total frame count is known."""
        nonlocal _tqdm_bar
        if progress_callback is not None:
            # Sentinel -1: writing phase started, total now known.
            progress_callback(-1, total)
        else:
            from tqdm import tqdm

            _tqdm_bar = tqdm(total=total, desc="Streaming frames", unit="frame")

    def _progress_update(written: int) -> None:
        """Advance the bar after each frame is written."""
        if progress_callback is not None:
            progress_callback(written, total_expected or 0)
        elif _tqdm_bar is not None:
            _tqdm_bar.update(1)

    def _progress_close() -> None:
        """Close the bar when the acquisition is complete."""
        nonlocal progress_closed
        if progress_closed:
            return
        progress_closed = True
        if progress_callback is not None:
            progress_callback(-2, 0)
        elif _tqdm_bar is not None:
            _tqdm_bar.close()

    resources.callback(_progress_close)

    def _open_output_stream(
        frame_height: int,
        frame_width: int,
        scale_y: float,
        scale_x: float,
        pos_coords: list[Position],
        output_plate: Plate | None,
    ) -> tuple[Any, AcquisitionSettings]:
        """Create the final OME-ZARR stream for raw or merged planes."""
        assert dtype is not None
        dimensions: list[Dimension] = []
        if num_t > 1:
            dimensions.append(Dimension(name="t", count=num_t, chunk_size=1, type="time"))
        if len(pos_coords) > 1 or output_plate is not None:
            dimensions.append(
                Dimension(
                    name="p",
                    type="position",
                    coords=pos_coords,
                )
            )
        if num_c > 1:
            dimensions.append(Dimension(name="c", count=num_c, chunk_size=1, type="channel"))
        if num_z > 1:
            dimensions.append(
                Dimension(
                    name="z",
                    count=num_z,
                    chunk_size=max(1, num_z),
                    type="space",
                    scale=ecfg.z_spacing,
                    unit="um",
                )
            )
        dimensions.extend(
            [
                Dimension(
                    name="y",
                    count=frame_height,
                    chunk_size=min(512, frame_height),
                    shard_size_chunks=ecfg.spatial_shard_size_chunks,
                    type="space",
                    scale=scale_y,
                    unit="um",
                ),
                Dimension(
                    name="x",
                    count=frame_width,
                    chunk_size=min(512, frame_width),
                    shard_size_chunks=ecfg.spatial_shard_size_chunks,
                    type="space",
                    scale=scale_x,
                    unit="um",
                ),
            ]
        )
        output_settings = AcquisitionSettings(
            root_path=str(zarr_path),
            dimensions=tuple(dimensions),
            dtype=str(dtype),
            format="ome-zarr",  # type: ignore[arg-type]
            compression=ecfg.compression,  # type: ignore[arg-type]
            plate=output_plate,
            overwrite=ecfg.overwrite_zarr,
        )
        logger.info(
            f"AcquisitionSettings: shape={output_settings.shape} "
            f"output_frames={output_settings.num_frames} "
            f"input_frames={len(coord_to_linear)}"
        )
        return zarr_resources.enter_context(create_stream(output_settings)), output_settings

    # We iterate the gRPC stream manually (via __aiter__ + __anext__)
    # instead of `async for` so we can wrap each step in wait_for() to
    # detect inactivity (no frame for `inactivity_timeout` seconds).
    # In config mode the total frame count is known upfront, so the
    # primary exit condition is frame_count >= total_expected.
    # Intentionally NO status-monitor stop_event here: ZEN delivers the
    # experiment-finished status *before* all pixel data has been pushed
    # through gRPC, so reacting to it would truncate the write.
    async_iter = async_iterable.__aiter__()
    resources.push_async_callback(_close_async_iterator, async_iter)
    while True:
        try:
            stream_c, response = await asyncio.wait_for(async_iter.__anext__(), timeout=inactivity_timeout)
        except asyncio.TimeoutError:
            logger.info(f"No frames for {inactivity_timeout:.0f}s - assuming experiment finished.")
            break
        except StopAsyncIteration:
            break

        fd = response.frame_data
        fp = fd.frame_position

        # 5-D acquisition coordinate of this frame.
        t, z, m, s = fp.t, fp.z, fp.m, fp.s
        c = stream_c

        full_size = fd.frame_size
        frame_dtype = _runtime_grayscale_dtype(
            fd.pixel_data.pixel_type,
            len(fd.pixel_data.raw_data),
            full_size.width,
            full_size.height,
        )
        if dtype is None:
            dtype = frame_dtype
            logger.info(
                "Using runtime stream pixel type %s (%s).",
                fd.pixel_data.pixel_type.name,
                dtype,
            )
            if dtype != configured_dtype:
                logger.warning(
                    "Configured dtype %s differs from runtime dtype %s; " "using the runtime value.",
                    configured_dtype,
                    dtype,
                )
        elif frame_dtype != dtype:
            raise ValueError("Streamed pixel type changed during acquisition: " f"{dtype} to {frame_dtype}.")

        # ZEN API reports scale in metres; convert to µm for OME metadata.
        sx = fd.scaling.x * 1e6
        sy = fd.scaling.y * 1e6
        # Stage positions in metres → µm. Scene assembly requires real stage
        # coordinates; substituting zero would silently overlap tiles.
        stage_position = fd.frame_stage_position
        if stage_position.x is None or stage_position.y is None or stage_position.z is None:
            raise ValueError("Streaming frame has no complete stage position")
        stage_x = stage_position.x * 1e6
        stage_y = stage_position.y * 1e6
        stage_z = stage_position.z * 1e6

        frame = _decode_grayscale_frame(
            fd.pixel_data.raw_data,
            dtype,
            full_size.width,
            full_size.height,
        )
        if expected_frame_shape is None:
            expected_frame_shape = frame.shape
        elif frame.shape != expected_frame_shape:
            raise ValueError(
                "Streamed frame shape changed during acquisition: " f"{expected_frame_shape} to {frame.shape}."
            )

        frame_meta = {
            "position_x": stage_x,
            "position_y": stage_y,
            "position_z": stage_z,
        }

        # ----- first frame: build AcquisitionSettings & open the ZARR stream -----
        # Deferred until now because ome-writers needs pixel dimensions
        # (H × W) and physical scale (µm/px) from the frame data itself.
        if not layout_initialized:
            try:
                status_response = await experiment_service.get_status(ExperimentServiceGetStatusRequest())
                status = status_response.status
                if status.time_points_count > 0:
                    num_t = status.time_points_count
                if status.channels_count > 0:
                    num_c = status.channels_count
                if status.zstack_slices_count > 0:
                    num_z = status.zstack_slices_count
                if status.scenes_count > 0:
                    num_s = status.scenes_count
                if status.tiles_count > 0:
                    num_m = status.tiles_count
                logger.info(
                    f"Live dimensions: T={num_t}, C={num_c}, Z={num_z}, "
                    f"Tiles(M)={num_m}, Scenes(S)={num_s}, "
                    f"images={status.images_count}"
                )
            except Exception as exc:
                logger.warning(
                    "Could not query live dimensions; using config values: %s",
                    exc,
                )

            if ecfg.channel_index is not None:
                num_c = 1
                logger.info(
                    "Streaming selected channel %d as singleton C axis.",
                    ecfg.channel_index,
                )
            else:
                num_c = len(source_channels)

            pos_coords, plate = _build_position_layout(ecfg, num_s, num_m)
            direct_hcs = plate is not None and num_m > 1
            coord_to_linear = {
                coord: idx
                for idx, coord in enumerate(
                    itertools.product(
                        range(num_t),
                        range(num_s),
                        range(num_m),
                        range(num_c),
                        range(num_z),
                    )
                )
            }
            if direct_hcs:
                output_coord_to_linear = {
                    coord: idx
                    for idx, coord in enumerate(
                        itertools.product(
                            range(num_t),
                            range(num_s),
                            range(num_c),
                            range(num_z),
                        )
                    )
                }
            frame_height, frame_width = frame.shape
            scale_x = sx
            scale_y = sy

            logger.info(
                f"Frame size from stream: {frame_height}x{frame_width}, "
                f"pixel scale: {scale_x:.4f} x {scale_y:.4f} µm"
            )
            logger.info(
                f"Known dimensions: T={num_t}, C={num_c}, Z={num_z}, "
                f"Tiles(M)={num_m}, Scenes(S)={num_s}, "
                f"Frame={frame_height}x{frame_width}"
            )

            if plate is not None:
                logger.info(
                    "OME-ZARR HCS layout enabled for wells: %s",
                    ", ".join(dict.fromkeys(f"{position.plate_row}{position.plate_column}" for position in pos_coords)),
                )
            total_expected = len(coord_to_linear)
            _progress_init(total_expected)
            layout_initialized = True
            if direct_hcs:
                logger.info("HCS direct mosaic mode: waiting for " f"{num_s * num_m} tile geometries.")
            else:
                zarr_stream, settings = _open_output_stream(
                    frame_height,
                    frame_width,
                    scale_y,
                    scale_x,
                    pos_coords,
                    plate,
                )
                logger.info("Receiving & writing pixel stream on-the-fly ...")

        # ----- dispatch the current frame -----
        # Translate the 5-D coordinate to a linear write index.  Frames
        # from outside the expected coordinate space (e.g. a concurrent
        # experiment) are silently dropped.
        key = (t, s, m, c, z)
        lin_idx = coord_to_linear.get(key)
        if lin_idx is None:
            logger.warning(f"Unexpected coordinate {key} – skipping frame.")
            continue

        tile_key = (s, m)
        if tile_key not in tile_geometries or z == 0:
            tile_geometries[tile_key] = TileGeometry(
                scene_index=s,
                tile_index=m,
                position_index=s * num_m + m,
                center_x=stage_x,
                center_y=stage_y,
                position_z=stage_z,
                scale_x=sx,
                scale_y=sy,
                height=frame.shape[0],
                width=frame.shape[1],
            )

        if lin_idx in received_indices:
            logger.warning(f"Duplicate coordinate {key} - ignoring frame.")
            continue

        received_indices.add(lin_idx)
        frame_count = len(received_indices)
        _progress_update(frame_count)

        if direct_hcs:
            plane_key = (t, s, c, z)
            hcs_plane_tiles.setdefault(plane_key, {})[m] = (
                frame,
                frame_meta,
            )

            if zarr_stream is None and len(tile_geometries) == num_s * num_m:
                scene_geometries = {
                    scene_index: calculate_scene_geometry(
                        [tile_geometries[(scene_index, tile_index)] for tile_index in range(num_m)]
                    )
                    for scene_index in range(num_s)
                }
                output_height = max(geometry.height for geometry in scene_geometries.values())
                output_width = max(geometry.width for geometry in scene_geometries.values())
                zarr_stream, settings = _open_output_stream(
                    output_height,
                    output_width,
                    sy,
                    sx,
                    pos_coords,
                    plate,
                )
                logger.info(f"HCS scene mosaic canvas: Y={output_height} " f"X={output_width}")

            if zarr_stream is not None:
                for buffered_key, tile_records in list(hcs_plane_tiles.items()):
                    if len(tile_records) < num_m:
                        continue
                    _, scene_index, _, _ = buffered_key
                    geometry = scene_geometries[scene_index]
                    mosaic = _assemble_scene_plane(
                        {tile_index: record[0] for tile_index, record in tile_records.items()},
                        geometry,
                        output_height,
                        output_width,
                    )
                    mosaic_meta = dict(next(iter(tile_records.values()))[1])
                    mosaic_meta["position_x"] = geometry.origin_x
                    mosaic_meta["position_y"] = geometry.origin_y
                    output_index = output_coord_to_linear[buffered_key]
                    pending[output_index] = (mosaic, mosaic_meta)
                    del hcs_plane_tiles[buffered_key]
                while next_write in pending:
                    mosaic, mosaic_meta = pending.pop(next_write)
                    zarr_stream.append(
                        mosaic,
                        frame_metadata=mosaic_meta,
                    )
                    next_write += 1
        else:
            # Reorder ZEN's channel-major delivery into the raw tile order
            # required by ome-writers. Generic mode retains this path so its
            # optional individual source-tile groups remain available.
            pending[lin_idx] = (frame, frame_meta)
            while next_write in pending:
                frm, fmeta = pending.pop(next_write)
                zarr_stream.append(frm, frame_metadata=fmeta)
                next_write += 1

        buffered_frames = len(pending)
        if direct_hcs:
            buffered_frames += sum(len(tile_records) for tile_records in hcs_plane_tiles.values())
        if buffered_frames > ecfg.max_pending_frames:
            raise BufferError(
                "Out-of-order pixel buffer exceeded "
                f"max_pending_frames={ecfg.max_pending_frames}; "
                f"next expected linear index is {next_write}."
            )

        # Break as soon as all expected frames have been received.
        # monitor_all_experiments never closes on its own; without this
        # check the loop would block indefinitely on the next wait_for().
        if len(received_indices) == len(coord_to_linear):
            logger.info(
                f"Closing pixel stream (received "
                f"{frame_count}/{total_expected} frames, "
                f"last linear index={lin_idx})."
            )
            break

    if zarr_stream is None:
        _progress_close()
        if direct_hcs:
            missing_geometry = sorted(set(itertools.product(range(num_s), range(num_m))) - set(tile_geometries))
            raise RuntimeError(
                "Cannot create direct HCS output because tile geometry " f"was not received for {missing_geometry}."
            )
        raise RuntimeError("No pixel frames were received; no OME-ZARR was created.")

    # After the receive loop exits, drain any frames still sitting in
    # `pending` and fill gaps (frames that never arrived) with skip()
    # placeholders so the ZARR layout remains fully rectangular.
    # This handles the inactivity-timeout exit path where the buffer
    # may still contain valid out-of-order frames.
    if zarr_stream is not None:
        if direct_hcs:
            for buffered_key, tile_records in list(hcs_plane_tiles.items()):
                if not tile_records:
                    continue
                _, scene_index, _, _ = buffered_key
                geometry = scene_geometries[scene_index]
                mosaic = _assemble_scene_plane(
                    {tile_index: record[0] for tile_index, record in tile_records.items()},
                    geometry,
                    output_height,
                    output_width,
                )
                mosaic_meta = dict(next(iter(tile_records.values()))[1])
                mosaic_meta["position_x"] = geometry.origin_x
                mosaic_meta["position_y"] = geometry.origin_y
                pending[output_coord_to_linear[buffered_key]] = (
                    mosaic,
                    mosaic_meta,
                )
            output_frame_count = len(output_coord_to_linear)
        else:
            output_frame_count = len(coord_to_linear)

        while next_write < output_frame_count:
            if next_write in pending:
                frm, fmeta = pending.pop(next_write)
                zarr_stream.append(frm, frame_metadata=fmeta)
            else:
                zarr_stream.skip(frames=1)
                logger.warning(f"Missing frame at linear index" f" {next_write} – skipped.")
            next_write += 1

        # Finalize the writer before rewriting HCS metadata or moving groups.
        zarr_resources.close()
        if plate is not None:
            _materialize_hcs_row_groups(zarr_path)
        elif num_s * num_m > 1:
            _assemble_scene_mosaics(
                zarr_path,
                list(tile_geometries.values()),
                keep_source_tiles=ecfg.keep_source_tiles,
            )

    _progress_close()
    logger.info(f"OME-ZARR written successfully: {zarr_path}")
    logger.info(f"Frames received: {frame_count} / {total_expected} expected")
    return zarr_path


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Stream ZEN pixel data into OME-ZARR via ZEN-API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--experiment-config",
        type=str,
        default=None,
        help=(
            "Path to an experiment config INI file (see experiment_config.ini). "
            "When provided, dimensions are known upfront and frames are written "
            "on-the-fly. Most other CLI flags are ignored except "
            "--start-experiment / --no-start-experiment which override the INI."
        ),
    )
    parser.add_argument(
        "--zenapi-config",
        type=str,
        default=str(Path(__file__).parent / "config.ini"),
        help="Path to the ZEN-API configuration file.",
    )
    parser.add_argument(
        "--experiment",
        type=str,
        required=False,
        default=None,
        help="ZEN experiment name (without .czexp extension). Required unless --experiment-config is used.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=".",
        help="Directory to store the OME-ZARR output.",
    )
    parser.add_argument(
        "--dtype",
        type=str,
        default="uint16",
        choices=["uint8", "uint16", "float32"],
        help="Pixel data type (must match experiment output).",
    )
    parser.add_argument(
        "--start-experiment",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Start the ZEN experiment from the script. "
            "Use --no-start-experiment to wait for the user to start from ZEN UI. "
            "In config mode this overrides the INI 'start_from_script' setting."
        ),
    )
    parser.add_argument(
        "--czi-name",
        type=str,
        default="zenapi_stream",
        help="CZI output name when starting experiment from script (without .czi).",
    )
    parser.add_argument(
        "--channel-index",
        type=int,
        default=None,
        help="Filter pixel stream by channel index (None = all channels).",
    )
    parser.add_argument(
        "--compression",
        type=str,
        default="blosc-zstd",
        choices=["blosc-zstd", "blosc-lz4", "zstd", "none"],
        help="Compression algorithm for the OME-ZARR store.",
    )
    parser.add_argument(
        "--no-overwrite-zarr",
        action="store_true",
        default=False,
        help="Do not overwrite an existing OME-ZARR at the output path.",
    )
    parser.add_argument(
        "--pyramid-levels",
        type=int,
        default=None,
        help=(
            "Number of 2x Y/X pyramid levels to generate after acquisition. "
            "In config mode this overrides the INI value."
        ),
    )
    parser.add_argument(
        "--spatial-shard-size-chunks",
        type=int,
        default=None,
        help=("Number of chunks per Y/X shard; 0 disables sharding. " "In config mode this overrides the INI value."),
    )
    parser.add_argument(
        "--inactivity-timeout",
        type=float,
        default=_STATUS_POLL_TIMEOUT,
        help="Seconds without a frame before acquisition is considered complete.",
    )
    parser.add_argument(
        "--viewer",
        type=str,
        default=None,
        choices=["ndv", "napari"],
        help=(
            "Open the OME-ZARR in a viewer after acquisition. "
            "'ndv' uses the ndv viewer (requires ndv, zarr, dask, xarray). "
            "'napari' uses napari (requires napari, napari-ome-zarr). "
            "Omit to skip."
        ),
    )

    return parser.parse_args()


async def _run_configured_experiment(ecfg: ExperimentConfig, inactivity_timeout: float) -> Path:
    """Export experiment metadata before opening the configured pixel streams."""
    channel, metadata = initialize_zenapi(ecfg.zenapi_config)
    try:
        service = ExperimentServiceStub(channel=channel, metadata=metadata)
        acquisition = await load_experiment_acquisition(service, ecfg.experiment_name)
    finally:
        channel.close()

    ecfg.time_points = acquisition.time_points
    ecfg.channels = acquisition.channels
    ecfg.z_planes = acquisition.z_planes
    ecfg.z_spacing = acquisition.z_spacing
    ecfg.tiles = acquisition.tiles
    ecfg.scenes = acquisition.scenes
    ecfg.positions = list(acquisition.positions)
    return await stream_to_omezarr_with_config(ecfg, inactivity_timeout=inactivity_timeout)


def main() -> None:
    """Entry point: parse arguments, run the appropriate streaming mode, and open the viewer."""
    dotenv.load_dotenv(Path(__file__).parent / ".env")
    configure_logging()
    args = parse_args()

    if args.experiment_config:
        # ---------- config-file mode (exported dimensions, on-the-fly write) ----------
        ecfg = load_experiment_config(args.experiment_config)
        # CLI --start-experiment / --no-start-experiment overrides the INI value
        if args.start_experiment is not None:
            ecfg.start_from_script = args.start_experiment
        if args.pyramid_levels is not None:
            if args.pyramid_levels < 0:
                raise ValueError("--pyramid-levels must be non-negative")
            ecfg.pyramid_levels = args.pyramid_levels
        if args.spatial_shard_size_chunks is not None:
            if args.spatial_shard_size_chunks < 0:
                raise ValueError("--spatial-shard-size-chunks must be non-negative")
            ecfg.spatial_shard_size_chunks = args.spatial_shard_size_chunks or None
        logger.info(f"Loaded experiment config: {args.experiment_config}")
        zarr_path = asyncio.run(
            _run_configured_experiment(
                ecfg,
                args.inactivity_timeout,
            )
        )
    else:
        # ---------- CLI mode (unknown dimensions, buffered write) ----------
        if not args.experiment:
            logger.error("Either --experiment-config or --experiment must be provided.")
            sys.exit(1)

        compression = args.compression if args.compression != "none" else None

        shard_size = 2 if args.spatial_shard_size_chunks is None else args.spatial_shard_size_chunks or None
        zarr_path = asyncio.run(
            stream_to_omezarr(
                zenapi_config=args.zenapi_config,
                experiment_name=args.experiment,
                output_dir=args.output_dir,
                dtype=np.dtype(args.dtype),
                start_experiment_from_script=bool(args.start_experiment),
                czi_name=args.czi_name,
                channel_index=args.channel_index,
                overwrite_zarr=not args.no_overwrite_zarr,
                compression=compression,
                spatial_shard_size_chunks=shard_size,
                inactivity_timeout=args.inactivity_timeout,
            )
        )
        if args.pyramid_levels:
            generate_omezarr_pyramids(
                zarr_path,
                args.pyramid_levels,
                shard_size,
            )

    logger.info(f"Done. OME-ZARR: {zarr_path}")

    if args.viewer == "ndv":
        open_in_ndv_viewer(zarr_path)
    elif args.viewer == "napari":
        open_in_napari_viewer(zarr_path)


if __name__ == "__main__":
    main()
