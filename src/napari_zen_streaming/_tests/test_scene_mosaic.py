"""Integration tests for coordinate-based scene mosaic finalization."""

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import zarr

import napari_zen_streaming.ZEN_stream2omezarr as stream_module
from napari_zen_streaming._scene_geometry import (
    TileGeometry,
    calculate_scene_geometry,
)
from napari_zen_streaming.ZEN_omezarr import ExperimentConfig
from napari_zen_streaming.ZEN_stream2omezarr import (
    _assemble_scene_mosaics,
    _assemble_scene_plane,
    _build_position_layout,
    _open_ready_channel_streams,
)


def _experiment_config(*, use_hcs_layout: bool) -> ExperimentConfig:
    """Build a two-well configuration for layout-selection tests."""
    return ExperimentConfig(
        experiment_name="wells",
        czi_name="wells",
        start_from_script=False,
        overwrite_czi=True,
        time_points=1,
        channels=1,
        z_planes=1,
        z_spacing=1.0,
        tiles=2,
        scenes=2,
        output_dir=".",
        dtype="uint16",
        compression=None,
        overwrite_zarr=True,
        channel_index=None,
        zenapi_config="config.ini",
        positions=[
            {
                "scene_index": 0,
                "well_row": 1,
                "well_column": 1,
                "field_index": 1,
            },
            {
                "scene_index": 1,
                "well_row": 2,
                "well_column": 1,
                "field_index": 1,
            },
        ],
        use_hcs_layout=use_hcs_layout,
    )


def test_well_layout_can_be_written_as_hcs() -> None:
    """Valid wells produce a plate when HCS output is selected."""
    positions, plate = _build_position_layout(
        _experiment_config(use_hcs_layout=True),
        num_s=2,
        num_m=2,
    )

    assert plate is not None
    assert len(positions) == 2


def test_channel_subscriptions_wait_for_server_headers() -> None:
    """All channel subscriptions must be acknowledged before auto-start."""
    events = []
    ready = [asyncio.Event(), asyncio.Event()]

    class Stream:
        def __init__(self, index):
            self.index = index

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            events.append(("closed", self.index))

        async def send_message(self, request, end):
            events.append(("sent", request.channel_index, end))

        async def recv_initial_metadata(self):
            await ready[self.index].wait()
            events.append(("ready", self.index))

    class Channel:
        def request(self, _route, _cardinality, _request_type, _response_type, *, metadata):
            assert metadata == {"authorization": "test"}
            return Stream(len([event for event in events if event[0] == "sent"]))

    async def run():
        async with contextlib.AsyncExitStack() as resources:
            opening = asyncio.create_task(
                _open_ready_channel_streams(Channel(), {"authorization": "test"}, [0, 1], 1.0, resources)
            )
            await asyncio.sleep(0)
            assert not opening.done()
            ready[0].set()
            await asyncio.sleep(0)
            assert not opening.done()
            ready[1].set()
            streams = await opening
            assert [index for index, _stream in streams] == [0, 1]

    asyncio.run(run())
    assert events[:2] == [("sent", 0, True), ("sent", 1, True)]
    assert ("ready", 0) in events and ("ready", 1) in events


def test_well_layout_can_be_written_as_generic_scenes() -> None:
    """The same wells remain generic positions when HCS is not selected."""
    positions, plate = _build_position_layout(
        _experiment_config(use_hcs_layout=False),
        num_s=2,
        num_m=2,
    )

    assert plate is None
    assert [position.name for position in positions] == [
        "S0_M0",
        "S0_M1",
        "S1_M0",
        "S1_M1",
    ]


def test_assemble_scene_plane_merges_tiles_by_coordinates() -> None:
    """Direct HCS assembly places all M tiles before writing a plane."""
    geometry = calculate_scene_geometry(
        [
            TileGeometry(0, 0, 0, 1.5, 1.5, 0.0, 1.0, 1.0, 4, 4),
            TileGeometry(0, 1, 1, 4.5, 1.5, 0.0, 1.0, 1.0, 4, 4),
        ]
    )

    mosaic = _assemble_scene_plane(
        {
            0: np.full((4, 4), 2, dtype=np.uint16),
            1: np.full((4, 4), 4, dtype=np.uint16),
        },
        geometry,
        output_height=geometry.height,
        output_width=geometry.width,
    )

    assert mosaic.shape == (4, 7)
    assert np.all(mosaic[:, :3] == 2)
    assert np.all(mosaic[:, 3:] == 4)


@pytest.mark.parametrize("start_from_script", [False, True])
def test_configured_hcs_stream_writes_merged_scene_directly(
    tmp_path: Path,
    monkeypatch,
    start_from_script: bool,
) -> None:
    """HCS streaming writes one merged plane without a tile-store rewrite."""
    frames = []
    for tile_index, value, center_x in ((0, 2, 0.5), (1, 4, 2.5)):
        image = np.full((2, 2), value, dtype=np.uint16)
        frames.append(
            SimpleNamespace(
                frame_data=SimpleNamespace(
                    frame_position=SimpleNamespace(
                        t=0,
                        z=0,
                        m=tile_index,
                        s=0,
                    ),
                    frame_size=SimpleNamespace(width=2, height=2),
                    pixel_data=SimpleNamespace(
                        pixel_type=stream_module.PixelType.GRAY16,
                        raw_data=image.tobytes(),
                    ),
                    scaling=SimpleNamespace(x=1e-6, y=1e-6),
                    frame_stage_position=SimpleNamespace(
                        x=center_x * 1e-6,
                        y=0.5e-6,
                        z=0.0,
                    ),
                )
            )
        )

    class _Channel:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            """Record that the public writer released the gRPC channel."""
            self.closed = True

    class _ExperimentService:
        async def get_status(self, _request):
            """Return dimensions matching the fake stream."""
            return SimpleNamespace(
                status=SimpleNamespace(
                    time_points_count=1,
                    channels_count=1,
                    zstack_slices_count=1,
                    scenes_count=1,
                    tiles_count=2,
                    images_count=2,
                )
            )

    class _StreamingService:
        def monitor_all_experiments(self, _request):
            """Yield both fake tiles through one channel subscription."""

            async def _responses():
                for response in frames:
                    yield response

            return _responses()

    class _OutputStream:
        def __init__(self, settings) -> None:
            self.settings = settings
            self.appended = []
            self.skipped = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def append(self, frame, frame_metadata) -> None:
            self.appended.append((frame.copy(), frame_metadata))

        def skip(self, frames: int) -> None:
            self.skipped += frames

    output_streams = []

    def _create_stream(settings):
        output_stream = _OutputStream(settings)
        output_streams.append(output_stream)
        return output_stream

    channel = _Channel()
    monkeypatch.setattr(
        stream_module,
        "initialize_zenapi",
        lambda _config: (channel, None),
    )
    monkeypatch.setattr(
        stream_module,
        "ExperimentServiceStub",
        lambda **_kwargs: _ExperimentService(),
    )
    monkeypatch.setattr(
        stream_module,
        "ExperimentStreamingServiceStub",
        lambda **_kwargs: _StreamingService(),
    )
    startup_events = []

    async def open_ready_streams(*_args):
        startup_events.append("ready")
        return [(0, _StreamingService().monitor_all_experiments(None))]

    async def start_experiment(**_kwargs):
        assert startup_events == ["ready"]
        startup_events.append("started")
        return "experiment-1", "image.czi"

    monkeypatch.setattr(stream_module, "_open_ready_channel_streams", open_ready_streams)
    monkeypatch.setattr(stream_module, "start_experiment", start_experiment)
    monkeypatch.setattr(stream_module, "create_stream", _create_stream)
    monkeypatch.setattr(
        stream_module,
        "_materialize_hcs_row_groups",
        lambda _path: None,
    )
    monkeypatch.setattr(
        stream_module,
        "_assemble_scene_mosaics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("post-write assembly must not run")),
    )
    pyramid_calls = []

    def _generate_pyramids(path, levels, shard_size) -> None:
        assert channel.closed
        pyramid_calls.append((path, levels, shard_size))

    monkeypatch.setattr(
        stream_module,
        "generate_omezarr_pyramids",
        _generate_pyramids,
    )

    config = _experiment_config(use_hcs_layout=True)
    config.start_from_script = start_from_script
    config.output_dir = str(tmp_path)
    config.scenes = 1
    config.pyramid_levels = 2
    config.positions = [
        {
            "scene_index": 0,
            "well_row": 1,
            "well_column": 1,
            "field_index": 1,
        }
    ]

    zarr_path = asyncio.run(
        stream_module.stream_to_omezarr_with_config(
            config,
            progress_callback=lambda _current, _total: None,
        )
    )

    assert len(output_streams) == 1
    output_stream = output_streams[0]
    assert output_stream.settings.shape == (1, 2, 4)
    assert len(output_stream.appended) == 1
    mosaic, metadata = output_stream.appended[0]
    assert mosaic.tolist() == [[2, 2, 4, 4], [2, 2, 4, 4]]
    assert metadata["position_x"] == 0.0
    assert metadata["position_y"] == 0.0
    assert output_stream.skipped == 0
    assert startup_events == (["ready", "started"] if start_from_script else [])
    assert pyramid_calls == [
        (
            zarr_path,
            2,
            config.spatial_shard_size_chunks,
        )
    ]


def test_find_omezarr_image_paths_discovers_hcs_positions(
    tmp_path: Path,
) -> None:
    """Pyramid discovery finds image groups nested below plate metadata."""
    zarr_path = tmp_path / "plate.zarr"
    root = zarr.open_group(str(zarr_path), mode="w")
    row = root.create_group("A")
    well = row.create_group("1")
    image = well.create_group("0")
    image.attrs["ome"] = {"multiscales": []}

    assert stream_module._find_omezarr_image_paths(zarr_path) == [zarr_path / "A" / "1" / "0"]


def _create_raw_tile(
    root: zarr.Group,
    index: int,
    value: int,
    path: str | None = None,
) -> None:
    """Create one raw TCYX tile with ome-writers-style metadata."""
    group = root.create_group(path or f"raw_{index}")
    group.create_array(
        "0",
        data=np.full((1, 1, 4, 4), value, dtype=np.uint16),
        chunks=(1, 1, 4, 4),
        dimension_names=("t", "c", "y", "x"),
    )
    group.attrs["ome"] = {
        "multiscales": [
            {
                "axes": [
                    {"name": "t", "type": "time"},
                    {"name": "c", "type": "channel"},
                    {
                        "name": "y",
                        "type": "space",
                        "unit": "micrometer",
                    },
                    {
                        "name": "x",
                        "type": "space",
                        "unit": "micrometer",
                    },
                ],
                "datasets": [
                    {
                        "path": "0",
                        "coordinateTransformations": [
                            {
                                "type": "scale",
                                "scale": [1.0, 1.0, 1.0, 1.0],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def test_assemble_scene_mosaic_preserves_raw_tiles(
    tmp_path: Path,
) -> None:
    """Finalize overlap and gaps while retaining raw tile provenance."""
    store_path = tmp_path / "test.zarr"
    root = zarr.open_group(store_path, mode="w")
    ome_group = root.create_group("OME")
    ome_group.attrs["ome"] = {
        "version": "0.5",
        "series": ["raw_0", "raw_1", "raw_2"],
    }
    for index, value in enumerate((2, 4, 8)):
        _create_raw_tile(root, index, value)

    tiles = [
        TileGeometry(0, 0, 0, 1.5, 1.5, 0.0, 1.0, 1.0, 4, 4),
        TileGeometry(0, 1, 1, 4.5, 1.5, 0.0, 1.0, 1.0, 4, 4),
        TileGeometry(0, 2, 2, 1.5, 6.5, 0.0, 1.0, 1.0, 4, 4),
    ]

    _assemble_scene_mosaics(store_path, tiles, keep_source_tiles=True)

    mosaic = root["scene_0"]["0"][:]
    assert mosaic.shape == (1, 1, 9, 7)
    assert np.all(mosaic[0, 0, :4, 3] == 4)
    assert np.all(mosaic[0, 0, 4, :] == 0)
    assert np.all(mosaic[0, 0, 5:, :4] == 8)
    assert root["OME"].attrs["ome"]["series"] == ["scene_0"]
    assert all(f"raw_{index}" in root for index in range(3))
    source_tiles = root["scene_0"].attrs["zen_scene"]["source_tiles"]
    assert source_tiles[1]["stage_position_xyz"] == [4.5, 1.5, 0.0]
    assert root["scene_0"].attrs["zen_scene"]["overlap_policy"] == ("higher_m_index")


def test_assemble_scene_mosaic_removes_raw_tiles_when_disabled(
    tmp_path: Path,
) -> None:
    """Source groups are removed only after their mosaic is complete."""
    store_path = tmp_path / "remove-sources.zarr"
    root = zarr.open_group(store_path, mode="w")
    ome_group = root.create_group("OME")
    ome_group.attrs["ome"] = {
        "version": "0.5",
        "series": ["raw_0", "raw_1"],
    }
    _create_raw_tile(root, 0, 2)
    _create_raw_tile(root, 1, 4)
    tiles = [
        TileGeometry(0, 0, 0, 1.5, 1.5, 0.0, 1.0, 1.0, 4, 4),
        TileGeometry(0, 1, 1, 4.5, 1.5, 0.0, 1.0, 1.0, 4, 4),
    ]

    _assemble_scene_mosaics(
        store_path,
        tiles,
        keep_source_tiles=False,
    )

    assert root["OME"].attrs["ome"]["series"] == ["scene_0"]
    assert root["scene_0"]["0"].shape == (1, 1, 4, 7)
    assert not any(f"raw_{index}" in root for index in range(2))
    assert not root["scene_0"].attrs["zen_scene"]["source_tiles_retained"]


def test_assemble_hcs_scene_mosaic_replaces_tile_images(
    tmp_path: Path,
) -> None:
    """One HCS scene advertises its merged mosaic, not each M tile."""
    store_path = tmp_path / "hcs-scenes.zarr"
    root = zarr.open_group(store_path, mode="w")
    well_group = root.create_group("B/02")
    well_group.attrs["ome"] = {
        "version": "0.5",
        "well": {
            "images": [{"path": "0"}, {"path": "1"}],
        },
    }
    _create_raw_tile(root, 0, 2, path="B/02/0")
    _create_raw_tile(root, 1, 4, path="B/02/1")
    tiles = [
        TileGeometry(0, 0, 0, 1.5, 1.5, 0.0, 1.0, 1.0, 4, 4),
        TileGeometry(0, 1, 1, 4.5, 1.5, 0.0, 1.0, 1.0, 4, 4),
    ]

    _assemble_scene_mosaics(
        store_path,
        tiles,
        hcs_positions=[
            {
                "scene_index": 0,
                "well_row": 2,
                "well_column": 2,
                "field_index": 1,
            }
        ],
    )

    assert root["B/02/scene_0/0"].shape == (1, 1, 4, 7)
    assert root["B/02"].attrs["ome"]["well"]["images"] == [{"path": "scene_0"}]
    assert "0" in root["B/02"]
    assert "1" in root["B/02"]
