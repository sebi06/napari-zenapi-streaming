"""Tests for runtime ZEN pixel-type handling."""

import asyncio

import numpy as np
import pytest
from zen_api.acquisition.v1beta import (
    ExperimentStreamingServiceMonitorAllExperimentsResponse,
    FrameData,
    FramePixelData,
    PixelType,
)

from napari_zen_streaming._scene_geometry import (
    TileGeometry,
    assemble_channel_scene_mosaics,
)
from napari_zen_streaming.ZEN_pipeline import StreamingPipeline
from napari_zen_streaming.ZEN_utils import (
    _decode_grayscale_frame,
    _runtime_grayscale_dtype,
)


@pytest.mark.parametrize(
    ("pixel_type", "dtype", "signal"),
    [
        (PixelType.GRAY8, np.uint8, 200),
        (PixelType.GRAY16, np.uint16, 3100),
    ],
)
def test_display_pipeline_preserves_pixels_through_mosaic(
    pixel_type, dtype, signal
) -> None:
    """Different tile ranges and constant tiles retain their original values."""
    images = [
        np.array([[100, 110, 120], [130, 120, 110]], dtype=dtype),
        np.array([[100, signal, 120], [130, 120, 110]], dtype=dtype),
        np.full((2, 3), 100, dtype=dtype),
    ]
    frames = []
    geometries = {}
    pipeline = object.__new__(StreamingPipeline)

    class Viewer:
        async def add_frame(self, image, metadata) -> None:
            frames.append((0, 0, metadata.frame_m, 0, image))

    pipeline.viewer = Viewer()
    pipeline.frames_processed = 0
    pipeline.stop_event = asyncio.Event()
    pipeline.stop_event.set()
    pipeline.queue = asyncio.Queue()
    for tile, image in enumerate(images):
        frame_data = FrameData(
            pixel_data=FramePixelData(
                pixel_type=pixel_type, raw_data=image.tobytes()
            )
        )
        frame_data.frame_size.width = 3
        frame_data.frame_size.height = 2
        frame_data.scaling.x = 1e-6
        frame_data.scaling.y = 1e-6
        frame_data.frame_stage_position.x = tile * 3e-6
        frame_data.frame_stage_position.y = 0.0
        frame_data.frame_stage_position.z = 0.0
        frame_data.frame_position.s = 0
        frame_data.frame_position.t = 0
        frame_data.frame_position.m = tile
        frame_data.frame_position.c = 0
        frame_data.frame_position.h = 0
        frame_data.frame_position.z = 0
        pipeline.queue.put_nowait(
            ExperimentStreamingServiceMonitorAllExperimentsResponse(
                frame_data=frame_data
            )
        )
        geometries[(0, tile)] = TileGeometry(
            0, tile, tile, tile * 3.0, 0.0, 0.0, 1.0, 1.0, 2, 3
        )

    asyncio.run(pipeline._process_frames())

    assert pipeline.frames_processed == len(images)
    for (_, _, _, _, actual), expected in zip(frames, images, strict=True):
        assert actual.dtype == expected.dtype
        np.testing.assert_array_equal(actual, expected)
    mosaic = assemble_channel_scene_mosaics(frames, geometries)
    assert mosaic.dtype == np.dtype(dtype)
    np.testing.assert_array_equal(
        mosaic[0, 0, 0], np.concatenate(images, axis=1)
    )


@pytest.mark.parametrize(
    ("pixel_type", "bytes_per_pixel", "expected"),
    [
        (PixelType.GRAY8, 1, np.dtype(np.uint8)),
        (PixelType.GRAY16, 2, np.dtype(np.uint16)),
    ],
)
def test_runtime_grayscale_dtype(
    pixel_type: PixelType,
    bytes_per_pixel: int,
    expected: np.dtype,
) -> None:
    """Runtime pixel metadata selects the matching NumPy dtype."""
    assert (
        _runtime_grayscale_dtype(
            pixel_type,
            payload_size=12 * bytes_per_pixel,
            width=4,
            height=3,
        )
        == expected
    )


def test_runtime_grayscale_dtype_rejects_bad_payload_size() -> None:
    """A malformed payload cannot be silently decoded with the wrong dtype."""
    with pytest.raises(ValueError, match="payload size"):
        _runtime_grayscale_dtype(
            PixelType.GRAY16,
            payload_size=12,
            width=4,
            height=3,
        )


def test_runtime_grayscale_dtype_rejects_color_payloads() -> None:
    """Color payloads require a different OME-ZARR dimension model."""
    with pytest.raises(ValueError, match="BGR24"):
        _runtime_grayscale_dtype(
            PixelType.BGR24,
            payload_size=36,
            width=4,
            height=3,
        )


def test_decode_grayscale_frame_preserves_native_yx_order() -> None:
    """A non-square ZEN frame keeps height on Y and width on X."""
    expected = np.array(
        [
            [0, 1, 2],
            [3, 4, 5],
        ],
        dtype=np.uint16,
    )

    frame = _decode_grayscale_frame(
        expected.tobytes(),
        expected.dtype,
        width=3,
        height=2,
    )

    np.testing.assert_array_equal(frame, expected)
    assert frame.shape == (2, 3)
    assert frame.flags.c_contiguous
