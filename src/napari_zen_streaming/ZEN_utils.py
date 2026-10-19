"""Extract streamed metadata and prepare normalized display frames."""

import logging
from typing import Any

import numpy as np
from numpy.typing import DTypeLike

from napari_zen_streaming.ZEN_config import ImageMetadata

logger = logging.getLogger(__name__)


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


def extract_image_data(
    response: Any, dtype: np.dtype, shape: tuple[int, int]
) -> np.ndarray:
    """
    Extract raw image data from ZEN API response.

    Converts raw binary pixel data into a properly shaped numpy array.

    Args:
        response: ZEN API response object containing pixel_data.raw_data
        dtype: Expected data type of pixel data (e.g., np.uint16)
        shape: Expected image shape as (height, width)

    Returns:
        2D numpy array with raw image data

    Note:
        The raw_data is a binary buffer that needs to be:
        1. Interpreted with the correct dtype (e.g., uint16)
        2. Reshaped to match image dimensions (height, width)
    """
    raw_data = response.frame_data.pixel_data.raw_data

    image = np.frombuffer(raw_data, dtype=dtype)

    return image.reshape(shape)


def normalize_for_display(
    image: np.ndarray, target_dtype: DTypeLike = np.uint8
) -> np.ndarray:
    """
    Normalize image to target dtype range for display.

    Performs min-max normalization to map image values to the full range
    of the target data type (e.g., 0-255 for uint8, 0-65535 for uint16).

    This changes intensities per frame and is intended for preview, not
    quantitative measurement. The OME-ZARR writer preserves raw intensities.

    Args:
        image: Input image array (any dtype)
        target_dtype: Target data type for display (default: uint8)

    Returns:
        Normalized image array with target dtype

    Edge Cases:
        - Empty image: Returns empty array with target dtype
        - Constant image (min == max): Returns zeros to avoid division by zero

    Note:
        For microscopy data, uint16 is often preferred over uint8
        to preserve more dynamic range, but uint8 uses less memory.
    """
    target_dtype = np.dtype(target_dtype)
    if image.size == 0:
        return image.astype(target_dtype)

    img_min = np.min(image)
    img_max = np.max(image)

    if img_min == img_max:
        logger.debug("Constant image detected (min == max), returning zeros")
        return np.zeros_like(image, dtype=target_dtype)

    normalized = (image.astype(np.float64) - float(img_min)) / (
        float(img_max) - float(img_min)
    )
    if np.issubdtype(target_dtype, np.integer):
        normalized *= np.iinfo(target_dtype).max
    return normalized.astype(target_dtype)


def process_frame(
    response: Any, pixel_dtype: np.dtype, display_dtype: np.dtype
) -> tuple[np.ndarray, ImageMetadata]:
    """
    Complete frame processing pipeline.

    This is the main entry point for processing a single frame from ZEN API.
    It orchestrates the complete pipeline:
    1. Extract metadata (dimensions, scaling, position, frame indices)
    2. Extract raw pixel data
    3. Normalize for display

    Args:
        response: ZEN API response object containing frame_data
        pixel_dtype: Data type of raw pixel data (e.g., np.uint16 for 16-bit cameras)
        display_dtype: Target data type for display (e.g., np.uint8 for memory efficiency)

    Returns:
        Tuple of (normalized_image, metadata)
        - normalized_image: 2D array ready for display in Napari
        - metadata: ImageMetadata object with all frame information

    """
    metadata = extract_metadata(response)

    raw_image = extract_image_data(response, pixel_dtype, metadata.shape)

    display_image = normalize_for_display(raw_image, display_dtype)

    return display_image, metadata
