"""
Napari plugin widget for ZEN API streaming.
"""

import asyncio
import logging

from napari.qt.threading import thread_worker
from qtpy.QtWidgets import QLabel, QVBoxLayout, QWidget

from napari_zen_streaming._logging import configure_logging
from napari_zen_streaming.ZEN_config import load_runtime_settings

logger = logging.getLogger(__name__)


def create_zen_widget() -> QWidget:
    """
    Factory function called by napari to create the widget.

    This creates a minimal status widget and initializes the full ZEN streaming
    application in the background. The actual experiment selector UI is created
    by the StreamingViewer and added as a separate dock widget.
    """
    # Create simple status widget
    widget = QWidget()
    layout = QVBoxLayout()

    status_label = QLabel("Initializing ZEN Streaming...")
    layout.addWidget(status_label)
    widget.setLayout(layout)

    from napari_zen_streaming.main import ZENApplication
    from napari_zen_streaming.ZEN_config import ZENConfig

    try:
        load_runtime_settings()
        configure_logging()
        app = ZENApplication(ZENConfig.from_env())
    except (FileNotFoundError, ValueError) as exc:
        logger.exception("Invalid ZEN streaming configuration")
        status_label.setText(str(exc))
        return widget

    @thread_worker
    def init_zen():
        """Open the connection on the event loop that will receive frames."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app.worker_loop = loop
        try:
            logger.debug("Initializing ZEN connection...")
            experiments = loop.run_until_complete(app.setup_connection())

            logger.debug(
                f"Successfully retrieved {len(experiments)} experiments"
            )

            # Return to main thread to setup viewer
            return app, experiments, loop

        except Exception as e:
            logger.error(f"Initialization failed: {e}", exc_info=True)
            if app.connection is not None:
                app.connection.close()
            loop.close()
            return None, None, None

    worker = init_zen()

    @worker.returned.connect
    def on_init_complete(result):
        app, experiments, loop = result

        if app is not None and experiments is not None and loop is not None:
            logger.debug("Setting up viewer in main thread...")
            app.setup_viewer(experiments)

            # Auto-close this initialization widget
            from napari import current_viewer

            viewer = current_viewer()
            if viewer:
                # Find and remove this dock widget
                for key, dock_widget in list(
                    viewer.window.dock_widgets.items()
                ):
                    try:
                        if dock_widget is widget or (
                            hasattr(dock_widget, "widget")
                            and dock_widget.widget() is widget
                        ):
                            viewer.window.remove_dock_widget(dock_widget)
                            break
                    except (AttributeError, KeyError, RuntimeError) as e:
                        logger.debug(f"Skipping dock widget {key}: {e}")
                        continue

            logger.debug("Starting pipeline in worker thread...")

            @thread_worker
            def run_pipeline():
                asyncio.set_event_loop(loop)
                try:
                    logger.debug("Pipeline starting...")
                    loop.run_until_complete(app.run())
                    logger.debug("Pipeline completed")
                except Exception as e:
                    logger.error(f"Pipeline error: {e}", exc_info=True)
                finally:
                    loop.close()

            pipeline_worker = run_pipeline()
            pipeline_worker.start()

            logger.debug("ZEN streaming fully initialized")

        else:
            logger.error("Failed to initialize")
            status_label.setText("Initialization Failed\n(Check console)")
            status_label.setStyleSheet("color: red; padding: 10px;")

    # Start initialization
    worker.start()

    return widget
