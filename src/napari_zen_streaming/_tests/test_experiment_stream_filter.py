"""Tests for experiment-isolated display streaming."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from qtpy.QtCore import QCoreApplication, QEvent, Qt
from qtpy.QtWidgets import QDockWidget, QMainWindow

from napari_zen_streaming.main import ZENApplication
from napari_zen_streaming.ZEN_config import ZENConfig
from napari_zen_streaming.ZEN_pipeline import StreamingPipeline
from napari_zen_streaming.ZEN_ui import StreamingViewer


def _response(experiment_id: str) -> SimpleNamespace:
    """Create a minimal streamed-frame response."""
    return SimpleNamespace(
        frame_data=SimpleNamespace(experiment_id=experiment_id),
    )


def test_pipeline_filters_only_when_target_is_armed() -> None:
    """ZEN-started mode accepts all frames; napari-started mode is isolated."""
    pipeline = object.__new__(StreamingPipeline)
    pipeline._target_experiment_id = None
    pipeline.stop_event = asyncio.Event()
    pipeline.viewer = SimpleNamespace(
        closed=Event(), watch_enabled=True, _standalone_zarr_active=False
    )

    assert pipeline._accepts_response(_response("zen-started"))

    pipeline.viewer.watch_enabled = False
    assert not pipeline._accepts_response(_response("zen-started"))
    pipeline.set_target_experiment("napari-started")

    assert pipeline._accepts_response(_response("napari-started"))
    assert not pipeline._accepts_response(_response("other-experiment"))


def test_target_is_armed_before_loaded_experiment_starts() -> None:
    """The global reader is ready and filtered before acquisition begins."""
    events: list[str] = []
    pipeline = object.__new__(StreamingPipeline)
    pipeline._target_experiment_id = None

    async def ensure_reader_ready() -> None:
        assert pipeline._target_experiment_id == "experiment-42"
        events.append("reader-ready")

    class Connection:
        async def prepare_experiment(
            self,
            experiment_name: str,
            overwrite: bool = True,
        ) -> str:
            events.append(f"prepared:{experiment_name}:{overwrite}")
            return "experiment-42"

        async def start_loaded_experiment(
            self,
            experiment_id: str,
            output_name: str,
        ) -> None:
            events.append(
                f"started:{experiment_id}:{output_name}:"
                f"{pipeline._target_experiment_id}"
            )

    pipeline.connection = Connection()
    pipeline.ensure_reader_ready = ensure_reader_ready

    experiment_id = asyncio.run(pipeline.start_targeted_experiment("Z Stack"))

    assert experiment_id == "experiment-42"
    assert events == [
        "prepared:Z Stack:True",
        "reader-ready",
        "started:experiment-42:Z Stack:experiment-42",
    ]


def test_failed_start_clears_experiment_filter() -> None:
    """A failed acquisition start must disarm and close its pixel reader."""
    pipeline = object.__new__(StreamingPipeline)
    pipeline._target_experiment_id = None

    async def ensure_reader_ready() -> None:
        return None

    class Connection:
        async def prepare_experiment(
            self,
            experiment_name: str,
            overwrite: bool = True,
        ) -> str:
            return "experiment-42"

        async def start_loaded_experiment(
            self,
            experiment_id: str,
            output_name: str,
        ) -> None:
            raise RuntimeError("start failed")

    pipeline.connection = Connection()
    pipeline.ensure_reader_ready = ensure_reader_ready
    pipeline.suspend_reader = AsyncMock()

    with pytest.raises(RuntimeError, match="start failed"):
        asyncio.run(pipeline.start_targeted_experiment("Z Stack"))

    assert pipeline._target_experiment_id is None
    pipeline.suspend_reader.assert_awaited_once()


def test_reader_failure_does_not_leave_start_waiting(streaming_app) -> None:
    """A failed subscription aborts startup instead of waiting for readiness."""
    app, _dock, _window, _streams = streaming_app
    app.connection.streaming_service.monitor_all_experiments.side_effect = (
        RuntimeError("subscription failed")
    )

    async def run() -> None:
        await app.pipeline.start()
        try:
            with pytest.raises(RuntimeError, match="subscription failed"):
                await asyncio.wait_for(
                    app.pipeline.start_targeted_experiment("test"),
                    timeout=0.5,
                )
            app.connection.start_loaded_experiment.assert_not_awaited()
            assert app.pipeline._target_experiment_id is None
            assert app.pipeline.reader_task is None
        finally:
            await app.pipeline.stop()
            await app.viewer.shutdown()

    asyncio.run(run())


def test_stream_close_failure_still_stops_processor(
    streaming_app, monkeypatch
) -> None:
    """Reader-close failures remain visible while accepted frames are drained."""
    app, _dock, _window, streams = streaming_app
    app.viewer.add_frame = AsyncMock()
    monkeypatch.setattr(
        "napari_zen_streaming.ZEN_pipeline.process_frame",
        lambda response: (response, None),
    )

    async def run() -> None:
        await app.pipeline.start()
        try:
            await app.pipeline.start_targeted_experiment("test")
            app.pipeline.queue.put_nowait(_response("experiment-42"))
            app.pipeline.queue.put_nowait(_response("experiment-42"))
            streams[0].aclose = AsyncMock(
                side_effect=RuntimeError("stream close failed")
            )
            with pytest.raises(RuntimeError, match="stream close failed"):
                await asyncio.wait_for(app.pipeline.stop(), timeout=0.5)
            assert app.pipeline.processor_task.done()
            assert app.pipeline.reader_task is None
            assert app.pipeline.frames_processed == 2
            assert app.pipeline.queue.empty()
            assert app.viewer.add_frame.await_count == 2
            streams[0].aclose.assert_awaited_once()
        finally:
            await app.pipeline.stop()
            await app.viewer.shutdown()

    asyncio.run(run())


def test_exhausted_reader_cannot_start_acquisition(streaming_app) -> None:
    """An empty global stream fails startup rather than leaving it waiting."""
    app, _dock, _window, _streams = streaming_app

    class EmptyStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self) -> None:
            return None

    app.connection.streaming_service.monitor_all_experiments.side_effect = (
        lambda _request: EmptyStream()
    )

    async def run() -> None:
        await app.pipeline.start()
        try:
            with pytest.raises(RuntimeError, match="Pixel stream closed"):
                await asyncio.wait_for(
                    app.pipeline.start_targeted_experiment("test"),
                    timeout=0.5,
                )
            app.connection.start_loaded_experiment.assert_not_awaited()
            assert app.pipeline.reader_task is None
        finally:
            await app.pipeline.stop()
            await app.viewer.shutdown()

    asyncio.run(run())


def test_stop_reports_completed_reader_failure(streaming_app) -> None:
    """Shutdown retrieves errors even when the reader has already exited."""
    app, _dock, _window, _streams = streaming_app
    app.viewer._mode_index = 0
    app.connection.streaming_service.monitor_all_experiments.side_effect = (
        RuntimeError("subscription failed")
    )

    async def run() -> None:
        await app.pipeline.start()
        try:
            await _wait_until(app.pipeline.reader_task.done)
            with pytest.raises(RuntimeError, match="subscription failed"):
                await asyncio.wait_for(app.pipeline.stop(), timeout=0.5)
            assert app.pipeline.processor_task.done()
            assert app.pipeline.reader_task is None
        finally:
            await app.pipeline.stop()
            await app.viewer.shutdown()

    asyncio.run(run())


def test_stop_drains_queued_frames(monkeypatch) -> None:
    """Shutdown finishes processing frames already accepted by the reader."""
    processed = []
    pipeline = object.__new__(StreamingPipeline)

    class Viewer:
        async def add_frame(self, image, metadata) -> None:
            processed.append(image)

    monkeypatch.setattr(
        "napari_zen_streaming.ZEN_pipeline.process_frame",
        lambda response, *_args: (response, None),
    )
    pipeline.viewer = Viewer()
    pipeline.frames_received = 2
    pipeline.frames_processed = 0
    pipeline.reader_task = None
    pipeline._reader_iter = None
    pipeline._reader_lock = asyncio.Lock()
    pipeline.stop_event = asyncio.Event()
    pipeline.queue = asyncio.Queue()
    pipeline.queue.put_nowait("first")
    pipeline.queue.put_nowait("second")

    async def run() -> None:
        pipeline.processor_task = asyncio.create_task(
            pipeline._process_frames()
        )
        try:
            await asyncio.wait_for(pipeline.stop(), timeout=0.5)
        finally:
            pipeline.processor_task.cancel()
            await asyncio.gather(
                pipeline.processor_task, return_exceptions=True
            )

    asyncio.run(run())
    assert processed == ["first", "second"]


@pytest.fixture
def streaming_app(qtbot, tmp_path, monkeypatch):
    """Build the real UI and pipeline with a local, cancellable pixel stream."""
    window = QMainWindow()
    qtbot.addWidget(window)
    docks = []

    def add_dock(widget, **kwargs):
        dock = QDockWidget(kwargs["name"], window)
        dock.setWidget(widget)
        window.addDockWidget(Qt.RightDockWidgetArea, dock)
        docks.append(dock)
        return dock

    napari_viewer = SimpleNamespace(
        window=SimpleNamespace(_qt_window=window, add_dock_widget=add_dock)
    )
    monkeypatch.setattr(
        "napari_zen_streaming.ZEN_ui.napari.current_viewer",
        lambda: napari_viewer,
    )
    config_file = tmp_path / "config.ini"
    config_file.write_text("[image_streaming]\n", encoding="ascii")
    config = ZENConfig(
        exp_name="test", exp_started_by_ui=True, config_file=config_file
    )
    viewer = StreamingViewer(config)
    streams = []

    class Stream:
        def __init__(self):
            self.closed = False
            self.frames = asyncio.Queue()

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.frames.get()

        async def aclose(self):
            self.closed = True

    def subscribe(request):
        stream = Stream()
        streams.append(stream)
        return stream

    connection = SimpleNamespace(
        streaming_service=SimpleNamespace(
            monitor_all_experiments=Mock(side_effect=subscribe)
        ),
        prepare_experiment=AsyncMock(return_value="experiment-42"),
        start_loaded_experiment=AsyncMock(),
        close=Mock(),
    )
    pipeline = StreamingPipeline(
        connection,
        SimpleNamespace(is_monitoring_mode=False, experiment_id=None),
        viewer,
        config,
    )
    app = ZENApplication.__new__(ZENApplication)
    app.viewer = viewer
    app.pipeline = pipeline
    app.connection = connection
    app.embedded = True
    viewer.app = app
    window.show()
    return app, docks[0], window, streams


async def _wait_until(predicate) -> None:
    """Wait for a worker-loop transition with a bounded failure time."""
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("mode", [0, 1, 2])
def test_only_watch_mode_subscribes_on_startup(streaming_app, mode) -> None:
    """Selected Display and OME-ZARR modes remain unsubscribed until Start."""
    app, _dock, _window, streams = streaming_app
    app.viewer._mode_index = mode

    async def run():
        try:
            await app.pipeline.start()
            await asyncio.sleep(0)
            assert len(streams) == int(mode == 0)
        finally:
            await app.pipeline.stop()
            await app.viewer.shutdown()

    asyncio.run(run())


def test_mode_and_channel_changes_preserve_idle_state(streaming_app) -> None:
    """Changing filters cannot accidentally arm a start-selected reader."""
    app, _dock, _window, streams = streaming_app
    viewer = app.viewer

    async def run():
        await app.pipeline.start()
        try:
            viewer._on_channel_filter_changed(0)
            await asyncio.sleep(0.02)
            assert not streams

            viewer.mode_combo.setCurrentIndex(0)
            await _wait_until(lambda: len(streams) == 1)

            viewer.mode_combo.setCurrentIndex(1)
            await _wait_until(lambda: streams[0].closed)
            viewer._on_channel_filter_changed(0)
            await asyncio.sleep(0.02)
            assert len(streams) == 1

            viewer.mode_combo.setCurrentIndex(0)
            await _wait_until(lambda: len(streams) == 2)
            viewer.mode_combo.setCurrentIndex(2)
            await _wait_until(lambda: streams[1].closed)
            await app.pipeline.resume_reader()
            assert len(streams) == 2
        finally:
            await app.pipeline.stop()
            await viewer.shutdown()

    asyncio.run(run())


def test_selected_run_filters_before_subscribing_and_returns_to_idle(
    streaming_app, monkeypatch
) -> None:
    """Explicit runs keep trailing pixels, then disconnect until next Start."""
    app, _dock, _window, streams = streaming_app
    pipeline = app.pipeline
    received = []
    app.viewer.add_frame = AsyncMock(
        side_effect=lambda *args: received.append(args)
    )
    monkeypatch.setattr(
        "napari_zen_streaming.ZEN_pipeline.process_frame",
        lambda response: (response.frame_data.experiment_id, None),
    )

    async def start(experiment_id, output_name):
        assert pipeline._target_experiment_id == experiment_id
        assert pipeline._reader_ready.is_set()

    app.connection.start_loaded_experiment.side_effect = start

    async def run():
        await pipeline.start()
        try:
            await pipeline.start_targeted_experiment("test")
            streams[0].frames.put_nowait(_response("other-experiment"))
            streams[0].frames.put_nowait(_response("experiment-42"))
            await _wait_until(lambda: pipeline.frames_processed == 1)
            assert received == [("experiment-42", None)]
            assert not streams[0].closed

            app.viewer._experiment_finished_slot()
            await _wait_until(lambda: streams[0].closed)
            assert not pipeline._accepts_response(_response("experiment-42"))
            await pipeline.start_targeted_experiment("test")
            assert len(streams) == 2
        finally:
            await pipeline.stop()
            await app.viewer.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["prepare", "start"])
def test_selected_start_failure_leaves_no_subscription(
    streaming_app, failure
) -> None:
    """Preparation and acquisition errors both leave selected mode idle."""
    app, _dock, _window, streams = streaming_app
    failing_method = (
        app.connection.prepare_experiment
        if failure == "prepare"
        else app.connection.start_loaded_experiment
    )
    failing_method.side_effect = RuntimeError("ZEN failed")

    async def run():
        await app.pipeline.start()
        try:
            with pytest.raises(RuntimeError, match="ZEN failed"):
                await app.pipeline.start_targeted_experiment("test")
            assert app.pipeline._target_experiment_id is None
            assert all(stream.closed for stream in streams)
            assert len(streams) == int(failure == "start")
        finally:
            await app.pipeline.stop()
            await app.viewer.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("mode", [0, 1, 2])
@pytest.mark.parametrize("remove", [False, True])
def test_dock_close_stops_owned_streams_without_closing_napari(
    streaming_app, mode, remove
) -> None:
    """Closing or deleting the dock closes Display and dedicated readers."""
    app, dock, window, streams = streaming_app
    app.viewer._mode_index = mode
    writer_started = asyncio.Event()
    writer_closed = asyncio.Event()

    async def dedicated_writer():
        app.viewer._standalone_zarr_active = True
        writer_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            writer_closed.set()
            app.viewer._standalone_zarr_active = False
            await app.pipeline.resume_reader()

    async def run():
        task = asyncio.create_task(app.run())
        try:
            await _wait_until(lambda: app.pipeline.processor_task is not None)
            if mode == 1:
                await app.pipeline.start_targeted_experiment("test")
            elif mode == 2:
                app.viewer.safe_execution(dedicated_writer)
                await asyncio.wait_for(writer_started.wait(), timeout=2)
            if remove:
                dock.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            else:
                dock.close()
            await asyncio.wait_for(asyncio.shield(task), timeout=2)
            assert window.isVisible()
            assert all(stream.closed for stream in streams)
            assert len(streams) == int(mode != 2)
            assert mode != 2 or writer_closed.is_set()
            app.connection.close.assert_called_once()
            await app.pipeline.resume_reader()
            assert len(streams) == int(mode != 2)
            assert not app.viewer._action_tasks
            assert asyncio.all_tasks() == {asyncio.current_task()}
        finally:
            window.hide()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_dock_close_stops_embedded_worker_loop(streaming_app, qtbot) -> None:
    """Main-thread mode changes and dock closure also reach a worker loop."""
    app, dock, window, streams = streaming_app

    async def run():
        app.worker_loop = asyncio.get_running_loop()
        await app.run()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, run())
        try:
            qtbot.waitUntil(lambda: app.pipeline.processor_task is not None)
            assert not streams
            app.viewer.mode_combo.setCurrentIndex(0)
            qtbot.waitUntil(lambda: len(streams) == 1)
            dock.close()
            qtbot.waitUntil(future.done)
            future.result()
            assert window.isVisible()
            assert streams[0].closed
            app.connection.close.assert_called_once()
        finally:
            window.hide()
            future.result(timeout=3)
