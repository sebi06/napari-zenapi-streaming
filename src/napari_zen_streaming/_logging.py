"""Logging configuration shared by the napari plugin and CLI."""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

PACKAGE_LOGGER_NAME = "napari_zen_streaming"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3
_MANAGED_HANDLER = "_napari_zen_streaming_handler"


def configured_log_level(level: int | str | None = None) -> int:
    """Return a numeric log level from an argument or ``ZEN_LOG_LEVEL``."""
    configured_level = level if level is not None else os.getenv("ZEN_LOG_LEVEL", "INFO")
    if isinstance(configured_level, int):
        return configured_level

    numeric_level = getattr(logging, configured_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {configured_level}")
    return numeric_level


def default_log_dir() -> Path:
    """Return a user-writable platform log directory."""
    configured_dir = os.getenv("ZEN_LOG_DIR")
    if configured_dir:
        return Path(configured_dir).expanduser()

    if sys.platform == "win32":
        base_dir = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base_dir = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base_dir / "napari-zenapi-streaming" / "logs"


def configure_logging(
    log_dir: str | Path | None = None,
    level: int | str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> logging.Logger:
    """Configure package terminal and rotating-file logging.

    Args:
        log_dir: Directory for ``zen_streaming.log``. Defaults to a
            user-writable platform directory or ``ZEN_LOG_DIR``.
        level: Minimum logging level for both handlers. Defaults to
            ``ZEN_LOG_LEVEL`` or ``INFO``.
        max_bytes: Maximum size of the active log before rotation.
        backup_count: Number of rotated log files to retain.

    Returns:
        The configured package logger.
    """
    target_dir = Path(log_dir) if log_dir is not None else default_log_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    effective_level = configured_log_level(level)

    package_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    package_logger.setLevel(effective_level)
    package_logger.propagate = False

    for handler in list(package_logger.handlers):
        if getattr(handler, _MANAGED_HANDLER, False):
            package_logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    terminal_handler = logging.StreamHandler(sys.stdout)
    log_file = target_dir / "zen_streaming.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    for handler in (terminal_handler, file_handler):
        handler.setLevel(effective_level)
        handler.setFormatter(formatter)
        setattr(handler, _MANAGED_HANDLER, True)
        package_logger.addHandler(handler)

    package_logger.info(
        "Logging to %s (level=%s)",
        log_file.resolve(),
        logging.getLevelName(effective_level),
    )
    return package_logger
