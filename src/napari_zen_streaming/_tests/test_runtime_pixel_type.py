"""Tests for runtime ZEN pixel-type handling."""

import numpy as np
import pytest
from zen_api.acquisition.v1beta import PixelType

from napari_zen_streaming.ZEN_stream2omezarr import (
    _decode_grayscale_frame,
    _runtime_grayscale_dtype,
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
