"""
Main entry point for ZEN API streaming application.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import dotenv
import napari
import qasync
from napari.qt.threading import thread_worker
from qtpy import QtWidgets

# Add parent directory to path for direct script execution
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))

try:
    from .ZEN_config import ZENConfig
    from .ZEN_init import ExperimentContext, ZENConnection
    from .ZEN_pipeline import StreamingPipeline
    from .ZEN_ui import StreamingViewer
except ImportError:
    from ZEN_config import ZENConfig
    from ZEN_init import ExperimentContext, ZENConnection
    from ZEN_pipeline import StreamingPipeline
    from ZEN_ui import StreamingViewer

# Configure logging
log_dir = Path(__file__).parent / "logging"
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "zen_streaming.log"

logging.basicConfig(
    level=logging.INFO,
    # level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(log_file)],
)
logger = logging.getLogger(__name__)


class ZENApplication:
    """
    Main application orchestrating connection, viewer, and pipeline.
    """

    def __init__(self, config: ZENConfig):
        self.config = config
        self.embedded = napari.current_viewer() is not None
        self.worker_loop = None

        # Initialize Qt and asyncio loop
        self.qt_app = napari.qt.get_qapp() if self.embedded else QtWidgets.QApplication(sys.argv)
        try:
            existing = asyncio.get_running_loop()
            self.loop = existing if isinstance(existing, qasync.QEventLoop) else qasync.QEventLoop(self.qt_app)
            asyncio.set_event_loop(self.loop)
        except RuntimeError:
            self.loop = qasync.QEventLoop(self.qt_app)
            asyncio.set_event_loop(self.loop)

        # Components
        self.connection: ZENConnection = None
        self.experiment: ExperimentContext = None
        self.viewer: StreamingViewer = None
        self.pipeline: StreamingPipeline = None

        logger.debug("ZEN Application initialized")

    async def setup_connection(self) -> list[str]:
        """Initialize connection and experiment context."""
        self.connection = ZENConnection(self.config)
        self.experiment = ExperimentContext(self.connection, self.config)
        await self.experiment.initialize()

        experiments = await self.connection.get_experiments()
        return experiments

    def setup_viewer(self, experiments: list[str] = None) -> None:
        """
        Setup viewer and pipeline (Qt main thread).

        Creates the StreamingViewer, populates the experiment dropdown,
        and initializes the StreamingPipeline.

        Args:
            experiments: List of experiment names to populate dropdown (optional)

        Note:
            This method must be called from the Qt main thread since it
            creates Qt widgets and modifies the napari viewer.
        """
        self.viewer = StreamingViewer(self.config)
        self.viewer.experiment = self.experiment
        self.viewer.app = self
        self.viewer.show()

        self.pipeline = StreamingPipeline(
            connection=self.connection,
            experiment=self.experiment,
            viewer=self.viewer,
            config=self.config,
        )

        if experiments:
            # Populate after the pipeline assigns viewer.connection so the
            # initial selection can load its dimensions immediately.
            self.viewer.populate_experiments(experiments)

    def run_coro_in_worker(self, coro):
        """Run coroutine in worker thread loop."""
        if self.worker_loop and not self.worker_loop.is_closed():
            return asyncio.run_coroutine_threadsafe(coro, self.worker_loop)
        elif self.embedded:
            return asyncio.create_task(coro)
        else:
            raise RuntimeError("Worker event loop not available")

    def start_pipeline_worker(self):
        """Run pipeline in background thread with its own event loop."""

        @thread_worker
        def run_pipeline():
            asyncio.set_event_loop(self.worker_loop)
            try:
                self.worker_loop.run_until_complete(self.run())
            except Exception as e:
                logger.error(f"Pipeline error: {e}", exc_info=True)

        worker = run_pipeline()
        worker.start()

    async def setup(self) -> None:
        experiments = await self.setup_connection()
        self.setup_viewer(experiments)

    async def run(self) -> None:
        """Run pipeline until viewer is closed."""
        logger.debug("Starting application")
        try:
            async with self.pipeline:
                while True:
                    try:
                        if not self.viewer.viewer.window._qt_window.isVisible():
                            break
                    except RuntimeError:
                        # Qt window deleted — napari is closing.
                        break
                    await asyncio.sleep(0.1)
        except KeyboardInterrupt:
            logger.debug("Keyboard interrupt received")
        finally:
            await self.cleanup()

    async def cleanup(self) -> None:
        logger.debug("Cleaning up application")
        if self.viewer:
            try:
                self.viewer.close()
            except RuntimeError:
                logger.debug("Viewer already deleted, skipping close")
        if self.connection:
            self.connection.close()
        logger.debug("Cleanup complete")

    def start(self) -> None:
        """Start application in embedded or standalone mode."""
        if self.embedded:

            @thread_worker
            def run_setup_and_pipeline():
                # Create dedicated event loop for this thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self.worker_loop = loop

                try:
                    # Setup connection
                    experiments = loop.run_until_complete(self.setup_connection())

                    # Return experiments to main thread
                    return experiments
                except Exception as e:
                    logger.error(f"Setup error: {e}", exc_info=True)
                    return None

            worker = run_setup_and_pipeline()

            @worker.returned.connect
            def on_setup_complete(experiments):
                if experiments is not None:
                    self.setup_viewer(experiments)

                    # Start pipeline in the worker loop
                    if self.worker_loop and not self.worker_loop.is_closed():
                        # Schedule pipeline start
                        asyncio.run_coroutine_threadsafe(self.run(), self.worker_loop)

                        # Keep worker loop running in background
                        @thread_worker
                        def keep_loop_alive():
                            asyncio.set_event_loop(self.worker_loop)
                            self.worker_loop.run_forever()

                        loop_worker = keep_loop_alive()
                        loop_worker.start()
                else:
                    logger.error("Failed to retrieve experiments")

            worker.start()
        else:
            # Standalone mode
            async def main_tasks():
                await self.setup()
                await self.run()

            with self.loop:
                self.loop.create_task(main_tasks())
                self.loop.run_forever()


def main():
    dotenv.load_dotenv()

    parser = argparse.ArgumentParser(description="ZEN API Streaming Application")
    parser.add_argument("--ui-start", choices=["true", "false"])
    parser.add_argument("--exp-name")
    parser.add_argument("--config-file")
    parser.add_argument("--channel-index", type=int)
    parser.add_argument("--restructure-timeout", type=float)
    args = parser.parse_args()

    if args.ui_start is not None:
        os.environ["ZEN_UI_START"] = args.ui_start.lower()
    if args.exp_name:
        os.environ["ZEN_EXP_NAME"] = args.exp_name
    if args.config_file:
        os.environ["ZEN_CONFIG_FILE"] = args.config_file
    if args.channel_index is not None:
        os.environ["ZEN_CHANNEL_INDEX"] = str(args.channel_index)
    if args.restructure_timeout is not None:
        os.environ["ZEN_RESTRUCTURE_TIMEOUT"] = str(args.restructure_timeout)

    try:
        config = ZENConfig.from_env()
        logger.debug("=" * 60)
        logger.debug("Starting ZEN API Streaming Application")
        logger.debug(f"Mode: {'Napari UI' if config.exp_started_by_ui else 'ZEN Monitor'}")
        logger.debug(f"Experiment: {config.exp_name}")
        logger.debug("=" * 60)

        app = ZENApplication(config)
        app.start()
    except Exception as e:
        logger.error(f"Failed to start application: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
