"""Tests for Display-mode completion after early ZEN status updates."""

import asyncio
import time
from collections import deque
from types import SimpleNamespace

import pytest

from napari_zen_streaming.ZEN_ui import StreamingViewer


def _frame_key(scene: int, tile: int, z_index: int) -> tuple[int, ...]:
    """Build one key for the 2-scene, 8-tile, 3-Z acquisition."""
    return (scene, 0, tile, z_index, 0)


def test_status_finish_waits_for_trailing_scene_tiles() -> None:
    """The final four scene tiles arriving after status are retained."""
    viewer = object.__new__(StreamingViewer)
    all_keys = {
        _frame_key(scene, tile, z_index)
        for scene in range(2)
        for tile in range(8)
        for z_index in range(3)
    }
    trailing_keys = {_frame_key(1, tile, 2) for tile in range(4, 8)}
    viewer.frame_data = dict.fromkeys(all_keys - trailing_keys)
    viewer.metadata_buffer = deque()
    viewer._selected_experiment_metadata = SimpleNamespace(
        time_points=1,
        channels=1,
        z_planes=3,
        tiles=8,
        scenes=2,
    )
    viewer._effective_channel_index = None
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: 1)
    viewer._mode_index = 1
    viewer._expected_total_frames = 48
    viewer.config = SimpleNamespace(restructure_timeout=0.5)
    viewer.last_frame_time = time.time()

    async def complete_stream() -> int:
        async def append_trailing_frames() -> None:
            await asyncio.sleep(0.05)
            for scene, time_index, tile, z_index, channel in trailing_keys:
                viewer.metadata_buffer.append(
                    SimpleNamespace(
                        frame_s=scene,
                        frame_t=time_index,
                        frame_m=tile,
                        frame_z=z_index,
                        frame_c=channel,
                    )
                )
            viewer.last_frame_time = time.time()

        append_task = asyncio.create_task(append_trailing_frames())
        received = await viewer._wait_for_display_stream_completion()
        await append_task
        return received

    assert asyncio.run(complete_stream()) == 48


def test_stream_only_completion_waits_for_inactivity_without_history() -> None:
    """A channel change or ZEN status cannot truncate trailing preview frames."""
    viewer = object.__new__(StreamingViewer)
    viewer._run_stream_only = True
    viewer._stream_frames_received = 3
    viewer.last_frame_time = time.time()
    viewer.config = SimpleNamespace(restructure_timeout=0.03)

    def reject_frame_keys():
        pytest.fail("stream-only completion requested retained history")

    viewer._received_display_frame_keys = reject_frame_keys

    async def finish() -> int:
        async def trailing_frame() -> None:
            await asyncio.sleep(0.01)
            viewer._stream_frames_received += 1
            viewer.last_frame_time = time.time()

        task = asyncio.create_task(trailing_frame())
        received = await viewer._wait_for_display_stream_completion()
        await task
        return received

    assert asyncio.run(finish()) == 4
