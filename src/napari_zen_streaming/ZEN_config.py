"""
Configuration for ZEN API streaming application.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Raw ZEN stream dtype used by Display mode and legacy paths. The OME-ZARR
# writer derives its dtype from each runtime frame's PixelType instead.
DEFAULT_PIXEL_DTYPE = np.dtype(np.uint16)


@dataclass(frozen=True)
class ZENConfig:
    """Immutable configuration for ZEN API connection and experiment."""

    # Experiment configuration
    exp_name: str
    exp_started_by_ui: bool = False

    # API configuration
    config_file: Path = field(default_factory=lambda: Path("config.ini"))

    # Data configuration
    pixel_dtype: np.dtype = field(default_factory=lambda: DEFAULT_PIXEL_DTYPE)
    display_dtype: np.dtype = field(default_factory=lambda: np.dtype(np.uint8))

    # Processing configuration
    image_buffer_size: int = 100
    enable_raw_data: bool = False
    channel_index: int | None = None

    # Viewer configuration
    # Seconds to wait before restructuring the live preview to final layers.
    restructure_timeout: float = 10.0
    show_only_last_frame_during_live: bool = True

    # Default selections for the UI
    default_output_dir: str = ""

    def __post_init__(self):
        """
        Validate configuration after initialization.

        Raises:
            FileNotFoundError: If config file doesn't exist
            ValueError: If any configuration values are invalid
        """
        if not self.config_file.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_file}")

        if not self.exp_name:
            raise ValueError("Experiment name cannot be empty")

        if self.image_buffer_size < 1:
            raise ValueError("Image buffer size must be positive")

        if self.restructure_timeout <= 0:
            raise ValueError("Restructure timeout must be positive")

        # Log configuration summary
        import logging

        logger = logging.getLogger(__name__)
        logger.debug("Configuration validated: exp={self.exp_name}, buffer={self.image_buffer_size}")

    @classmethod
    def from_env(cls) -> "ZENConfig":
        """
        Create configuration from environment variables.

        Environment Variables:
            ZEN_EXP_NAME: Experiment name (default: "ZEN_API_overview")
            ZEN_UI_START: Start from UI? "true"/"false" (default: "true")
            ZEN_CONFIG_FILE: Path to config file (default: "config.ini")
            ZEN_CHANNEL_INDEX: Specific channel to display (optional)
            ZEN_RESTRUCTURE_TIMEOUT: Timeout before restructuring (default: 3.0)
            ZEN_SHOW_ONLY_LAST_FRAME_DURING_LIVE: Use a 2D latest-frame
                preview during acquisition (default: true)
            ZEN_OUTPUT_DIR: Default output directory for OME-ZARR files (optional)

        Returns:
            ZENConfig: Configuration instance

        Raises:
            ValueError: If environment variables contain invalid values
        """
        try:
            config_file_raw = os.getenv("ZEN_CONFIG_FILE", "config.ini")
            config_file = Path(config_file_raw)
            # Resolve relative paths against the package directory so
            # the plugin works regardless of the working directory.
            if not config_file.is_absolute():
                config_file = Path(__file__).parent.parent.parent / config_file
            return cls(
                exp_name=os.getenv("ZEN_EXP_NAME", "ZEN_API_overview"),
                exp_started_by_ui=os.getenv("ZEN_UI_START", "true").lower() == "true",
                config_file=config_file,
                channel_index=int(ci) if (ci := os.getenv("ZEN_CHANNEL_INDEX")) else None,
                restructure_timeout=float(os.getenv("ZEN_RESTRUCTURE_TIMEOUT", "10.0")),
                show_only_last_frame_during_live=os.getenv(
                    "ZEN_SHOW_ONLY_LAST_FRAME_DURING_LIVE",
                    "true",
                ).lower()
                == "true",
                default_output_dir=os.getenv("ZEN_OUTPUT_DIR", ""),
            )
        except ValueError as e:
            raise ValueError(f"Invalid environment variable value: {e}") from e


@dataclass
class ImageMetadata:
    """Runtime metadata for a single image frame."""

    width: int
    height: int
    scaling_x_um: float
    scaling_y_um: float
    stage_x_um: float
    stage_y_um: float
    stage_z_um: float
    frame_s: int
    frame_m: int
    frame_c: int
    frame_h: int
    frame_t: int
    frame_z: int

    @property
    def shape(self) -> tuple[int, int]:
        """Get image shape as (height, width)."""
        return (self.height, self.width)

    @property
    def stage_position(self) -> tuple[float, float, float]:
        """Get stage position as (x, y, z) in micrometers."""
        return (self.stage_x_um, self.stage_y_um, self.stage_z_um)

    @property
    def scaling(self) -> tuple[float, float]:
        """Get pixel scaling as (x, y) in micrometers."""
        return (self.scaling_x_um, self.scaling_y_um)
