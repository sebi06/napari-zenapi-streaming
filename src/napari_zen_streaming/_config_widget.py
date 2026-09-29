import configparser
from pathlib import Path

import napari
from magicgui import magicgui
from qtpy.QtWidgets import QMessageBox

CONFIG_PATH = Path.cwd() / "config.ini"

def load_defaults():
    defaults = {
        "api_host": "127.0.0.1",
        "api_port": 5000,
        "cert_file": r"C:\ProgramData\Carl Zeiss\ZenApiGateway\Certificates\ZenApiPersonalSigningRootCA.pem",
        "control_token": "...",
        "stream_host": "127.0.0.1",
        "stream_port": 5280,
    }
    if CONFIG_PATH.exists():
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_PATH)
        defaults["api_host"] = cfg.get("api", "host", fallback=defaults["api_host"])
        defaults["api_port"] = cfg.getint("api", "port", fallback=defaults["api_port"])
        defaults["cert_file"] = cfg.get("api", "cert_file", fallback=defaults["cert_file"])
        defaults["control_token"] = cfg.get("api", "control-token", fallback=defaults["control_token"])
        defaults["stream_host"] = cfg.get("image_streaming", "host", fallback=defaults["stream_host"])
        defaults["stream_port"] = cfg.getint("image_streaming", "port", fallback=defaults["stream_port"])
    return defaults

def create_config_widget():
    defaults = load_defaults()

    @magicgui(
        call_button="Generate config.ini",
        api_host={"label": "API Host", "value": defaults["api_host"]},
        api_port={"label": "API Port", "value": defaults["api_port"], "min": 1, "max": 65535, "step": 1},
        cert_file={
            "label": "Cert File",
            "value": defaults["cert_file"],
            "mode": "r",  # File picker for reading existing files
            "filter": "*.pem;;*"  # Filter for PEM files, but allow all files
        },
        control_token={"label": "Control Token", "value": defaults["control_token"]},
        stream_host={"label": "Stream Host", "value": defaults["stream_host"]},
        stream_port={"label": "Stream Port", "value": defaults["stream_port"], "min": 1, "max": 65535, "step": 1},
    )
    def config_widget(
        api_host: str,
        api_port: int,
        cert_file: Path,  # Changed to Path for file picker
        control_token: str,
        stream_host: str,
        stream_port: int
    ):
        cfg = configparser.ConfigParser()
        cfg["api"] = {
            "host": api_host,
            "port": str(api_port),
            "cert_file": str(cert_file),  # Convert Path to string for config
            "control-token": control_token,
        }
        cfg["image_streaming"] = {
            "host": stream_host,
            "port": str(stream_port),
        }
        with open(CONFIG_PATH, "w") as f:
            cfg.write(f)

        msg = QMessageBox()
        msg.setWindowTitle("Success")
        msg.setText(f"config.ini saved successfully to:\n{CONFIG_PATH}")
        msg.setIcon(QMessageBox.Information)
        msg.exec_()

        viewer = napari.current_viewer()
        viewer.window.remove_dock_widget("all")
    return config_widget

# For napari.yaml compatibility - this creates the widget instance
ConfigWidget = create_config_widget()
