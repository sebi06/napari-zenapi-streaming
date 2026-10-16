#################################################################
# File        : misc.py
# Author      : SRh, JSm
# Institution : Carl Zeiss Microscopy GmbH
#
#
# Copyright(c) 2025 Carl Zeiss AG, Germany. All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################


import configparser
import ssl
from pathlib import Path

from grpclib.client import Channel


def initialize_zenapi(
    config_file: str | Path = "config.ini",
) -> tuple[Channel, list[tuple[str, str]]]:
    """
    Create a TLS-verified gRPC channel using the gateway configuration.

    Args:
        config_file (str | Path): Path to the configuration file. Can be a string or Path object.
                                   If a relative path is provided, it will be resolved relative
                                   to the current working directory. Defaults to "config.ini".

    Returns:
        Tuple consisting of gRPC Channel and metadata as a list of tuples.

    Raises:
        FileNotFoundError: If the configuration file does not exist.
    """

    config_path = Path(config_file).resolve()

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    config = configparser.ConfigParser()
    config.read(config_path)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    cert_file = Path(config["api"]["cert_file"])
    if not cert_file.is_absolute():
        cert_file = (config_path.parent / cert_file).resolve()

    context.load_verify_locations(cafile=str(cert_file))
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True

    context.set_alpn_protocols(["h2"])
    channel = Channel(
        host=config["api"]["host"],
        port=int(config["api"]["port"]),
        ssl=context,
    )
    metadata = [("control-token", config["api"]["control-token"])]

    return channel, metadata
