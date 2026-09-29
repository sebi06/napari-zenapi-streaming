"""Tests for streamed tile placement in scene mosaics."""

import numpy as np
import pytest

from napari_zen_streaming._scene_geometry import (
    TileGeometry,
    assemble_channel_scene_mosaics,
    calculate_scene_geometry,
)


def _tile(
    tile_index: int,
    center_x: float,
    center_y: float,
    *,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
) -> TileGeometry:
    """Build a 10 by 10 tile for geometry tests."""
    return TileGeometry(
        scene_index=0,
        tile_index=tile_index,
        position_index=tile_index,
        center_x=center_x,
        center_y=center_y,
        position_z=0.0,
        scale_x=scale_x,
        scale_y=scale_y,
        height=10,
        width=10,
    )


def test_calculate_scene_geometry_places_overlapping_tiles() -> None:
    """Overlapping tiles share pixels without enlarging both footprints."""
    geometry = calculate_scene_geometry([_tile(0, 4.5, 4.5), _tile(1, 12.5, 4.5)])

    assert (geometry.height, geometry.width) == (10, 18)
    assert geometry.origin_y == pytest.approx(0.0)
    assert geometry.origin_x == pytest.approx(0.0)
    assert [(tile.y_offset, tile.x_offset) for tile in geometry.tiles] == [
        (0, 0),
        (0, 8),
    ]


def test_calculate_scene_geometry_preserves_irregular_gaps() -> None:
    """Stage-position gaps remain empty space in the mosaic canvas."""
    geometry = calculate_scene_geometry([_tile(0, 4.5, 4.5), _tile(1, 19.5, 16.5)])

    assert (geometry.height, geometry.width) == (22, 25)
    assert [(tile.y_offset, tile.x_offset) for tile in geometry.tiles] == [
        (0, 0),
        (12, 15),
    ]
    assert geometry.tiles[1].translation_y == pytest.approx(12.0)
    assert geometry.tiles[1].translation_x == pytest.approx(15.0)


def test_calculate_scene_geometry_rejects_mixed_pixel_scales() -> None:
    """A single mosaic cannot represent tiles sampled on different grids."""
    with pytest.raises(ValueError, match="same pixel scale"):
        calculate_scene_geometry(
            [
                _tile(0, 4.5, 4.5),
                _tile(1, 14.5, 4.5, scale_x=0.5),
            ]
        )


def test_assemble_channel_scene_mosaics_merges_tiles_per_scene() -> None:
    """M tiles merge per scene and the higher M index wins overlaps."""
    geometries = {
        (0, 0): TileGeometry(0, 0, 0, 1.5, 1.5, 0.0, 1.0, 1.0, 4, 4),
        (0, 1): TileGeometry(0, 1, 1, 4.5, 1.5, 0.0, 1.0, 1.0, 4, 4),
        (1, 0): TileGeometry(1, 0, 2, 1.5, 1.5, 0.0, 1.0, 1.0, 4, 4),
        (1, 1): TileGeometry(1, 1, 3, 1.5, 6.5, 0.0, 1.0, 1.0, 4, 4),
    }
    frames = [
        (0, 0, 0, 0, np.full((4, 4), 2, dtype=np.uint16)),
        (0, 0, 1, 0, np.full((4, 4), 4, dtype=np.uint16)),
        (1, 0, 0, 0, np.full((4, 4), 6, dtype=np.uint16)),
        (1, 0, 1, 0, np.full((4, 4), 8, dtype=np.uint16)),
    ]

    result = assemble_channel_scene_mosaics(frames, geometries)

    assert result.shape == (2, 1, 1, 9, 7)
    assert np.all(result[0, 0, 0, :4, 3] == 4)
    assert np.all(result[1, 0, 0, 4, :] == 0)
    assert np.all(result[1, 0, 0, 5:, :4] == 8)
