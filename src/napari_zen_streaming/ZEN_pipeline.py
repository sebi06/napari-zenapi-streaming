"""
Streaming pipeline for ZEN API data acquisition and processing.

This module orchestrates the flow of microscopy image data from ZEN to Napari:
1. Reader task: Pulls frames from ZEN API streaming service
2. Experiment filter: Isolates napari-started acquisitions by frame ID
3. Queue: Buffers frames for processing
4. Processor task: Processes frames and updates Napari viewer

The pipeline supports two modes:
- Monitoring mode: Watches all experiments (ZEN-started)
- Targeted mode: Keeps the global stream open but accepts only frames matching
    the experiment ID loaded before a napari-started acquisition
"""

import asyncio
import contextlib
import logging
from typing import Any

from zen_api.acquisition.v1beta import (
    ExperimentStreamingServiceMonitorAllExperimentsRequest,
)

from napari_zen_streaming.ZEN_config import ZENConfig
from napari_zen_streaming.ZEN_init import ExperimentContext, ZENConnection
from napari_zen_streaming.ZEN_ui import StreamingViewer
from napari_zen_streaming.ZEN_utils import (
    extract_image_data,
    extract_metadata,
    process_frame,
)

logger = logging.getLogger(__name__)


class StreamingPipeline:
    """
    Manages the complete streaming pipeline from acquisition to display.

    Architecture:
    - Asynchronous reader task continuously pulls frames from ZEN API
    - Queue buffers frames to decouple reading from processing
    - Asynchronous processor task processes frames and updates viewer
    - Context manager (__aenter__/__aexit__) for clean startup/shutdown

    Thread Safety:
    - All operations are async (no threading)
    - Queue provides safe communication between tasks
    - Stop event signals graceful shutdown
    """

    def __init__(
        self, connection: ZENConnection, experiment: ExperimentContext, viewer: StreamingViewer, config: ZENConfig
    ):
        """
        Initialize streaming pipeline.

        Args:
            connection: ZEN API connection with service stubs
            experiment: Experiment context (contains ID and mode)
            viewer: Streaming viewer for display in Napari
            config: Application configuration
        """
        self.connection = connection
        self.experiment = experiment
        self.viewer = viewer
        self.viewer.connection = self.connection  # Give viewer access to connection
        self.config = config

        # ========== Pipeline Control ==========
        # Queue for buffering frames between reader and processor

        # Set max size of 200 images in queue; number is set to be large enough to handle bursts
        # but not too large to run out of a normal amount of memory (~4GB for 200 4096x3008 uint16 images)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=200)

        # Event to signal pipeline shutdown
        self.stop_event = asyncio.Event()

        # ========== Async Tasks ==========
        # Reader task pulls frames from ZEN API
        self.reader_task: asyncio.Task | None = None

        # Active gRPC async iterator for the reader task.
        # Stored here so suspend_reader() can call aclose() on it
        # explicitly, which sends RST_STREAM to ZEN and de-registers
        # this connection as a frame consumer.  Without an explicit
        # aclose() ZEN keeps distributing frames to the dead stream
        # and the standalone OME-ZARR writer only receives half of them.
        self._reader_iter: Any = None
        self._reader_ready = asyncio.Event()

        # Napari-started runs use the perpetual all-experiments transport but
        # accept only frames carrying this ID. None preserves ZEN-started mode.
        self._target_experiment_id: str | None = None

        # Processor task processes frames and updates viewer
        self.processor_task: asyncio.Task | None = None

        # ========== Statistics ==========
        # Track pipeline performance
        self.frames_received = 0  # Total frames pulled from ZEN
        self.frames_processed = 0  # Total frames processed and displayed

    async def start(self) -> None:
        """
        Start the streaming pipeline.

        Creates and starts two concurrent tasks:
        1. Reader task: Pulls frames from ZEN API
        2. Processor task: Processes frames from queue

        Both tasks run concurrently until stop() is called.
        """
        logger.debug("Starting streaming pipeline")

        # Start reader and processor tasks concurrently
        self.reader_task = asyncio.create_task(self._read_frames())
        self.processor_task = asyncio.create_task(self._process_frames())

        logger.debug("Pipeline started - reader and processor tasks running")

    async def stop(self) -> None:
        """
        Stop the streaming pipeline gracefully.

        Shutdown sequence:
        1. Set stop event to signal tasks
        2. Cancel reader task (it's blocking on async iteration)
        3. Wait for queue to empty (process remaining frames)
        4. Cancel processor task
        5. Log final statistics
        """
        logger.debug("Stopping streaming pipeline")

        # Signal all tasks to stop
        self.stop_event.set()

        # Cancel reader task (blocking on ZEN API async iterator)
        if self.reader_task:
            self.reader_task.cancel()
            try:
                await self.reader_task
            except asyncio.CancelledError:
                logger.debug("Reader task cancelled successfully")

        # Wait for queue to empty (process remaining frames)
        if self.queue:
            await self.queue.join()
            logger.debug("Frame queue emptied")

        # Cancel processor task
        if self.processor_task:
            self.processor_task.cancel()
            try:
                await self.processor_task
            except asyncio.CancelledError:
                logger.debug("Processor task cancelled successfully")

        logger.debug(
            f"Pipeline stopped. Statistics: Received={self.frames_received}, Processed={self.frames_processed}"
        )

    async def _read_frames(self) -> None:
        """
        Read frames from ZEN API streaming service.

        Supports two streaming modes:
        1. Monitor all experiments (ZEN-started): Watches all running experiments
          2. Filter one experiment (Napari-started): Uses the global stream but
              accepts only matching ``FrameData.experiment_id`` values

        The async iterator from ZEN API yields frames continuously until:
        - Experiment completes naturally (iterator exhausts)
        - Pipeline is stopped (stop_event set)
        - Error occurs

        When the iterator completes, notifies viewer that experiment finished.
        """
        logger.debug("Starting frame reader task")

        try:
            # Create appropriate streaming request based on mode.
            #
            # IMPORTANT: Always use monitor_all_experiments for the
            # pixel stream, even for napari-started experiments.
            # monitor_experiment closes the gRPC stream as soon as
            # the experiment status changes to "finished", which can
            # happen before all pixel data has been delivered.  Using
            # monitor_all_experiments keeps the stream open so every
            # frame arrives; termination is handled separately by the
            # status monitor or the restructure timeout.
            if self.experiment.is_monitoring_mode:
                logger.debug("Monitoring all experiments (ZEN-started mode)")
            else:
                logger.debug(
                    f"Monitoring all experiments for " f"{self.experiment.experiment_id} " f"(Napari-started mode)"
                )

            # Use the viewer's effective channel index (overridable
            # from the UI combo box) rather than the frozen config
            # value, so the user can change it at runtime.
            effective_ch = getattr(
                self.viewer,
                "_effective_channel_index",
                self.config.channel_index,
            )
            request = ExperimentStreamingServiceMonitorAllExperimentsRequest(
                channel_index=effective_ch,
                enable_raw_data=self.config.enable_raw_data,
            )
            self._reader_iter = self.connection.streaming_service.monitor_all_experiments(request).__aiter__()
            self._reader_ready.set()

            # Read frames from ZEN API stream
            async for response in self._reader_iter:
                # Check for graceful shutdown
                if self.stop_event.is_set():
                    logger.debug("Stop event detected - halting frame reader")
                    break

                if not self._accepts_response(response):
                    logger.debug(
                        "Ignoring frame from experiment %s while targeting %s",
                        response.frame_data.experiment_id,
                        self._target_experiment_id,
                    )
                    continue

                # Queue frame for processing
                if self.queue.full():
                    logger.warning(f"Queue full (maxsize={self.queue.maxsize})." " Dropping or delaying item.")

                await self.queue.put(response)
                self.frames_received += 1

            # Iterator completed naturally - experiment finished
            logger.debug(f"Frame iterator completed after " f"{self.frames_received} frames (experiment finished)")

            # Notify viewer that experiment has finished
            # This triggers restructure for ZEN-started experiments
            if hasattr(self.viewer, "on_experiment_finished"):
                try:
                    await self.viewer.on_experiment_finished()
                except Exception as e:
                    logger.error(
                        f"Error calling on_experiment_finished: {e}",
                        exc_info=True,
                    )

        except asyncio.CancelledError:
            logger.debug("Frame reader task cancelled (pipeline stopped)")
            raise
        except Exception as e:
            logger.error(f"Error in frame reader: {e}", exc_info=True)
            raise
        finally:
            self._reader_ready.clear()
            # Explicitly close the gRPC async iterator so ZEN Blue
            # de-registers this monitor_all_experiments consumer
            # immediately.  Cancelling the Python task alone does not
            # send RST_STREAM; without aclose() ZEN continues to
            # distribute frames to the dead stream, starving any
            # concurrent standalone OME-ZARR writer.
            iter_ref = self._reader_iter
            self._reader_iter = None
            if iter_ref is not None:
                with contextlib.suppress(Exception):
                    await iter_ref.aclose()
                logger.debug("gRPC reader iterator closed")
            logger.debug("Frame reader task stopped")

    def set_target_experiment(self, experiment_id: str | None) -> None:
        """Restrict accepted frames to one napari-started experiment ID."""
        self._target_experiment_id = experiment_id
        if experiment_id is None:
            logger.info("Pixel stream experiment filter cleared")
        else:
            logger.info(
                "Pixel stream filtered to experiment ID: %s",
                experiment_id,
            )

    def _accepts_response(self, response: Any) -> bool:
        """Return whether a streamed frame matches the active target."""
        target = self._target_experiment_id
        return target is None or response.frame_data.experiment_id == target

    async def ensure_reader_ready(self) -> None:
        """Ensure the perpetual all-experiments stream is armed."""
        await self.resume_reader()
        await self._reader_ready.wait()

    async def start_targeted_experiment(
        self,
        experiment_name: str,
        overwrite: bool = True,
    ) -> str:
        """Prepare, filter, and start one napari-triggered experiment.

        The all-experiments stream is opened first for reliable trailing-frame
        delivery. ZEN then loads the experiment, the client-side ID filter is
        armed, and only then is acquisition started.
        """
        await self.ensure_reader_ready()
        experiment_id = await self.connection.prepare_experiment(
            experiment_name,
            overwrite=overwrite,
        )
        self.set_target_experiment(experiment_id)
        try:
            await self.connection.start_loaded_experiment(
                experiment_id,
                experiment_name,
            )
        except Exception:
            self.set_target_experiment(None)
            raise
        return experiment_id

    async def _process_frames(self) -> None:
        """
        Process frames from the queue and update viewer.

        Processing pipeline for each frame:
        1. Wait for frame from queue (with timeout to check stop event)
        2. Extract and normalize image data using process_frame()
        3. Send to viewer for display
        4. Mark queue item as done

        Runs continuously until stop_event is set.
        Errors in individual frame processing are logged but don't stop the pipeline.
        """
        logger.debug("Starting frame processor task")

        try:
            while not self.stop_event.is_set():
                try:
                    # Wait for frame with timeout to periodically check stop event
                    response = await asyncio.wait_for(self.queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    # No frame available, continue loop to check stop event
                    continue

                try:
                    # Process the frame: extract image data and metadata.
                    # In zarr-write mode, skip display normalisation to
                    # preserve the original pixel values for OME-ZARR.
                    if getattr(self.viewer, "_zarr_write_mode", False):
                        metadata = extract_metadata(response)
                        display_image = extract_image_data(
                            response,
                            self.config.pixel_dtype,
                            metadata.shape,
                        )
                    else:
                        display_image, metadata = process_frame(
                            response,
                            self.config.pixel_dtype,
                            self.config.display_dtype,
                        )

                    # Update viewer with processed frame
                    # This updates the 7D streaming layer during acquisition
                    await self.viewer.add_frame(display_image, metadata)

                    self.frames_processed += 1

                except Exception as e:
                    # Log error but continue processing other frames
                    logger.error(f"Error processing frame {self.frames_processed + 1}: {e}", exc_info=True)

                finally:
                    # Always mark task as done for queue bookkeeping
                    self.queue.task_done()

        except asyncio.CancelledError:
            logger.debug("Frame processor task cancelled (pipeline stopped)")
            raise
        finally:
            logger.debug(f"Frame processor task stopped (processed {self.frames_processed} total frames)")

    async def __aenter__(self):
        """
        Context manager entry - start the pipeline.

        Example usage:
            async with StreamingPipeline(...) as pipeline:
                # Pipeline is running
                await some_operation()
            # Pipeline automatically stopped
        """
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        Context manager exit - stop the pipeline gracefully.

        Args:
            exc_type: Exception type (if any)
            exc_val: Exception value (if any)
            exc_tb: Exception traceback (if any)
        """
        await self.stop()

    async def suspend_reader(self) -> None:
        """Cancel the reader task and close the gRPC stream.

        Call this before starting a standalone OME-ZARR writer so that
        only one ``monitor_all_experiments`` consumer exists.  ZEN
        distributes frames across all active consumers; a second consumer
        causes frame loss in both.

        Explicitly calls ``aclose()`` on the gRPC async iterator after
        cancelling the task.  This sends an RST_STREAM to ZEN Blue so
        the server de-registers this consumer immediately, rather than
        waiting for garbage collection.  Without this, the standalone
        OME-ZARR writer's channel and the pipeline's half-open stream
        both appear as active consumers and ZEN splits every frame
        evenly between them.

        Safe to call when no reader is running (no-op).
        """
        if self.reader_task and not self.reader_task.done():
            self.reader_task.cancel()
            try:
                await self.reader_task
            except asyncio.CancelledError:
                pass
            self.reader_task = None
        # Belt-and-suspenders: aclose() is also called in _read_frames'
        # finally block, but if that await was itself interrupted we
        # may need another attempt here.
        if self._reader_iter is not None:
            with contextlib.suppress(Exception):
                await self._reader_iter.aclose()
            self._reader_iter = None
            logger.debug("Pipeline gRPC iterator explicitly closed")
        logger.debug("Pipeline reader suspended")

    async def resume_reader(self) -> None:
        """Restart the reader task after a standalone writer finishes.

        Creates a fresh ``_read_frames`` task so the pipeline can service
        the next experiment in Display mode.  Safe to call when the reader
        is already running (no-op).
        """
        if self.reader_task and not self.reader_task.done():
            logger.debug("Pipeline reader already running – skipping resume")
            return
        self.reader_task = asyncio.create_task(self._read_frames())
        logger.debug("Pipeline reader resumed")
