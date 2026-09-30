"""
Initialization and connection management for ZEN API.

This module provides two main classes:
1. ZENConnection: Manages the gRPC connection to ZEN Blue and provides service stubs
2. ExperimentContext: Manages the lifecycle of a specific experiment

Separation of Concerns:
- Connection management is separate from experiment lifecycle
- Connection can be reused across multiple experiments
- Experiment context tracks whether experiment was started by UI or API
"""

import logging
from pathlib import Path

from zen_api.acquisition.v1beta import (
    ExperimentServiceGetAvailableExperimentsRequest,
    ExperimentServiceGetImageOutputPathRequest,
    ExperimentServiceLoadRequest,
    ExperimentServiceStartExperimentRequest,
    ExperimentServiceStopRequest,
    ExperimentServiceStub,
    ExperimentStreamingServiceStub,
)

from napari_zen_streaming.misc import initialize_zenapi
from napari_zen_streaming.ZEN_config import ZENConfig

logger = logging.getLogger(__name__)


class ZENConnection:
    """
    Manages connection to ZEN API and provides service stubs.

    This class handles the low-level gRPC connection to ZEN Blue and provides
    high-level service stubs for interacting with experiments.

    Architecture:
    - Creates persistent gRPC channel to ZEN Blue
    - Provides ExperimentService for loading/starting experiments
    - Provides ExperimentStreamingService for receiving image data
    - Separates connection management from experiment lifecycle

    Thread Safety:
    - gRPC channels are thread-safe
    - Service stubs can be used from multiple async tasks
    """

    def __init__(self, config: ZENConfig):
        """
        Initialize ZEN API connection.

        Reads connection parameters from config.ini and establishes
        gRPC channel to ZEN Blue application.

        Args:
            config: ZEN configuration object containing config_file path

        Raises:
            ConnectionError: If unable to connect to ZEN Blue
            FileNotFoundError: If config file doesn't exist
        """
        self.config = config
        logger.debug(f"Initializing ZEN API connection from {config.config_file}")

        # Initialize gRPC channel and metadata from config file
        # This reads host, port, and authentication from config.ini
        self.channel, self.metadata = initialize_zenapi(str(config.config_file))

        # Create service stubs for API communication
        # Streaming service: For receiving image data
        self.streaming_service = ExperimentStreamingServiceStub(channel=self.channel, metadata=self.metadata)

        # Experiment service: For loading, starting, managing experiments
        self.experiment_service = ExperimentServiceStub(channel=self.channel, metadata=self.metadata)

        logger.debug("ZEN API connection established successfully")

    async def load_experiment(self, experiment_name: str) -> str:
        """
        Load an experiment from ZEN.

        Loads an experiment setup (.czexp file) into ZEN Blue memory.
        Does not start acquisition - use start_experiment() for that.

        Args:
            experiment_name: Name of experiment setup to load (without .czexp extension)

        Returns:
            Experiment ID (UUID) assigned by ZEN

        Raises:
            Exception: If experiment not found or failed to load

        Note:
            Experiment must exist in ZEN's experiment folder.
        """
        logger.debug(f"Loading experiment: {experiment_name}")

        response = await self.experiment_service.load(ExperimentServiceLoadRequest(experiment_name=experiment_name))

        exp_id = response.experiment_id
        logger.debug(f"Loaded experiment '{experiment_name}' with ID: {exp_id}")
        return exp_id

    async def start_experiment(self, experiment_name: str, overwrite: bool = True) -> str:
        """
        Load and start an experiment.

        Complete workflow:
        1. Load experiment setup from ZEN
        2. Optionally delete existing output file
        3. Start experiment execution (acquisition begins)

        Args:
            experiment_name: Name of experiment to start
            overwrite: If True, delete existing .czi file (default: True)

        Returns:
            Experiment ID of the started experiment (for status monitoring)

        Raises:
            Exception: If experiment failed to load or start

        Note:
            The display pipeline uses ``exp_id`` to filter the perpetual
            all-experiments pixel stream and to register for status updates.
        """
        exp_id = await self.prepare_experiment(
            experiment_name,
            overwrite=overwrite,
        )
        await self.start_loaded_experiment(exp_id, experiment_name)
        return exp_id

    async def prepare_experiment(
        self,
        experiment_name: str,
        overwrite: bool = True,
    ) -> str:
        """Load an experiment and prepare its output before acquisition.

        This separate preparation step exposes the experiment ID before the
        acquisition starts, allowing the pixel reader to arm an ID filter
        without missing initial frames.

        Args:
            experiment_name: Experiment setup name without ``.czexp``.
            overwrite: Delete an existing output CZI when true.

        Returns:
            The loaded experiment ID assigned by ZEN.
        """
        experiment_id = await self.load_experiment(experiment_name)
        if overwrite:
            await self._cleanup_existing_experiment(experiment_name)
        return experiment_id

    async def start_loaded_experiment(
        self,
        experiment_id: str,
        output_name: str,
    ) -> None:
        """Start a previously loaded experiment.

        Args:
            experiment_id: ID returned by :meth:`prepare_experiment`.
            output_name: Output CZI name used by ZEN.
        """
        logger.debug("Starting experiment execution: %s", output_name)
        await self.experiment_service.start_experiment(
            ExperimentServiceStartExperimentRequest(
                experiment_id=experiment_id,
                output_name=output_name,
            )
        )
        logger.debug(
            "Experiment '%s' started successfully with ID: %s",
            output_name,
            experiment_id,
        )

    async def stop_experiment(self, experiment_id: str) -> None:
        """
        Stop a running experiment.

        Sends a stop command to ZEN Blue for the specified experiment ID.

        Args:
            experiment_id: ID of the experiment to stop
        Raises:
            Exception: If stopping the experiment fails
        """
        logger.debug(f"Stopping experiment with ID: {experiment_id}")
        await self.experiment_service.stop(ExperimentServiceStopRequest(experiment_id=experiment_id))
        logger.debug(f"Experiment with ID: {experiment_id} stopped successfully")

    async def _cleanup_existing_experiment(self, experiment_name: str) -> None:
        """
        Delete existing experiment file if it exists.

        Prevents accumulation of old experiment files and ensures
        fresh start for new acquisitions.

        Args:
            experiment_name: Name of experiment (used to construct file path)

        Note:
            - Gets output path from ZEN API (user's default save location)
            - Constructs full path: <output_path>/<experiment_name>.czi
            - Only deletes if file exists (no error if missing)
        """
        # Get ZEN's default output folder
        request = ExperimentServiceGetImageOutputPathRequest()
        folder_response = await self.experiment_service.get_image_output_path(request)

        # Construct full path to experiment file
        file_path = Path(folder_response.image_output_path) / f"{experiment_name}.czi"

        # Delete if exists
        if file_path.exists():
            logger.warning(f"Deleting existing experiment file: {file_path}")
            file_path.unlink()
        else:
            logger.debug(f"No existing experiment file found at: {file_path}")

    async def get_experiments(self) -> list[str]:
        """
        Retrieve a list of available experiment names from ZEN API.

        Queries ZEN Blue for all available experiment setups (.czexp files)
        that can be loaded and started.

        Returns:
            List of available experiment names (without .czexp extension)

        Example:
            >>> experiments = await connection.get_experiments()
            >>> print(experiments)
            ['ZEN_API_overview', 'MyExperiment', 'Timelapse_Setup']

        Note:
            Used to populate the UI dropdown in napari-started mode.
        """
        request = ExperimentServiceGetAvailableExperimentsRequest()
        response = await self.experiment_service.get_available_experiments(request)

        experiment_names = sorted(exp.name for exp in response.experiments)
        logger.debug(f"Found {len(experiment_names)} available experiments")

        return experiment_names

    def close(self):
        """
        Close the gRPC connection to ZEN Blue.

        Performs cleanup and releases resources.
        Should be called when application shuts down.
        """
        logger.debug("Closing ZEN API connection")

        # Close gRPC channel if it has a close method
        if hasattr(self.channel, "close"):
            self.channel.close()
            logger.debug("gRPC channel closed")


class ExperimentContext:
    """
    Manages experiment lifecycle - separate from connection.

    This class tracks the state of a specific experiment and whether
    it was started by the application or by the user in ZEN Blue.

    Two Modes:
    1. UI Mode (exp_started_by_ui=True):
       - Application doesn't start experiments
       - Monitors all running experiments
       - No experiment_id available initially

    2. API Mode (exp_started_by_ui=False):
       - Application starts specific experiment
       - Has experiment_id for targeted monitoring
       - Can use status API for completion detection
    """

    def __init__(self, connection: ZENConnection, config: ZENConfig):
        """
        Initialize experiment context.

        Args:
            connection: Active ZEN connection (already established)
            config: ZEN configuration (determines mode)
        """
        self.connection = connection
        self.config = config
        self.experiment_id: str | None = None  # Set when experiment is started/loaded
        self._is_started = False  # True if this context started the experiment

    async def initialize(self) -> str:
        """
        Initialize the experiment based on configuration mode.

        Two initialization paths:
        1. UI mode: Don't start anything, wait for user to start in ZEN
        2. API mode: Start experiment via API

        Returns:
            Experiment ID (or None in UI mode)

        Note:
            In UI mode, experiment_id remains None until user starts experiment.
            In API mode, experiment_id is available immediately.
        """
        if self.config.exp_started_by_ui:
            # UI mode: Monitor experiments started by user in ZEN Blue
            logger.debug("Experiment context initialized in UI/monitoring mode")
            logger.debug("Waiting for user to start experiment in ZEN Blue")
            self.experiment_id = None

        else:
            # API mode: Start experiment programmatically
            logger.debug(f"Starting experiment via API: {self.config.exp_name}")
            self.experiment_id = await self.connection.start_experiment(self.config.exp_name, overwrite=True)
            self._is_started = True
            logger.debug(f"Experiment started with ID: {self.experiment_id}")

        return self.experiment_id

    @property
    def is_monitoring_mode(self) -> bool:
        """
        Check if in monitoring mode (experiment started by UI).

        Returns:
            True if monitoring mode (user starts in ZEN Blue)
            False if API mode (application starts experiment)
        """
        return self.config.exp_started_by_ui

    @property
    def is_started(self) -> bool:
        """
        Check if experiment was started by this context.

        Returns:
            True if this context started the experiment via API
            False if experiment was started by user in ZEN Blue
        """
        return self._is_started
