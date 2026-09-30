"""Tests for shared package logging configuration."""

import logging
from pathlib import Path

from napari_zen_streaming._logging import configure_logging, default_log_dir


def test_default_log_dir_uses_environment_setting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """ZEN_LOG_DIR selects the directory used by default."""
    monkeypatch.setenv("ZEN_LOG_DIR", str(tmp_path))

    assert default_log_dir() == tmp_path


def test_configure_logging_uses_environment_level(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """ZEN_LOG_LEVEL controls package and handler verbosity."""
    monkeypatch.setenv("ZEN_LOG_LEVEL", "DEBUG")

    logger = configure_logging(tmp_path)

    assert logger.level == logging.DEBUG
    assert all(handler.level == logging.DEBUG for handler in logger.handlers)


def test_configure_logging_is_idempotent_and_writes_file(
    tmp_path: Path,
    capsys,
) -> None:
    """Repeated setup keeps one terminal and one rotating file handler."""
    logger = configure_logging(tmp_path)
    logger = configure_logging(tmp_path)

    logging.getLogger("napari_zen_streaming.test").info("shared logging works")
    for handler in logger.handlers:
        handler.flush()

    managed_handlers = [
        handler for handler in logger.handlers if getattr(handler, "_napari_zen_streaming_handler", False)
    ]
    assert len(managed_handlers) == 2
    terminal_output = capsys.readouterr().out
    file_output = (tmp_path / "zen_streaming.log").read_text(encoding="utf-8")
    assert f"Logging to {tmp_path.resolve()}" in terminal_output
    assert "shared logging works" in terminal_output
    assert f"Logging to {tmp_path.resolve()}" in file_output
    assert "shared logging works" in file_output


def test_configure_logging_rotates_bounded_files(tmp_path: Path) -> None:
    """The active file and configured backups bound retained log growth."""
    logger = configure_logging(tmp_path, max_bytes=80, backup_count=2)

    for index in range(12):
        logger.info("rotation record %s %s", index, "x" * 40)
    for handler in logger.handlers:
        handler.flush()

    log_files = list(tmp_path.glob("zen_streaming.log*"))
    assert 1 < len(log_files) <= 3
