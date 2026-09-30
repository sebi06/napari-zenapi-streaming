"""
Utility functions for ZEN API streaming - pure functions with no side effects.

This module provides stateless functions for processing microscopy image data:
- Metadata extraction from ZEN API responses
- Image data extraction and reshaping
- Normalization for display
- Complete frame processing pipeline

All functions are pure (no side effects) and can be tested independently.
"""

import logging
from typing import Any

import numpy as np

from napari_zen_streaming.ZEN_config import ImageMetadata

logger = logging.getLogger(__name__)

_DTYPE_MAX = {
    np.uint8: 255,
    np.uint16: 65535,
}


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
        All spatial measurements are converted to micrometers (µm) without
        rounding so tile placement retains the streamed coordinate precision.
    """
    frame_data = response.frame_data
    size = frame_data.frame_size
    scaling = frame_data.scaling
    stage_pos = frame_data.frame_stage_position
    frame_pos = frame_data.frame_position
    frame_expID: str = frame_data.experiment_id

    # Create metadata object with converted units
    metadata = ImageMetadata(
        width=size.width,
        height=size.height,
        # Convert scaling from meters to micrometers (1e6 factor)
        scaling_x_um=scaling.x * 1e6,
        scaling_y_um=scaling.y * 1e6,
        # Convert stage position from meters to micrometers
        stage_x_um=round(stage_pos.x * 1e6, 3),
        stage_y_um=round(stage_pos.y * 1e6, 3),
        stage_z_um=round(stage_pos.z * 1e6, 3),
        # Frame indices in multidimensional dataset
        frame_s=frame_pos.s,  # Scene/Region
        frame_m=frame_pos.m,  # Mosaic tile
        frame_c=frame_pos.c,  # Channel
        frame_h=frame_pos.h,  # Phase (not commonly used)
        frame_t=frame_pos.t,  # Time point
        frame_z=frame_pos.z,  # Z-slice
    )

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            f"Frame Metadata SMTCZ:metadata {metadata.width}x{metadata.height} scaling {metadata.scaling_x_um}µm"
        )

    return metadata


def extract_image_data(response: Any, dtype: np.dtype, shape: tuple[int, int]) -> np.ndarray:
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

    # Convert binary buffer to numpy array with correct dtype
    image = np.frombuffer(raw_data, dtype=dtype)

    # Reshape to 2D image (height, width)
    return image.reshape(shape)


def normalize_for_display(image: np.ndarray, target_dtype: np.dtype = np.uint8) -> np.ndarray:
    """
    Normalize image to target dtype range for display.

    Performs min-max normalization to map image values to the full range
    of the target data type (e.g., 0-255 for uint8, 0-65535 for uint16).

    This is essential for proper visualization in Napari, as it ensures
    the image uses the full dynamic range of the display.

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
    # Handle empty image
    if image.size == 0:
        return image.astype(target_dtype)

    # Get min/max for normalization
    img_min = np.min(image)
    img_max = np.max(image)

    # Handle constant image (avoid division by zero)
    if img_min == img_max:
        logger.debug("Constant image detected (min == max), returning zeros")
        return np.zeros_like(image, dtype=target_dtype)

    # Normalize to [0, 1] range first
    normalized = (image - img_min) / (img_max - img_min)

    # Scale to target dtype range
    dt_max = _DTYPE_MAX.get(target_dtype)
    if dt_max is not None:
        return (normalized * dt_max).astype(target_dtype)

    return normalized


def process_frame(response: Any, pixel_dtype: np.dtype, display_dtype: np.dtype) -> tuple[np.ndarray, ImageMetadata]:
    """
    Complete frame processing pipeline.

    This is the main entry point for processing a single frame from ZEN API.
    It orchestrates the complete pipeline:
    1. Extract metadata (dimensions, scaling, position, frame indices)
    2. Extract raw pixel data
    3. Normalize for display

    Pure function with no side effects - safe for parallel processing.

    Args:
        response: ZEN API response object containing frame_data
        pixel_dtype: Data type of raw pixel data (e.g., np.uint16 for 16-bit cameras)
        display_dtype: Target data type for display (e.g., np.uint8 for memory efficiency)

    Returns:
        Tuple of (normalized_image, metadata)
        - normalized_image: 2D array ready for display in Napari
        - metadata: ImageMetadata object with all frame information

    Example:
        >>> response = await streaming_service.monitor_experiment(...)
        >>> image, metadata = process_frame(response, np.uint16, np.uint8)
        >>> print(f"Processed frame at position {metadata.frame_z}")
    """
    # Step 1: Extract metadata (contains image dimensions)
    metadata = extract_metadata(response)

    # Step 2: Extract raw image data using metadata shape
    raw_image = extract_image_data(response, pixel_dtype, metadata.shape)

    # Step 3: Normalize for display
    display_image = normalize_for_display(raw_image, display_dtype)

    return display_image, metadata
