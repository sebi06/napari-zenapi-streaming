#################################################################
# File        : zen_omezarr.py
# Author      : SRh
# Institution : Carl Zeiss Microscopy GmbH
#
# Utility functions for the ZEN-API OME-ZARR streaming workflow:
#   - ExperimentConfig dataclass & INI parser
#   - start_experiment() helper (ZEN-API experiment control)
#   - build_progress_bar() for console progress display
#   - open_in_ndv_viewer() / open_in_napari_viewer() for viewing
#   - read_omezarr_axis_names() for OME-ZARR 0.5 metadata
#
# Copyright(c) 2026 Carl Zeiss AG, Germany. All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

import configparser
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import napari

# ZEN API auto-generated stubs
from zen_api.acquisition.v1beta import (
    ExperimentServiceExportRequest,
    ExperimentServiceGetImageOutputPathRequest,
    ExperimentServiceLoadRequest,
    ExperimentServiceStartExperimentRequest,
    ExperimentServiceStub,
)

from napari_zen_streaming.misc import initialize_zenapi

logger = logging.getLogger(__name__)

# Plugin-only OME-ZARR defaults. Change these constants to alter plugin
# behavior that is intentionally not exposed as a UI setting. The standalone
# CLI continues to read the corresponding values from its experiment INI.
PLUGIN_DEFAULT_CZI_NAME = "zenapi_stream"
PLUGIN_DEFAULT_OVERWRITE_CZI = True
PLUGIN_DEFAULT_OVERWRITE_ZARR = True
PLUGIN_DEFAULT_SPATIAL_SHARD_SIZE_CHUNKS = 2
PLUGIN_DEFAULT_PYRAMID_LEVELS = 0


@dataclass
class ExperimentConfig:
    """Parsed experiment configuration."""

    experiment_name: str
    czi_name: str
    start_from_script: bool
    overwrite_czi: bool
    # dimensions
    time_points: int
    channels: int
    z_planes: int
    z_spacing: float
    tiles: int
    scenes: int
    # output
    output_dir: str
    dtype: str
    compression: str | None
    overwrite_zarr: bool
    # stream
    channel_index: int | None
    # zenapi
    zenapi_config: str
    # OME-ZARR layout
    positions: list[dict[str, object]] = field(default_factory=list)
    use_hcs_layout: bool = True
    keep_source_tiles: bool = False
    spatial_shard_size_chunks: int | None = 2
    max_pending_frames: int = 4096
    pyramid_levels: int = 0


def create_plugin_experiment_config(
    experiment_name: str,
    output_dir: str,
    dtype: str,
    compression: str | None,
    zenapi_config: str | Path,
    start_from_script: bool,
    use_hcs_layout: bool,
    keep_source_tiles: bool,
    pyramid_levels: int = PLUGIN_DEFAULT_PYRAMID_LEVELS,
) -> ExperimentConfig:
    """Create the plugin configuration without an experiment INI.

    Acquisition dimensions and positions are placeholders replaced by the
    selected ZEN experiment metadata before streaming starts.

    Args:
        experiment_name: Selected ZEN experiment name.
        output_dir: Destination directory selected in the plugin.
        dtype: Compatibility fallback. The writer uses runtime PixelType.
        compression: OME-ZARR compression selected in the plugin.
        zenapi_config: Main ZEN API gateway configuration path.
        start_from_script: Whether the plugin triggers the experiment.
        use_hcs_layout: Whether explicit wells use an HCS plate layout.
        keep_source_tiles: Keep generic raw tile groups after mosaicking.
        pyramid_levels: Number of post-acquisition 2x Y/X pyramid levels.

    Returns:
        Plugin OME-ZARR configuration with fixed advanced defaults.
    """
    return ExperimentConfig(
        experiment_name=experiment_name,
        czi_name=PLUGIN_DEFAULT_CZI_NAME,
        start_from_script=start_from_script,
        overwrite_czi=PLUGIN_DEFAULT_OVERWRITE_CZI,
        time_points=1,
        channels=1,
        z_planes=1,
        z_spacing=1.0,
        tiles=1,
        scenes=1,
        output_dir=output_dir,
        dtype=dtype,
        compression=compression,
        overwrite_zarr=PLUGIN_DEFAULT_OVERWRITE_ZARR,
        channel_index=None,
        zenapi_config=str(zenapi_config),
        use_hcs_layout=use_hcs_layout,
        keep_source_tiles=keep_source_tiles,
        spatial_shard_size_chunks=(PLUGIN_DEFAULT_SPATIAL_SHARD_SIZE_CHUNKS),
        pyramid_levels=pyramid_levels,
    )


@dataclass(frozen=True)
class ExperimentAcquisitionMetadata:
    """Acquisition dimensions and positions exported from a ZEN experiment."""

    experiment_name: str
    time_points: int
    channels: int
    z_planes: int
    z_spacing: float
    tiles: int
    scenes: int
    positions: tuple[dict[str, object], ...] = ()


def _xml_is_active(element: ET.Element | None, default: bool = True) -> bool:
    """Return whether an XML element is active according to ZEN metadata."""
    if element is None:
        return default
    value = element.get("IsActivated")
    return default if value is None else value.lower() == "true"


def _xml_text(
    element: ET.Element | None,
    path: str,
    default: str | None = None,
) -> str | None:
    """Read non-empty text from a relative XML child path."""
    if element is None:
        return default
    child = element.find(path)
    if child is None or child.text is None or not child.text.strip():
        return default
    return child.text.strip()


def _xml_float(element: ET.Element | None, path: str) -> float | None:
    """Read a floating-point value from a relative XML child path."""
    value = _xml_text(element, path)
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _xml_int(
    element: ET.Element | None,
    path: str,
    default: int = 1,
) -> int:
    """Read an integer value from a relative XML child path."""
    value = _xml_text(element, path)
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _find_active_dimension_setup(
    block: ET.Element,
    setup_name: str,
) -> ET.Element | None:
    """Find an active nested ZEN dimension setup."""

    def find_in(container: ET.Element | None) -> ET.Element | None:
        if container is None:
            return None
        for setup in container:
            if not _xml_is_active(setup, default=False):
                continue
            if setup.tag == setup_name:
                return setup
            nested = setup.find("./SubDimensionSetups")
            match = find_in(nested)
            if match is not None:
                return match
        return None

    return find_in(block.find("./SubDimensionSetups"))


def _parse_well_id(well_id: str) -> tuple[int | None, int | None]:
    """Convert a ZEN well name such as ``B03`` to one-based row/column."""
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", well_id)
    if match is None:
        return None, None
    row = 0
    for letter in match.group(1).upper():
        row = row * 26 + ord(letter) - ord("A") + 1
    return row, int(match.group(2))


def _xml_pair(
    element: ET.Element,
    path: str,
) -> tuple[float, float] | None:
    """Parse a comma-separated XML coordinate pair."""
    value = _xml_text(element, path)
    if value is None:
        return None
    try:
        first, second = value.split(",", maxsplit=1)
        return float(first), float(second)
    except ValueError:
        return None


def _well_id(row: int, column: int) -> str:
    """Return an Excel-style well identifier for one-based indices."""
    letters = ""
    value = row
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return f"{letters}{column}"


def _tile_region_well(
    region: ET.Element,
) -> tuple[int | None, int | None]:
    """Read an explicit well coordinate from one ZEN tile region."""
    template_id = _xml_text(region, "TemplateShapeId", "") or ""
    match = re.fullmatch(r"(\d+)-(\d+)", template_id)
    if match is not None:
        return int(match.group(1)), int(match.group(2))
    return _parse_well_id(region.get("Name", ""))


def _extract_tile_region_positions(
    sample_holder: ET.Element,
) -> tuple[dict[str, object], ...]:
    """Map active tile regions carrying explicit ZEN well identifiers."""
    regions = [
        region
        for region in sample_holder.findall("./TileRegions/TileRegion")
        if _xml_text(region, "IsUsedForAcquisition", "true") == "true"
    ]
    if not regions:
        return ()

    field_counts: dict[tuple[int, int], int] = {}
    positions: list[dict[str, object]] = []
    for scene_index, region in enumerate(regions):
        row, column = _tile_region_well(region)
        center = _xml_pair(region, "CenterPosition")
        if row is None or column is None:
            return ()

        well_key = (row, column)
        field_counts[well_key] = field_counts.get(well_key, 0) + 1
        positions.append(
            {
                "scene_index": scene_index,
                "well_id": _well_id(row, column),
                "well_row": row,
                "well_column": column,
                "field_index": field_counts[well_key],
                "position_name": region.get("Name"),
                "stage_position": {
                    "x": center[0] if center is not None else None,
                    "y": center[1] if center is not None else None,
                    "z": _xml_float(region, "Z"),
                },
            }
        )
    return tuple(positions)


def _extract_xml_positions(block: ET.Element) -> tuple[dict[str, object], ...]:
    """Extract active sample-holder positions for optional HCS metadata."""
    regions_setup = _find_active_dimension_setup(block, "RegionsSetup")
    sample_holder = regions_setup.find("./SampleHolder") if regions_setup is not None else None
    arrays = (
        sample_holder.findall("./SingleTileRegionArrays/SingleTileRegionArray") if sample_holder is not None else []
    )
    positions: list[dict[str, object]] = []
    scene_index = 0
    for well in arrays:
        if not _xml_is_active(well) or _xml_text(well, "IsUsedForAcquisition", "true") != "true":
            continue
        well_id = well.get("Name", "")
        row, column = _parse_well_id(well_id)
        field_index = 0
        for position in well.findall("./SingleTileRegions/SingleTileRegion"):
            if _xml_text(position, "IsUsedForAcquisition", "true") != "true":
                continue
            field_index += 1
            positions.append(
                {
                    "scene_index": scene_index,
                    "well_id": well_id,
                    "well_row": row,
                    "well_column": column,
                    "field_index": field_index,
                    "position_name": position.get("Name"),
                    "stage_position": {
                        "x": _xml_float(position, "X"),
                        "y": _xml_float(position, "Y"),
                        "z": _xml_float(position, "Z"),
                    },
                }
            )
            scene_index += 1
    if positions:
        return tuple(positions)
    if sample_holder is None:
        return ()
    return _extract_tile_region_positions(sample_holder)


def _parse_xml_tile_layout(block: ET.Element) -> tuple[int, int]:
    """Read per-scene M tiles from active ZEN ``TileRegion`` grids."""
    regions_setup = _find_active_dimension_setup(block, "RegionsSetup")
    sample_holder = regions_setup.find("./SampleHolder") if regions_setup is not None else None
    if sample_holder is None:
        return 1, 1

    tile_regions = sample_holder.findall("./TileRegions/TileRegion")
    active_regions = [region for region in tile_regions if _xml_text(region, "IsUsedForAcquisition", "true") == "true"]
    if not active_regions:
        return 1, 1

    tile_counts: list[int] = []
    for region in active_regions:
        columns = max(1, _xml_int(region, "Columns"))
        rows = max(1, _xml_int(region, "Rows"))
        tile_counts.append(columns * rows)
    return max(tile_counts), len(tile_counts)


def parse_experiment_xml(
    xml: str,
    experiment_name: str = "<experiment-export>",
) -> ExperimentAcquisitionMetadata:
    """Parse acquisition dimensions from a ZEN ``.czexp`` XML document."""
    root = ET.fromstring(xml)
    blocks = root.findall("./ExperimentBlocks/AcquisitionBlock")
    active_blocks = [block for block in blocks if _xml_is_active(block)]
    if not active_blocks:
        raise ValueError("The experiment contains no active acquisition blocks.")
    block = active_blocks[0]

    time_setup = _find_active_dimension_setup(block, "TimeSeriesSetup")
    time_points = max(1, _xml_int(time_setup, "Duration/Cycles"))

    channels = sum(
        1
        for track in block.findall(".//MultiTrackSetup/Track")
        if _xml_is_active(track)
        for channel in track.findall("./Channels/Channel")
        if _xml_is_active(channel)
    )

    z_setup = _find_active_dimension_setup(block, "ZStackSetup")
    first = _xml_float(z_setup, "First/Distance/Value")
    last = _xml_float(z_setup, "Last/Distance/Value")
    interval = _xml_float(z_setup, "Interval/Distance/Value")
    if first is not None and last is not None and interval:
        z_planes = max(1, round(abs(last - first) / abs(interval)) + 1)
        z_spacing = abs(interval) * 1e6
    else:
        z_planes = 1
        z_spacing = 1.0

    positions = _extract_xml_positions(block)
    tiles, tile_scenes = _parse_xml_tile_layout(block)
    scenes = max(1, len(positions)) if positions else tile_scenes
    return ExperimentAcquisitionMetadata(
        experiment_name=experiment_name,
        time_points=time_points,
        channels=max(1, channels),
        z_planes=z_planes,
        z_spacing=z_spacing,
        tiles=tiles,
        scenes=scenes,
        positions=positions,
    )


def parse_experiment_file(
    path: str | Path,
) -> ExperimentAcquisitionMetadata:
    """Parse acquisition metadata from a persisted ZEN experiment file."""
    source = Path(path)
    return parse_experiment_xml(
        source.read_text(encoding="utf-8"),
        experiment_name=source.stem,
    )


async def load_experiment_acquisition(
    experiment_service: Any,
    experiment_name: str,
) -> ExperimentAcquisitionMetadata:
    """Load and export a ZEN experiment, then parse its acquisition XML."""
    loaded = await experiment_service.load(ExperimentServiceLoadRequest(experiment_name=experiment_name))
    exported = await experiment_service.export(ExperimentServiceExportRequest(experiment_id=loaded.experiment_id))
    xml = getattr(exported, "xml", None)
    if not isinstance(xml, str) or not xml.strip():
        raise ValueError("ZEN returned an empty experiment XML export.")
    return parse_experiment_xml(xml, experiment_name=experiment_name)


def load_experiment_config(config_path: str | Path) -> ExperimentConfig:
    """Load experiment configuration from an INI file.

    Args:
        config_path: Path to the experiment config INI file.

    Returns:
        Populated ExperimentConfig dataclass.
    """
    config_path = Path(config_path).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Experiment config not found: {config_path}")

    cfg = configparser.ConfigParser()
    cfg.read(config_path)

    # zenapi config: if empty, fall back to config.ini next to the script
    zenapi_config = cfg.get("zenapi", "config", fallback="").strip()
    if not zenapi_config:
        zenapi_config = str(Path(__file__).parent / "config.ini")

    # channel index: empty string means None (all channels)
    ch_idx_raw = cfg.get("stream", "channel_index", fallback="").strip()
    channel_index = int(ch_idx_raw) if ch_idx_raw else None

    # compression: 'none' string -> None
    compression_raw = cfg.get("output", "compression", fallback="blosc-zstd").strip()
    compression = compression_raw if compression_raw != "none" else None

    shard_raw = cfg.get("output", "spatial_shard_size_chunks", fallback="2").strip().lower()
    spatial_shard_size_chunks = None if shard_raw in {"", "0", "none"} else int(shard_raw)
    if spatial_shard_size_chunks is not None and spatial_shard_size_chunks < 1:
        raise ValueError("spatial_shard_size_chunks must be a positive integer, 0, or none")
    max_pending_frames = cfg.getint(
        "stream",
        "max_pending_frames",
        fallback=4096,
    )
    if max_pending_frames < 1:
        raise ValueError("max_pending_frames must be a positive integer")
    pyramid_levels = cfg.getint("output", "pyramid_levels", fallback=0)
    if pyramid_levels < 0:
        raise ValueError("pyramid_levels must be non-negative")

    return ExperimentConfig(
        experiment_name=cfg.get("experiment", "name"),
        czi_name=cfg.get("experiment", "czi_name", fallback="zenapi_stream"),
        start_from_script=cfg.getboolean("experiment", "start_from_script", fallback=False),
        overwrite_czi=cfg.getboolean("experiment", "overwrite_czi", fallback=True),
        time_points=cfg.getint("dimensions", "time_points", fallback=1),
        channels=cfg.getint("dimensions", "channels", fallback=1),
        z_planes=cfg.getint("dimensions", "z_planes", fallback=1),
        z_spacing=cfg.getfloat("dimensions", "z_spacing", fallback=1.0),
        tiles=cfg.getint("dimensions", "tiles", fallback=1),
        scenes=cfg.getint("dimensions", "scenes", fallback=1),
        output_dir=cfg.get("output", "directory", fallback="."),
        dtype=cfg.get("output", "dtype", fallback="uint16"),
        compression=compression,
        overwrite_zarr=cfg.getboolean("output", "overwrite", fallback=True),
        channel_index=channel_index,
        zenapi_config=zenapi_config,
        use_hcs_layout=cfg.getboolean(
            "output",
            "use_hcs_layout",
            fallback=True,
        ),
        keep_source_tiles=cfg.getboolean(
            "output",
            "keep_source_tiles",
            fallback=False,
        ),
        spatial_shard_size_chunks=spatial_shard_size_chunks,
        max_pending_frames=max_pending_frames,
        pyramid_levels=pyramid_levels,
    )


async def start_experiment(
    exp_name: str,
    czi_name: str,
    overwrite: bool = False,
    zenapi_config: str | Path = "config.ini",
) -> tuple[str, Path]:
    """Start a ZEN experiment via the ZEN-API.

    Args:
        exp_name: Experiment name (without .czexp extension).
        czi_name: Desired CZI output name (without .czi extension).
        overwrite: Allow overwriting an existing CZI.
        zenapi_config: Path to the ZEN-API config file.

    Returns:
        Tuple of (experiment_id, czi_path).
    """
    channel, metadata = initialize_zenapi(zenapi_config)
    try:
        exp_service = ExperimentServiceStub(channel=channel, metadata=metadata)
        my_exp = await exp_service.load(ExperimentServiceLoadRequest(experiment_name=exp_name))
        logger.info(f"Loaded experiment: {exp_name} (id={my_exp.experiment_id})")

        save_path = await exp_service.get_image_output_path(ExperimentServiceGetImageOutputPathRequest())
        czi_path = Path(save_path.image_output_path) / f"{czi_name}.czi"
        logger.info(f"CZI save location: {czi_path}")

        if czi_path.exists():
            if overwrite:
                czi_path.unlink()
                logger.info(f"Overwrote existing CZI: {czi_path.name}")
            else:
                raise FileExistsError(f"CZI file already exists: {czi_path}")

        await exp_service.start_experiment(
            ExperimentServiceStartExperimentRequest(
                experiment_id=my_exp.experiment_id,
                output_name=czi_name,
            )
        )
        logger.info("Experiment execution started.")
        return my_exp.experiment_id, czi_path
    finally:
        channel.close()


def build_progress_bar(current: int, total: int | None, width: int = 40) -> str:
    """Render a text progress bar string.

    Args:
        current: Current frame number (1-based).
        total: Total expected frames, or None if unknown.
        width: Character width of the bar.

    Returns:
        Formatted progress bar string.
    """
    if total is not None and total > 0:
        frac = min(current / total, 1.0)
        filled = int(width * frac)
        bar = "█" * filled + "░" * (width - filled)
        return f"\r  [{bar}] {current}/{total} frames ({frac*100:.1f}%)"
    else:
        # unknown total – show a spinner-style counter
        return f"\r  Frames written: {current}"


def open_in_ndv_viewer(zarr_path: Path) -> None:
    """Open an OME-ZARR dataset in the ndv viewer.

    Reads axis names (T, C, Z, Y, X) and per-axis scales from the OME-ZARR
    0.5 metadata stored in ``zarr.json`` and wraps the highest-resolution
    array as a dask-backed ``xr.DataArray`` with physically-spaced
    coordinates so that ndv shows correct proportions (including Z depth)
    and labelled sliders.

    Requires optional dependencies: ``ndv``, ``zarr``, ``dask``, ``xarray``.
    If any are missing the function logs a warning and returns silently.

    Args:
        zarr_path: Path to the ``.ome.zarr`` directory.
    """
    try:
        import dask.array as da
        import ndv
        import numpy as np
        import xarray as xr
        import zarr
    except ImportError as e:
        logger.warning(f"Cannot open viewer (missing dependency: {e}). Install with: pip install ndv zarr dask xarray")
        return

    try:
        logger.info(f"Opening OME-ZARR in ndv viewer: {zarr_path}")
        image_metadata = _read_first_image_metadata(zarr_path)
        if image_metadata is None:
            logger.error("No OME-ZARR multiscale image metadata found.")
            return
        image_path, _ = image_metadata
        zarr_group = zarr.open(str(image_path), mode="r")

        # OME-ZARR stores the image array under the "0" key (highest resolution).
        # zarr.open() returns Array | Group; subscripting a Group with a string
        # key returns a broad union. We narrow to Array here.
        zarr_arr: zarr.Array = zarr_group["0"]  # type: ignore[index,assignment]
        if not hasattr(zarr_arr, "shape"):
            logger.error("Expected a zarr Array at key '0', got: %s", type(zarr_arr).__name__)
            return
        logger.info(f"Array shape: {zarr_arr.shape}, dtype: {zarr_arr.dtype}")

        # Read axis names from OME-ZARR 0.5 metadata (zarr v3: zarr.json).
        # ome-writers 0.3+ stores multiscales under attributes.ome.multiscales
        axis_names = read_omezarr_axis_names(zarr_path)
        axis_scales = read_omezarr_axis_scales(zarr_path)

        if axis_names and len(axis_names) == len(zarr_arr.shape):
            # Wrap zarr v3 array with dask for lazy chunk-aware loading,
            # then label dimensions via xarray for the ndv viewer.
            # Use physical coordinates (index * scale) so that the viewer
            # renders the correct aspect ratio, especially for Z stacks
            # where the z-spacing typically differs from the XY pixel size.
            dask_arr = da.from_array(zarr_arr, chunks=zarr_arr.chunks)  # type: ignore[arg-type]
            coords = {}
            for name, size in zip(axis_names, zarr_arr.shape):
                scale = axis_scales.get(name, 1.0) if axis_scales else 1.0
                coords[name] = np.arange(size, dtype=np.float64) * scale
            data = xr.DataArray(dask_arr, dims=axis_names, coords=coords)
            logger.info(f"Viewer data: shape={data.shape}, dims={data.dims}")
            if axis_scales:
                logger.info(f"Applied axis scales: {axis_scales}")
            ndv.imshow(data)
        else:
            logger.warning("Could not read axis names from OME-ZARR metadata, showing without labels.")
            ndv.imshow(zarr_arr)
    except Exception as e:
        logger.error(f"Failed to open OME-ZARR in ndv viewer: {e}")


def open_in_napari_viewer(
    zarr_path: Path,
    viewer: "napari.Viewer | None" = None,
) -> None:
    """Open an OME-ZARR dataset in the napari viewer.

    Uses the ``napari-ome-zarr`` plugin to read the dataset with full
    OME-ZARR metadata support (channels, scales, axis labels).

    When *viewer* is ``None`` (standalone mode) a new viewer is created
    and ``napari.run()`` is called to start the Qt event loop.  When an
    existing *viewer* is supplied (plugin mode) the dataset is opened
    into that viewer and the caller keeps control of the event loop.

    Requires optional dependencies: ``napari`` and ``napari-ome-zarr``.
    If any are missing the function logs a warning and returns silently.

    Args:
        zarr_path: Path to the ``.ome.zarr`` directory.
        viewer: An existing napari ``Viewer`` instance to reuse.
            If ``None``, a new viewer is created.
    """
    try:
        import napari  # noqa: F811

    except ImportError as e:
        logger.warning(f"Cannot open napari viewer (missing dependency: {e}). Install with: pip install napari[all]")
        return

    try:
        import napari_ome_zarr  # noqa: F401
    except ImportError as e:
        logger.warning(f"Cannot open napari viewer (missing plugin: {e}). Install with: pip install napari-ome-zarr")
        return

    standalone = viewer is None
    try:
        logger.info(f"Opening OME-ZARR in napari viewer: {zarr_path}")
        if viewer is None:
            viewer = napari.Viewer()
        viewer.open(str(zarr_path), plugin="napari-ome-zarr")
        logger.info("napari viewer opened successfully.")

        # Label viewer dimension sliders and layer names with
        # OME-ZARR axis names (e.g. T, C, Z, Y, X).
        axis_names = read_omezarr_axis_names(zarr_path)
        if axis_names:
            viewer.dims.axis_labels = axis_names
            suffix = f" ({','.join(axis_names)})"
            for layer in viewer.layers:
                layer.name = f"{layer.name}{suffix}"

        if standalone:
            napari.run()
    except Exception as e:
        logger.error(
            f"Failed to open OME-ZARR in napari viewer: {e}",
            exc_info=True,
        )
        raise RuntimeError(
            f"Could not display '{zarr_path.name}' in napari. "
            f"Verify the file is a valid OME-ZARR and that "
            f"napari-ome-zarr is up to date."
        ) from e


def read_omezarr_axis_names(zarr_path: Path) -> tuple[str, ...] | None:
    """Read axis names from the first OME-ZARR 0.5 image metadata.

    OME-ZARR 0.5 (zarr v3) stores the multiscales metadata inside
    ``zarr.json → attributes.ome.multiscales``, unlike OME-ZARR 0.4
    which used ``.zattrs``.

    Args:
        zarr_path: Path to the ``.ome.zarr`` directory.

    Returns:
        Tuple of uppercase axis names (e.g. ``('T', 'C', 'Z', 'Y', 'X')``),
        or ``None`` if the metadata cannot be read.
    """
    image_metadata = _read_first_image_metadata(zarr_path)
    if image_metadata is None:
        return None

    _, root_meta = image_metadata

    multiscales = root_meta.get("attributes", {}).get("ome", {}).get("multiscales", [])
    if not isinstance(multiscales, list) or not multiscales:
        return None
    first_multiscale = multiscales[0]
    if not isinstance(first_multiscale, dict):
        return None
    axes = first_multiscale.get("axes", [])
    if not axes:
        return None

    try:
        axis_names = tuple(str(axis["name"]).upper() for axis in axes)
    except (KeyError, TypeError):
        return None
    logger.info(f"OME-ZARR axis names: {axis_names}")
    return axis_names


def read_omezarr_axis_scales(zarr_path: Path) -> dict[str, float] | None:
    """Read scale factors from the first OME-ZARR 0.5 image metadata.

    The scale transform is stored in
    ``zarr.json → attributes.ome.multiscales[0].datasets[0].coordinateTransformations``
    as a list entry with ``{"type": "scale", "scale": [s0, s1, ...]}``.
    The scale values are ordered to match the axes list.

    Args:
        zarr_path: Path to the ``.ome.zarr`` directory.

    Returns:
        Dict mapping uppercase axis name to its scale factor,
        or ``None`` if the metadata cannot be read.
    """
    image_metadata = _read_first_image_metadata(zarr_path)
    if image_metadata is None:
        return None

    _, root_meta = image_metadata

    multiscales = root_meta.get("attributes", {}).get("ome", {}).get("multiscales", [])
    if not isinstance(multiscales, list) or not multiscales:
        return None
    first_multiscale = multiscales[0]
    if not isinstance(first_multiscale, dict):
        return None
    axes = first_multiscale.get("axes", [])
    datasets = first_multiscale.get("datasets", [])
    if not axes or not datasets:
        return None

    # Find the scale transform for the highest-resolution dataset (index 0)
    transforms = datasets[0].get("coordinateTransformations", [])
    scale_values = None
    for t in transforms:
        if t.get("type") == "scale":
            scale_values = t.get("scale")
            break

    if scale_values is None or len(scale_values) != len(axes):
        return None

    try:
        axis_scales = {str(axis["name"]).upper(): float(scale) for axis, scale in zip(axes, scale_values)}
    except (KeyError, TypeError, ValueError):
        return None
    logger.info(f"OME-ZARR axis scales: {axis_scales}")
    return axis_scales


def _read_first_image_metadata(
    zarr_path: Path,
) -> tuple[Path, dict[str, Any]] | None:
    """Return metadata for the root or first descendant multiscale image."""
    root_metadata = zarr_path / "zarr.json"
    candidates = [root_metadata]
    if zarr_path.exists():
        candidates.extend(path for path in sorted(zarr_path.rglob("zarr.json")) if path != root_metadata)

    for metadata_path in candidates:
        try:
            with metadata_path.open(encoding="utf-8") as metadata_file:
                metadata = json.load(metadata_file)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        attributes = metadata.get("attributes")
        if not isinstance(attributes, dict):
            continue
        ome = attributes.get("ome")
        if not isinstance(ome, dict):
            continue
        multiscales = ome.get("multiscales")
        if multiscales:
            return metadata_path.parent, metadata
    return None
