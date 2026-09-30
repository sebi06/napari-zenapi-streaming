"""Tests for experiment-isolated display streaming."""

import asyncio
from types import SimpleNamespace

import pytest

from napari_zen_streaming.ZEN_pipeline import StreamingPipeline


def _response(experiment_id: str) -> SimpleNamespace:
    """Create a minimal streamed-frame response."""
    return SimpleNamespace(
        frame_data=SimpleNamespace(experiment_id=experiment_id),
    )


def test_pipeline_filters_only_when_target_is_armed() -> None:
    """ZEN-started mode accepts all frames; napari-started mode is isolated."""
    pipeline = object.__new__(StreamingPipeline)
    pipeline._target_experiment_id = None

    assert pipeline._accepts_response(_response("zen-started"))

    pipeline.set_target_experiment("napari-started")

    assert pipeline._accepts_response(_response("napari-started"))
    assert not pipeline._accepts_response(_response("other-experiment"))


def test_target_is_armed_before_loaded_experiment_starts() -> None:
    """The global reader is ready and filtered before acquisition begins."""
    events: list[str] = []
    pipeline = object.__new__(StreamingPipeline)
    pipeline._target_experiment_id = None

    async def ensure_reader_ready() -> None:
        events.append("reader-ready")

    class Connection:
        async def prepare_experiment(
            self,
            experiment_name: str,
            overwrite: bool = True,
        ) -> str:
            events.append(f"prepared:{experiment_name}:{overwrite}")
            return "experiment-42"

        async def start_loaded_experiment(
            self,
            experiment_id: str,
            output_name: str,
        ) -> None:
            events.append(f"started:{experiment_id}:{output_name}:" f"{pipeline._target_experiment_id}")

    pipeline.connection = Connection()
    pipeline.ensure_reader_ready = ensure_reader_ready

    experiment_id = asyncio.run(pipeline.start_targeted_experiment("Z Stack"))

    assert experiment_id == "experiment-42"
    assert events == [
        "reader-ready",
        "prepared:Z Stack:True",
        "started:experiment-42:Z Stack:experiment-42",
    ]


def test_failed_start_clears_experiment_filter() -> None:
    """A failed acquisition start must restore global ZEN-started monitoring."""
    pipeline = object.__new__(StreamingPipeline)
    pipeline._target_experiment_id = None

    async def ensure_reader_ready() -> None:
        return None

    class Connection:
        async def prepare_experiment(
            self,
            experiment_name: str,
            overwrite: bool = True,
        ) -> str:
            return "experiment-42"

        async def start_loaded_experiment(
            self,
            experiment_id: str,
            output_name: str,
        ) -> None:
            raise RuntimeError("start failed")

    pipeline.connection = Connection()
    pipeline.ensure_reader_ready = ensure_reader_ready

    with pytest.raises(RuntimeError, match="start failed"):
        asyncio.run(pipeline.start_targeted_experiment("Z Stack"))

    assert pipeline._target_experiment_id is None
