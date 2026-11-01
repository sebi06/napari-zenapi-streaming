"""Stream a configured ZEN experiment to OME-ZARR without opening napari."""

import argparse
import asyncio
import logging
from pathlib import Path

from zen_api.acquisition.v1beta import ExperimentServiceStub

from napari_zen_streaming._logging import configure_logging
from napari_zen_streaming.misc import initialize_zenapi
from napari_zen_streaming.ZEN_config import load_runtime_settings
from napari_zen_streaming.ZEN_omezarr import (
    load_experiment_acquisition,
    load_experiment_config,
)
from napari_zen_streaming.ZEN_stream2omezarr import (
    stream_to_omezarr_with_config,
)

logger = logging.getLogger(__name__)


async def run(
    config_path: Path,
    start_experiment: bool,
    experiment_name: str | None = None,
) -> Path:
    """Export acquisition metadata, then stream raw frames to OME-ZARR.

    Args:
        config_path: Experiment INI containing the setup and output settings.
        start_experiment: Start via ZEN API instead of waiting for ZEN UI.
        experiment_name: Override the INI experiment name before metadata export.

    Returns:
        Path to the completed OME-ZARR store.

    Raises:
        IncompleteAcquisitionError: Frames are missing; partial output remains.
    """
    config = load_experiment_config(config_path)
    if experiment_name is not None:
        config.experiment_name = experiment_name
    config.start_from_script = start_experiment
    config.overwrite_czi = False
    config.overwrite_zarr = False

    channel, metadata = initialize_zenapi(config.zenapi_config)
    try:
        service = ExperimentServiceStub(channel=channel, metadata=metadata)
        acquisition = await load_experiment_acquisition(
            service, config.experiment_name
        )
    finally:
        channel.close()

    config.time_points = acquisition.time_points
    config.channels = acquisition.channels
    config.z_planes = acquisition.z_planes
    config.z_spacing = acquisition.z_spacing
    config.tiles = acquisition.tiles
    config.scenes = acquisition.scenes
    config.positions = list(acquisition.positions)

    return await stream_to_omezarr_with_config(config, inactivity_timeout=30.0)


def main() -> None:
    """Run the example with an experiment INI and optional API startup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "experiment_config",
        type=Path,
        nargs="?",
        default=Path(__file__).resolve().parents[1]
        / "experiment_streaming_config.ini",
        help="Experiment INI (default: repository experiment_streaming_config.ini).",
    )
    parser.add_argument(
        "--experiment",
        metavar="NAME",
        help="Override the INI experiment name (without .czexp).",
    )
    parser.add_argument(
        "--start-experiment",
        action="store_true",
        help="Start the selected experiment via ZEN API; otherwise wait for ZEN UI.",
    )
    args = parser.parse_args()
    load_runtime_settings()
    configure_logging()
    output = asyncio.run(
        run(args.experiment_config, args.start_experiment, args.experiment)
    )
    logger.info("Completed OME-ZARR: %s", output)


if __name__ == "__main__":
    main()
