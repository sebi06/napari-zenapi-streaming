"""Extract streamed metadata and preserve native grayscale pixel values."""

import logging
from typing import Any

import numpy as np
from zen_api.acquisition.v1beta import PixelType

from napari_zen_streaming.ZEN_config import ImageMetadata

logger = logging.getLogger(__name__)

_GRAYSCALE_PIXEL_DTYPES: dict[PixelType, np.dtype] = {
    PixelType.GRAY8: np.dtype(np.uint8),
    PixelType.GRAY16: np.dtype(np.uint16),
}


def _runtime_grayscale_dtype(
    pixel_type: PixelType,
    payload_size: int,
    width: int,
    height: int,
) -> np.dtype:
    """Validate the payload size and return its native grayscale dtype."""
    try:
        dtype = _GRAYSCALE_PIXEL_DTYPES[pixel_type]
    except KeyError as exc:
        name = getattr(pixel_type, "name", str(pixel_type))
        raise ValueError(f"Unsupported streamed pixel type: {name}") from exc

    expected_size = width * height * dtype.itemsize
    if payload_size != expected_size:
        raise ValueError(
            "Streamed pixel payload size does not match its runtime "
            f"metadata: got {payload_size} bytes, expected "
            f"{expected_size} for {width}x{height} {pixel_type.name}."
        )
    return dtype


def _decode_grayscale_frame(
    raw_data: bytes,
    dtype: np.dtype,
    width: int,
    height: int,
) -> np.ndarray:
    """Decode native pixels in YX order without rotation or normalization."""
    frame = np.frombuffer(raw_data, dtype=dtype).reshape((height, width))
    return np.ascontiguousarray(frame)


def extract_metadata(response: Any) -> ImageMetadata:
    """
    Extract image metadata from ZEN API response.

    Extracts and converts all relevant metadata:
    - Image dimensions (width, height)
    - Pixel scaling (converted from meters to micrometers)
    - Stage position (converted from meters to micrometers)
    - Frame position in multidimensional dataset (S, T, M, C, H, Z)

    Args:
        response: ZEN API response object containing frame_data with metadata

    Returns:
        ImageMetadata object with all extracted information

    Note:
        Spatial measurements are converted to micrometers. Stage positions
        are rounded to three decimal places; pixel scales are not rounded.
    """
    frame_data = response.frame_data
    size = frame_data.frame_size
    scaling = frame_data.scaling
    stage_pos = frame_data.frame_stage_position
    frame_pos = frame_data.frame_position

    metadata = ImageMetadata(
        width=size.width,
        height=size.height,
        scaling_x_um=scaling.x * 1e6,
        scaling_y_um=scaling.y * 1e6,
        stage_x_um=round(stage_pos.x * 1e6, 3),
        stage_y_um=round(stage_pos.y * 1e6, 3),
        stage_z_um=round(stage_pos.z * 1e6, 3),
        frame_s=frame_pos.s,  # Scene/Region
        frame_m=frame_pos.m,  # Mosaic tile
        frame_c=frame_pos.c,  # Channel
        frame_h=frame_pos.h,  # Phase (not commonly used)
        frame_t=frame_pos.t,  # Time point
        frame_z=frame_pos.z,  # Z-slice
    )

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Frame %sx%s, X scaling %s um",
            metadata.width,
            metadata.height,
            metadata.scaling_x_um,
        )

    return metadata


def extract_image_data(response: Any) -> np.ndarray:
    """Decode a frame using its runtime PixelType, preserving intensities."""
    frame_data = response.frame_data
    raw_data = frame_data.pixel_data.raw_data
    size = frame_data.frame_size
    dtype = _runtime_grayscale_dtype(
        frame_data.pixel_data.pixel_type,
        len(raw_data),
        size.width,
        size.height,
    )
    return _decode_grayscale_frame(raw_data, dtype, size.width, size.height)


def process_frame(response: Any) -> tuple[np.ndarray, ImageMetadata]:
    """
    Complete frame processing pipeline.

    This is the main entry point for processing a single frame from ZEN API.
    It orchestrates the complete pipeline:
    1. Extract metadata (dimensions, scaling, position, frame indices)
    2. Extract raw pixel data
    Pixel values and the runtime grayscale dtype are preserved unchanged.

    Args:
        response: ZEN API response object containing frame_data

    Returns:
        Tuple of (image, metadata)
        - image: Native 2D grayscale array ready for display in Napari
        - metadata: ImageMetadata object with all frame information

    """
    metadata = extract_metadata(response)

    return extract_image_data(response), metadata
