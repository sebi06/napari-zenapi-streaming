"""Tests for user-owned configuration and environment precedence."""

import configparser
from pathlib import Path
from types import SimpleNamespace

import pytest

from napari_zen_streaming import ZEN_config as config_module


@pytest.fixture
def user_config_dir(tmp_path: Path, monkeypatch) -> Path:
    """Keep configuration tests isolated from real user and checkout files."""
    directory = tmp_path / "user"
    monkeypatch.setattr(config_module, "default_config_dir", lambda: directory)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZEN_CONFIG_FILE", raising=False)
    monkeypatch.delenv("ZEN_ENV_FILE", raising=False)
    return directory


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_platform_config_directory(platform, tmp_path, monkeypatch) -> None:
    """Windows and XDG settings use a user directory, not site-packages."""
    monkeypatch.setattr(
        config_module, "sys", SimpleNamespace(platform=platform)
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert (
        config_module.default_config_dir()
        == tmp_path / "napari-zenapi-streaming"
    )


def test_initialize_config_preserves_existing_settings(
    user_config_dir,
) -> None:
    """Initialization creates a usable template once and never overwrites it."""
    path = config_module.initialize_user_config()
    parsed = configparser.ConfigParser()
    parsed.read(path)
    assert path == user_config_dir / "config.ini"
    assert parsed["api"]["host"] == "127.0.0.1"
    assert parsed["api"]["control-token"] == "<YOUR_CONTROL_TOKEN_HERE>"
    path.write_text("user settings", encoding="utf-8")
    with pytest.raises(FileExistsError):
        config_module.initialize_user_config()
    assert path.read_text(encoding="utf-8") == "user settings"


def test_user_config_is_independent_of_install_location(
    user_config_dir, tmp_path, monkeypatch
) -> None:
    """A wheel installation resolves configuration outside its Python prefix."""
    path = config_module.initialize_user_config()
    installed_module = (
        tmp_path
        / "env"
        / "Lib"
        / "site-packages"
        / "napari_zen_streaming"
        / "ZEN_config.py"
    )
    monkeypatch.setattr(config_module, "__file__", str(installed_module))
    assert config_module.ZENConfig.from_env().config_file == path


def test_missing_wheel_config_reports_user_path(
    user_config_dir, tmp_path, monkeypatch
) -> None:
    """Missing settings explain initialization rather than searching Lib."""
    installed_module = (
        tmp_path
        / "env"
        / "Lib"
        / "site-packages"
        / "napari_zen_streaming"
        / "ZEN_config.py"
    )
    monkeypatch.setattr(config_module, "__file__", str(installed_module))
    assert (
        config_module.default_gateway_config()
        == user_config_dir / "config.ini"
    )
    with pytest.raises(FileNotFoundError, match="--init"):
        config_module.ZENConfig.from_env()


def test_explicit_relative_config_uses_working_directory(
    user_config_dir, tmp_path, monkeypatch
) -> None:
    """An explicit non-default path is resolved relative to the caller."""
    path = tmp_path / "gateway.ini"
    path.write_text("[api]\n", encoding="utf-8")
    monkeypatch.setenv("ZEN_CONFIG_FILE", "gateway.ini")
    assert config_module.ZENConfig.from_env().config_file == path


def test_runtime_settings_preserve_process_environment(
    user_config_dir, monkeypatch
) -> None:
    """User dotenv settings fill missing values but never override exports."""
    user_config_dir.mkdir()
    settings = user_config_dir / ".env"
    settings.write_text(
        "ZEN_EXP_NAME=dotenv\nZEN_OUTPUT_DIR=output\n", encoding="utf-8"
    )
    monkeypatch.setenv("ZEN_EXP_NAME", "process")
    monkeypatch.delenv("ZEN_OUTPUT_DIR", raising=False)
    assert config_module.load_runtime_settings() == settings
    assert config_module.os.environ["ZEN_EXP_NAME"] == "process"
    assert config_module.os.environ["ZEN_OUTPUT_DIR"] == "output"


def test_explicit_settings_path_is_checked(
    user_config_dir, tmp_path, monkeypatch
) -> None:
    """A misspelled explicit dotenv path fails visibly."""
    monkeypatch.setenv("ZEN_ENV_FILE", str(tmp_path / "missing.env"))
    with pytest.raises(FileNotFoundError, match="Settings file not found"):
        config_module.load_runtime_settings()
