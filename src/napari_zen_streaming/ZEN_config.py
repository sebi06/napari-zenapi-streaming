"""
Configuration for ZEN API streaming application.
"""

import argparse
import configparser
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def default_config_dir() -> Path:
    """Return the platform-specific directory for user-owned configuration."""
    if sys.platform == "win32":
        base = Path(
            os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming"
        )
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "napari-zenapi-streaming"


def default_gateway_config() -> Path:
    """Prefer user configuration while retaining source-checkout defaults."""
    user_config = default_config_dir() / "config.ini"
    if user_config.is_file():
        return user_config
    working_config = Path.cwd() / "config.ini"
    if working_config.is_file():
        return working_config
    checkout = Path(__file__).resolve().parents[2]
    checkout_config = checkout / "config.ini"
    if (checkout / "pyproject.toml").is_file() and checkout_config.is_file():
        return checkout_config
    return user_config


def load_runtime_settings() -> Path | None:
    """Load optional user settings without replacing process environment.

    ``ZEN_ENV_FILE`` selects an explicit dotenv file. Otherwise the user
    configuration directory is preferred, with a package-local dotenv file
    retained only for editable source checkouts.
    """
    explicit = os.getenv("ZEN_ENV_FILE")
    settings_file = (
        Path(explicit).expanduser()
        if explicit
        else default_config_dir() / ".env"
    )
    if explicit and not settings_file.is_file():
        raise FileNotFoundError(f"Settings file not found: {settings_file}")
    if not explicit and not settings_file.is_file():
        package_file = Path(__file__).parent / ".env"
        if (Path(__file__).resolve().parents[2] / "pyproject.toml").is_file():
            settings_file = package_file
    if not settings_file.is_file():
        return None
    load_dotenv(settings_file, override=False)
    return settings_file


def initialize_user_config() -> Path:
    """Create a gateway configuration template without overwriting a file."""
    config_file = default_config_dir() / "config.ini"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    template = configparser.ConfigParser()
    template.read_dict(
        {
            "api": {
                "host": "127.0.0.1",
                "port": "5000",
                "cert_file": r"C:\ProgramData\Carl Zeiss\ZenApiGateway\Certificates\ZenApiPersonalSigningRootCA.pem",
                "control-token": "<YOUR_CONTROL_TOKEN_HERE>",
            },
            "image_streaming": {"host": "127.0.0.1", "port": "5280"},
        }
    )
    with config_file.open("x", encoding="utf-8") as handle:
        template.write(handle)
    return config_file


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
    config_file: Path = field(default_factory=default_gateway_config)

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

    def __post_init__(self) -> None:
        """
        Validate configuration after initialization.

        Raises:
            FileNotFoundError: If config file doesn't exist
            ValueError: If any configuration values are invalid
        """
        if not self.config_file.is_file():
            raise FileNotFoundError(
                f"Config file not found: {self.config_file}. Run "
                "python -m napari_zen_streaming.ZEN_config --init "
                "or set ZEN_CONFIG_FILE to an existing gateway configuration."
            )

        if not self.exp_name:
            raise ValueError("Experiment name cannot be empty")

        if self.image_buffer_size < 1:
            raise ValueError("Image buffer size must be positive")

        if self.restructure_timeout <= 0:
            raise ValueError("Restructure timeout must be positive")

        logger.debug(
            "Configuration validated: exp=%s, buffer=%s",
            self.exp_name,
            self.image_buffer_size,
        )

    @classmethod
    def from_env(cls) -> "ZENConfig":
        """
        Create configuration from environment variables.

        Environment Variables:
            ZEN_EXP_NAME: Experiment name (default: "ZEN_API_overview")
            ZEN_UI_START: Start from UI? "true"/"false" (default: "true")
            ZEN_CONFIG_FILE: Gateway config path (default: user directory)
            ZEN_CHANNEL_INDEX: Specific channel to display (optional)
            ZEN_RESTRUCTURE_TIMEOUT: Timeout before restructuring (default: 10.0)
            ZEN_SHOW_ONLY_LAST_FRAME_DURING_LIVE: Use a 2D latest-frame
                preview during acquisition (default: true)
            ZEN_OUTPUT_DIR: Default output directory for OME-ZARR files (optional)

        Returns:
            ZENConfig: Configuration instance

        Raises:
            ValueError: If environment variables contain invalid values
        """
        try:
            config_file_raw = os.getenv("ZEN_CONFIG_FILE")
            config_file = (
                Path(config_file_raw).expanduser().resolve()
                if config_file_raw
                else default_gateway_config()
            )
            if config_file_raw == "config.ini" and not config_file.is_file():
                config_file = default_gateway_config()
            return cls(
                exp_name=os.getenv("ZEN_EXP_NAME", "ZEN_API_overview"),
                exp_started_by_ui=os.getenv("ZEN_UI_START", "true").lower()
                == "true",
                config_file=config_file,
                channel_index=(
                    int(ci) if (ci := os.getenv("ZEN_CHANNEL_INDEX")) else None
                ),
                restructure_timeout=float(
                    os.getenv("ZEN_RESTRUCTURE_TIMEOUT", "10.0")
                ),
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


def main() -> None:
    """Initialize configuration for an installed plugin or standalone app."""
    parser = argparse.ArgumentParser(
        description="Create a user-owned ZEN API configuration"
    )
    parser.add_argument(
        "--init",
        action="store_true",
        required=True,
        help="Create config.ini without overwriting existing settings",
    )
    parser.parse_args()
    try:
        config_file = initialize_user_config()
    except FileExistsError:
        parser.exit(
            message=f"Configuration already exists: {default_config_dir() / 'config.ini'}\n"
        )
    parser.exit(
        message=f"Created {config_file}. Set your control token and certificate path before connecting.\n"
    )


if __name__ == "__main__":
    main()
