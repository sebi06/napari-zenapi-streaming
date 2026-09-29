"""Tests for Display-mode live preview behavior."""

import asyncio
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from napari_zen_streaming.ZEN_config import ZENConfig
from napari_zen_streaming.ZEN_ui import StreamingViewer


class _RecordedSignal:
    """Record signal payloads without requiring a Qt event loop."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def emit(self, *args: object) -> None:
        """Record one emitted payload."""
        self.calls.append(args)


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
