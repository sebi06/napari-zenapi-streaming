"""Tests for worker-to-Qt progress reporting."""

from types import SimpleNamespace

import napari.utils

from napari_zen_streaming.ZEN_ui import (
    StreamingViewer,
    _ThrottledProgressEmitter,
)


def test_progress_emitter_coalesces_intermediate_updates() -> None:
    """High-rate progress is limited while boundary events are preserved."""
    emitted: list[tuple[int, int]] = []
    times = iter((0.0, 0.0, 0.02, 0.11, 0.12, 0.12))
    progress = _ThrottledProgressEmitter(
        lambda current, total: emitted.append((current, total)),
        interval=0.1,
        clock=lambda: next(times),
    )

    progress(-1, 10)
    progress(1, 10)
    progress(2, 10)
    progress(3, 10)
    progress(4, 10)
    progress(10, 10)

    assert emitted == [(-1, 10), (1, 10), (3, 10), (10, 10)]


def test_progress_emitter_always_reports_completion() -> None:
    """The completion sentinel bypasses the reporting interval."""
    emitted: list[tuple[int, int]] = []
    progress = _ThrottledProgressEmitter(
        lambda current, total: emitted.append((current, total)),
        interval=10.0,
        clock=lambda: 0.0,
    )

    progress(1, 100)
    progress(2, 100)
    progress(-2, 0)

    assert emitted == [(1, 100), (-2, 0)]


def test_standalone_writer_removes_stale_streaming_progress(
    monkeypatch,
) -> None:
    """Standalone writing owns one bar and removes stale streaming bars."""

    class _Progress:
        _all_instances: set["_Progress"] = set()

        def __init__(self, total=None) -> None:
            self.total = total
            self.n = 0
            self.desc = ""
            self.closed = False
            self._all_instances.add(self)

        def set_description(self, description: str) -> None:
            self.desc = description

        def update(self, delta: int) -> None:
            self.n += delta

        def close(self) -> None:
            self.closed = True
            self._all_instances.discard(self)

    monkeypatch.setattr(napari.utils, "progress", _Progress)

    stale_bar = _Progress(total=64)
    stale_bar.set_description("Streaming frames")
    viewer = object.__new__(StreamingViewer)
    viewer._napari_progress = None
    viewer._napari_progress_bars = set()
    viewer._progress_phase = "idle"
    viewer._standalone_zarr_active = True
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: 2)
    viewer.start_button = SimpleNamespace(isEnabled=lambda: True)

    viewer._update_progress_slot(0, 64)
    assert _Progress._all_instances == {stale_bar}

    viewer._update_progress_slot(-1, 64)
    assert stale_bar.closed
    assert len(_Progress._all_instances) == 1
    writing_bar = next(iter(_Progress._all_instances))
    assert writing_bar.desc == "Writing OME-ZARR"

    viewer._update_progress_slot(59, 64)
    assert writing_bar.n == 59

    viewer._update_progress_slot(-2, 0)
    assert writing_bar.closed
    assert not _Progress._all_instances


def test_targeted_display_keeps_only_one_streaming_bar(monkeypatch) -> None:
    """An orphaned bar and queued updates cannot multiply Display bars."""

    class _Progress:
        _all_instances: set["_Progress"] = set()

        def __init__(self, total=None) -> None:
            self.total = total
            self.n = 0
            self.desc = ""
            self.closed = False
            self._all_instances.add(self)

        def set_description(self, description: str) -> None:
            self.desc = description

        def update(self, delta: int) -> None:
            self.n += delta

        def close(self) -> None:
            self.closed = True
            self._all_instances.discard(self)

    monkeypatch.setattr(napari.utils, "progress", _Progress)
    orphan = _Progress(total=81)
    orphan.set_description("Streaming frames")
    viewer = object.__new__(StreamingViewer)
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: 1)
    viewer.is_streaming = True
    viewer._napari_progress = None
    viewer._napari_progress_bars = set()
    viewer._progress_phase = "idle"
    viewer._standalone_zarr_active = False
    viewer.start_button = SimpleNamespace(isEnabled=lambda: True)

    viewer._update_progress_slot(2, 81)
    assert orphan.closed
    assert len(_Progress._all_instances) == 1
    first_bar = next(iter(_Progress._all_instances))
    viewer._napari_progress = None

    viewer._update_progress_slot(3, 81)
    assert first_bar.closed
    assert len(_Progress._all_instances) == 1
    assert next(iter(_Progress._all_instances)).n == 3

    viewer._update_progress_slot(-2, 0)
    viewer.is_streaming = False
    viewer._update_progress_slot(4, 81)
    assert not _Progress._all_instances
