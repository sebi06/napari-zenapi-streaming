"""Tests for Display-mode live preview behavior."""

import asyncio
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from qtpy.QtCore import QPoint
from qtpy.QtWidgets import QScrollArea

from napari_zen_streaming.ZEN_config import ZENConfig
from napari_zen_streaming.ZEN_ui import (
    StreamingViewer,
    _resolve_streamed_z_spacing_um,
)


class _RecordedSignal:
    """Record signal payloads without requiring a Qt event loop."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def emit(self, *args: object) -> None:
        """Record one emitted payload."""
        self.calls.append(args)


@pytest.mark.parametrize("mode", [0, 1, 2])
def test_experiment_controls_scroll_when_dock_is_small(
    qtbot, tmp_path: Path, monkeypatch, mode: int
) -> None:
    """All controls remain reachable, and scrollbars hide when content fits."""
    docks = []
    monkeypatch.setattr(
        "napari_zen_streaming.ZEN_ui.napari.current_viewer",
        lambda: SimpleNamespace(
            window=SimpleNamespace(
                add_dock_widget=lambda widget, **kwargs: docks.append(widget)
            )
        ),
    )
    gateway_config = tmp_path / "config.ini"
    gateway_config.write_text("[image_streaming]\n", encoding="ascii")
    viewer = StreamingViewer(
        ZENConfig(exp_name="test", config_file=gateway_config)
    )
    dock = docks[0]
    qtbot.addWidget(dock)
    assert isinstance(dock, QScrollArea)
    assert dock.widget() is viewer.dropdown_widget
    viewer.mode_combo.setCurrentIndex(mode)
    if mode != 2:
        viewer.chk_stream_only.setChecked(True)
        assert viewer.stream_only_enabled
        assert not viewer.chk_live_latest.isEnabled()
        viewer._freeze_live_preview_option()
        assert not viewer.chk_stream_only.isEnabled()
        viewer._release_live_preview_option()
        assert viewer.chk_stream_only.isEnabled()
        assert not viewer.chk_live_latest.isEnabled()
    dock.resize(1000, 120)
    dock.show()

    scrollbar = dock.verticalScrollBar()
    qtbot.waitUntil(lambda: scrollbar.maximum() > 0)
    scrollbar.setValue(scrollbar.maximum())
    last_control = (
        viewer.omezarr_options_panel if mode == 2 else viewer.chk_live_latest
    )
    bottom = last_control.mapTo(
        dock.viewport(), QPoint(0, last_control.height())
    ).y()
    assert 0 < bottom <= dock.viewport().height()

    dock.resize(1000, viewer.dropdown_widget.minimumSizeHint().height() + 100)
    qtbot.waitUntil(lambda: not scrollbar.isVisible())
    assert scrollbar.maximum() == 0


@pytest.mark.parametrize("mode", [0, 1, 2])
def test_worker_mode_checks_do_not_read_qt_widgets(mode: int) -> None:
    """Mode properties use cached state rather than a worker-side Qt call."""
    viewer = StreamingViewer.__new__(StreamingViewer)
    viewer._mode_index = mode
    assert viewer.display_enabled == (mode != 2)
    assert viewer.watch_enabled == (mode == 0)
    assert viewer.omezarr_enabled == (mode == 2)


def _metadata(frame_index: int) -> SimpleNamespace:
    """Create metadata with one unique Z index per synthetic frame."""
    return SimpleNamespace(
        frame_s=0,
        frame_t=0,
        frame_m=0,
        frame_z=frame_index,
        frame_c=0,
        scaling_y_um=0.5,
        scaling_x_um=0.5,
    )


def _viewer_for_buffer_test(latest_only: bool) -> StreamingViewer:
    """Build the minimal viewer state needed by ``_process_buffer``."""
    viewer = object.__new__(StreamingViewer)
    images = [np.full((2, 3), value, dtype=np.uint8) for value in range(3)]
    viewer.image_buffer = deque(images)
    viewer.metadata_buffer = deque(_metadata(index) for index in range(3))
    viewer.frame_data = {}
    viewer.frame_metadata_by_key = {}
    viewer.frame_keys = []
    viewer.unique_channels = set()
    viewer.image_shape = None
    viewer.scaling_y_um = 1.0
    viewer.scaling_x_um = 1.0
    viewer.is_processing = True
    viewer._run_live_latest = latest_only
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: 0)
    viewer._mode_index = 0
    viewer.signals = SimpleNamespace(
        create_layer=_RecordedSignal(),
        update_layer=_RecordedSignal(),
    )
    return viewer


def test_latest_preview_default_can_be_disabled_from_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The persisted preview default is read from the environment."""
    gateway_config = tmp_path / "config.ini"
    gateway_config.write_text("[image_streaming]\n", encoding="ascii")
    monkeypatch.setenv("ZEN_CONFIG_FILE", str(gateway_config))
    monkeypatch.setenv(
        "ZEN_SHOW_ONLY_LAST_FRAME_DURING_LIVE",
        "false",
    )

    config = ZENConfig.from_env()

    assert not config.show_only_last_frame_during_live


def test_latest_preview_stores_all_frames_without_dense_history() -> None:
    """Latest-only preview retains all frames and emits the newest image."""
    viewer = _viewer_for_buffer_test(latest_only=True)

    def reject_dense_history() -> np.ndarray:
        raise AssertionError("latest-only preview built dense history")

    viewer._build_dense_array = reject_dense_history

    asyncio.run(viewer._process_buffer())

    assert len(viewer.frame_data) == 3
    assert set(viewer.frame_data) == {
        (0, 0, 0, 0, 0),
        (0, 0, 0, 1, 0),
        (0, 0, 0, 2, 0),
    }
    assert len(viewer.signals.create_layer.calls) == 1
    assert len(viewer.signals.update_layer.calls) == 1
    preview_image = viewer.signals.update_layer.calls[0][0]
    np.testing.assert_array_equal(preview_image, np.full((2, 3), 2))


@pytest.mark.parametrize("channel", [None, 1])
def test_stream_only_does_not_store_or_assemble_frames(channel) -> None:
    """Stream-only renders native pixels and never builds acquisition history."""
    viewer = _viewer_for_buffer_test(latest_only=False)
    viewer._run_stream_only = True
    viewer._stream_preview_created = False
    viewer._stream_frames_received = 0
    viewer._effective_channel_index = channel
    viewer.processing_lock = asyncio.Lock()
    viewer.signals.restructure_complete = _RecordedSignal()
    viewer.signals.update_progress = _RecordedSignal()
    viewer.is_streaming = True
    viewer.image_buffer.clear()
    viewer.metadata_buffer.clear()

    def reject_history():
        raise AssertionError("stream-only built acquisition history")

    viewer._build_dense_array = reject_history

    async def stream() -> None:
        for index in range(100):
            image = np.full((2, 3), 3000 + index, dtype=np.uint16)
            metadata = _metadata(index)
            metadata.frame_c = index % 2
            viewer.image_buffer.append(image)
            viewer.metadata_buffer.append(metadata)
            await viewer._process_buffer()
            assert not viewer.frame_data
            assert not viewer.frame_metadata_by_key
            assert not viewer.frame_keys
            assert not viewer.image_buffer
        # Completion must flush the last frame without invoking mosaic assembly.
        viewer.image_buffer.append(np.full((2, 3), 4100, dtype=np.uint16))
        metadata = _metadata(100)
        metadata.frame_c = 1
        viewer.metadata_buffer.append(metadata)
        await viewer.perform_restructure()

    asyncio.run(stream())

    assert len(viewer.signals.create_layer.calls) == 1
    assert len(viewer.signals.update_layer.calls) == (
        100 if channel is None else 50
    )
    image, metadata = viewer.signals.update_layer.calls[-1]
    assert image.dtype == np.dtype(np.uint16)
    np.testing.assert_array_equal(image, np.full((2, 3), 4100))
    assert metadata[0].frame_c == 1
    assert not viewer.signals.restructure_complete.calls
    assert not viewer.is_streaming
    assert not viewer.frame_data
    assert viewer.signals.update_progress.calls == [(-2, 0)]


def test_stream_only_filters_queued_frames_before_rendering() -> None:
    """Changing channel ignores old-channel frames already in the UI buffer."""
    viewer = _viewer_for_buffer_test(latest_only=True)
    viewer._run_stream_only = True
    viewer._stream_preview_created = False
    viewer._effective_channel_index = 1
    # Existing buffered frames are channel zero.
    asyncio.run(viewer._process_buffer())

    assert not viewer.signals.create_layer.calls
    assert not viewer.signals.update_layer.calls
    assert not viewer.frame_data


def test_stream_only_add_frame_filters_and_finishes_without_history() -> None:
    """The actual receive path stays bounded and completes from inactivity."""
    viewer = _viewer_for_buffer_test(latest_only=True)
    viewer.image_buffer.clear()
    viewer.metadata_buffer.clear()
    viewer._run_stream_only = True
    viewer._stream_preview_created = False
    viewer._stream_frames_received = 0
    viewer._effective_channel_index = 1
    viewer._standalone_zarr_active = False
    viewer._zarr_stream = None
    viewer._zarr_frame_count = 0
    viewer.image_layer = None
    viewer.current_experiment_id = None
    viewer.is_streaming = True
    viewer.is_processing = False
    viewer.restructure_task = None
    viewer.processing_lock = asyncio.Lock()
    viewer.config = SimpleNamespace(restructure_timeout=0.01)
    viewer.signals.experiment_finished = _RecordedSignal()
    viewer.signals.restructure_complete = _RecordedSignal()
    viewer.signals.update_progress = _RecordedSignal()

    async def stream() -> None:
        for index in range(10):
            metadata = _metadata(index)
            metadata.frame_c = index % 2
            await viewer.add_frame(
                np.full((2, 3), 3000 + index, dtype=np.uint16),
                metadata,
            )
            await asyncio.sleep(0.005)
            assert not viewer.frame_data
            assert not viewer.frame_metadata_by_key
        await asyncio.wait_for(viewer.restructure_task, timeout=2.0)

    asyncio.run(stream())

    assert viewer._stream_frames_received == 5
    assert len(viewer.signals.create_layer.calls) == 1
    assert len(viewer.signals.update_layer.calls) == 4
    image, _metadata_list = viewer.signals.update_layer.calls[-1]
    np.testing.assert_array_equal(image, np.full((2, 3), 3009))
    assert not viewer.signals.restructure_complete.calls
    assert viewer.signals.experiment_finished.calls == [()]
    assert not viewer.is_streaming


def test_stream_only_option_forces_latest_preview_and_is_frozen() -> None:
    """No-history viewing overrides full history until the acquisition ends."""
    viewer = object.__new__(StreamingViewer)
    viewer._live_latest_preference = False
    viewer._run_live_latest = None
    viewer._stream_only_preference = True
    viewer._run_stream_only = None
    viewer.signals = SimpleNamespace(
        set_live_preview_option_enabled=_RecordedSignal()
    )
    viewer.chk_live_latest = SimpleNamespace(setEnabled=lambda value: None)
    viewer.chk_stream_only = SimpleNamespace(setEnabled=lambda value: None)

    viewer._freeze_live_preview_option()
    viewer._set_stream_only_preference(False)

    assert viewer.stream_only_enabled
    assert viewer._run_live_latest is True
    viewer._release_live_preview_option()
    assert not viewer.stream_only_enabled
    assert viewer._run_live_latest is None


def test_clear_layers_preserves_unrelated_user_data() -> None:
    """Starting again removes plugin layers, not user images or annotations."""
    viewer = object.__new__(StreamingViewer)
    unrelated, streaming, previous_result = object(), object(), object()
    viewer.viewer = SimpleNamespace(
        layers=[unrelated, streaming, previous_result]
    )
    viewer.image_layer = streaming
    viewer.image_layers = {}
    viewer._owned_layers = [streaming, previous_result]
    viewer._napari_progress = None

    viewer._clear_layers()
    viewer._clear_layers()

    assert viewer.viewer.layers == [unrelated]
    assert viewer.image_layer is None
    assert not viewer.image_layers
    assert not viewer._owned_layers


def test_targeted_display_reports_received_frame_progress() -> None:
    """Selected experiments report unique received frames against XML dimensions."""
    viewer = _viewer_for_buffer_test(latest_only=True)
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: 1)
    viewer._mode_index = 1
    viewer._selected_experiment_metadata = SimpleNamespace(
        time_points=1, channels=1, z_planes=3, tiles=1, scenes=1
    )
    viewer._effective_channel_index = None
    viewer.signals.update_progress = _RecordedSignal()

    asyncio.run(viewer._process_buffer())

    assert viewer.signals.update_progress.calls == [(1, 3), (3, 3)]


def test_full_history_preview_still_builds_dense_array() -> None:
    """Unchecked preview retains the existing dense-history path."""
    viewer = _viewer_for_buffer_test(latest_only=False)
    dense_history = np.zeros((3, 1, 1, 1, 1, 2, 3), dtype=np.uint8)
    build_calls = 0

    def build_dense_history() -> np.ndarray:
        nonlocal build_calls
        build_calls += 1
        return dense_history

    viewer._build_dense_array = build_dense_history

    asyncio.run(viewer._process_buffer())

    assert len(viewer.frame_data) == 3
    assert build_calls == 1
    emitted_history = viewer.signals.update_layer.calls[0][0]
    assert emitted_history is dense_history


@pytest.mark.parametrize("latest_only", [False, True])
def test_live_updates_preserve_pixels_and_user_contrast(
    latest_only: bool,
) -> None:
    """New tiles must not rewrite native values or reset display contrast."""
    viewer = object.__new__(StreamingViewer)
    viewer._run_live_latest = latest_only
    image = np.array([[100, 110, 120], [130, 140, 3100]], dtype=np.uint16)
    data = image if latest_only else image.reshape((1, 1, 1, 1, 1, 2, 3))
    viewer.image_layer = SimpleNamespace(data=None, contrast_limits=(50, 4000))
    viewer.viewer = SimpleNamespace(
        dims=SimpleNamespace(
            axis_labels=(), set_current_step=lambda axis, step: None
        )
    )

    viewer._update_layer_slot(data, [_metadata(0)])

    assert viewer.image_layer.data is data
    assert viewer.image_layer.data.dtype == np.dtype(np.uint16)
    assert viewer.image_layer.contrast_limits == (50, 4000)


def test_preview_option_is_frozen_for_one_acquisition() -> None:
    """Checkbox changes cannot alter a run after its option is frozen."""
    viewer = object.__new__(StreamingViewer)
    option_signal = _RecordedSignal()
    enabled_states: list[bool] = []
    viewer._live_latest_preference = True
    viewer._run_live_latest = None
    viewer.signals = SimpleNamespace(
        set_live_preview_option_enabled=option_signal,
    )
    viewer.chk_live_latest = SimpleNamespace(
        setEnabled=enabled_states.append,
    )

    viewer._freeze_live_preview_option()
    viewer._set_live_latest_preference(False)
    viewer._freeze_live_preview_option()

    assert viewer._run_live_latest is True
    assert option_signal.calls == [(False,)]

    viewer._release_live_preview_option()

    assert viewer._run_live_latest is None
    assert enabled_states == [True]


def test_post_restructure_straggler_does_not_freeze_option() -> None:
    """A discarded late frame cannot lock the option after completion."""
    viewer = object.__new__(StreamingViewer)
    option_signal = _RecordedSignal()
    viewer.image_buffer = deque(maxlen=10)
    viewer.metadata_buffer = deque(maxlen=10)
    viewer.last_frame_time = 0.0
    viewer._standalone_zarr_active = False
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: 0)
    viewer._mode_index = 0
    viewer.is_streaming = False
    viewer._restructure_completed_at = time.time()
    viewer._live_latest_preference = True
    viewer._run_live_latest = None
    viewer.signals = SimpleNamespace(
        set_live_preview_option_enabled=option_signal,
    )

    asyncio.run(
        viewer.add_frame(
            np.zeros((2, 3), dtype=np.uint8),
            _metadata(0),
        )
    )

    assert not viewer.image_buffer
    assert viewer._run_live_latest is None
    assert not option_signal.calls


def test_streamed_z_positions_override_stale_xml_spacing() -> None:
    """ZEN-started stacks use physical spacing from their streamed frames."""
    metadata_by_key = {}
    for tile, z_origin in ((0, 12.0), (1, 40.0)):
        for z_index in range(81):
            key = (0, 0, tile, z_index, 0)
            metadata_by_key[key] = SimpleNamespace(
                stage_z_um=z_origin + z_index * 0.27,
            )

    spacing = _resolve_streamed_z_spacing_um(
        metadata_by_key,
        fallback_um=1.0,
    )

    assert spacing == pytest.approx(0.27)


@pytest.mark.parametrize("mode, expected_spacing", [(0, 1.0), (1, 7.0)])
def test_display_z_fallback_uses_selected_xml_only_for_targeted_mode(
    mode: int, expected_spacing: float
) -> None:
    """Passive ZEN watching must not borrow a selected setup's Z spacing."""
    viewer = object.__new__(StreamingViewer)
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: mode)
    viewer._mode_index = mode
    viewer.image_layers = {}
    viewer.image_layer = None
    viewer.unique_channels = {0, 1}
    viewer.frame_metadata_by_key = {}
    viewer._z_spacing_um = 7.0
    viewer.scaling_y_um = 0.5
    viewer.scaling_x_um = 0.5
    viewer.signals = SimpleNamespace(experiment_finished=_RecordedSignal())
    layer_options: list[dict[str, object]] = []
    viewer.viewer = SimpleNamespace(
        layers=[],
        add_image=lambda data, **kwargs: layer_options.append(kwargs),
        dims=SimpleNamespace(
            axis_labels=(), ndim=5, set_current_step=lambda axis, step: None
        ),
    )
    image = np.array([[100, 110, 120], [130, 140, 3100]], dtype=np.uint16)
    viewer._restructure_complete_slot(
        {
            0: {"data": image, "name": "Channel 1"},
            1: {"data": image, "name": "Channel 2"},
        }
    )

    assert len(layer_options) == 2
    assert all(
        options["scale"][2] == expected_spacing for options in layer_options
    )
    assert all(options["blending"] == "additive" for options in layer_options)
    assert all(
        options["contrast_limits"] == (100, 3100) for options in layer_options
    )
