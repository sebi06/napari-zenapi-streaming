"""Tests for well-aware HCS layout controls."""

from pathlib import Path
from types import SimpleNamespace

import numpy as np

from napari_zen_streaming.ZEN_omezarr import (
    PLUGIN_DEFAULT_CZI_NAME,
    PLUGIN_DEFAULT_OVERWRITE_CZI,
    PLUGIN_DEFAULT_OVERWRITE_ZARR,
    PLUGIN_DEFAULT_PYRAMID_LEVELS,
    PLUGIN_DEFAULT_SPATIAL_SHARD_SIZE_CHUNKS,
)
from napari_zen_streaming.ZEN_ui import StreamingViewer


class _Checkbox:
    """Record checkbox state without creating Qt widgets."""

    def __init__(self) -> None:
        self.visible = False
        self.enabled = False
        self.checked = True
        self.text = ""

    def setVisible(self, value: bool) -> None:
        self.visible = value

    def setEnabled(self, value: bool) -> None:
        self.enabled = value

    def setChecked(self, value: bool) -> None:
        self.checked = value

    def setText(self, value: str) -> None:
        self.text = value

    def isVisible(self) -> bool:
        return self.visible

    def isChecked(self) -> bool:
        return self.checked


def test_hcs_option_shows_detected_b2_and_c3_wells() -> None:
    """Complete well metadata exposes an HCS option naming both wells."""
    viewer = object.__new__(StreamingViewer)
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: 2)
    viewer.chk_hcs_layout = _Checkbox()
    viewer.chk_keep_source_tiles = _Checkbox()
    viewer._selected_experiment_metadata = SimpleNamespace(
        scenes=2,
        positions=(
            {
                "scene_index": 0,
                "well_id": "B2",
                "well_row": 2,
                "well_column": 2,
            },
            {
                "scene_index": 1,
                "well_id": "C3",
                "well_row": 3,
                "well_column": 3,
            },
        ),
    )

    viewer._update_hcs_layout_option()

    assert viewer.chk_hcs_layout.visible
    assert viewer.chk_hcs_layout.enabled
    assert viewer.chk_hcs_layout.text == ("Write HCS plate layout (B2, C3)")
    assert not viewer.chk_keep_source_tiles.visible


def test_metadata_refresh_preserves_unchecked_hcs_layout() -> None:
    """Fresh XML metadata must not override the user's layout selection."""
    viewer = object.__new__(StreamingViewer)
    viewer.dropdown = SimpleNamespace(currentText=lambda: "B2")
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: 2)
    viewer.chk_hcs_layout = _Checkbox()
    viewer.chk_hcs_layout.checked = False
    viewer.chk_keep_source_tiles = _Checkbox()
    viewer.spin_t = viewer.spin_c = viewer.spin_z = viewer.spin_z_spacing = SimpleNamespace(setValue=lambda value: None)
    viewer.label_tiles = viewer.label_scenes = SimpleNamespace(setValue=lambda value: None)
    viewer._on_dim_changed = lambda: None
    viewer.dim_panel = SimpleNamespace(setVisible=lambda visible: None)
    metadata = SimpleNamespace(
        experiment_name="B2",
        time_points=1,
        channels=1,
        z_planes=81,
        z_spacing=0.27,
        tiles=1,
        scenes=1,
        positions=({"scene_index": 0, "well_id": "B2", "well_row": 2, "well_column": 2},),
    )

    viewer._apply_experiment_metadata(metadata)

    assert viewer.chk_hcs_layout.visible
    assert viewer.chk_hcs_layout.enabled
    assert not viewer.chk_hcs_layout.isChecked()


def test_source_tile_option_shows_for_generic_mosaics() -> None:
    """Generic mosaics expose source retention and default to cleanup."""
    viewer = object.__new__(StreamingViewer)
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: 2)
    viewer.chk_hcs_layout = _Checkbox()
    viewer.chk_keep_source_tiles = _Checkbox()
    viewer.chk_keep_source_tiles.checked = False
    viewer.chk_create_pyramid = _Checkbox()
    viewer.chk_create_pyramid.checked = True
    viewer.spin_pyramid_levels = SimpleNamespace(value=lambda: 2)
    viewer._selected_experiment_metadata = SimpleNamespace(
        scenes=2,
        positions=(),
    )

    viewer._update_hcs_layout_option()

    assert not viewer.chk_hcs_layout.visible
    assert viewer.chk_keep_source_tiles.visible
    assert viewer.chk_keep_source_tiles.enabled
    assert not viewer.chk_keep_source_tiles.checked


def test_plugin_config_uses_ui_and_fixed_defaults() -> None:
    """The plugin builds a run config without an experiment INI."""
    viewer = object.__new__(StreamingViewer)
    viewer.config = SimpleNamespace(
        pixel_dtype=np.dtype(np.uint16),
        config_file=Path("gateway.ini"),
    )
    viewer.omezarr_dir_edit = SimpleNamespace(text=lambda: "F:/output")
    viewer.compression_combo = SimpleNamespace(currentText=lambda: "blosc-zstd")
    viewer.chk_auto_trigger = SimpleNamespace(isChecked=lambda: False)
    viewer.chk_hcs_layout = _Checkbox()
    viewer.chk_hcs_layout.visible = True
    viewer.chk_keep_source_tiles = _Checkbox()
    viewer.chk_keep_source_tiles.checked = False
    viewer.chk_create_pyramid = _Checkbox()
    viewer.chk_create_pyramid.checked = True
    viewer.spin_pyramid_levels = SimpleNamespace(value=lambda: 2)

    config = viewer._create_plugin_omezarr_config("B2_C3")

    assert config.experiment_name == "B2_C3"
    assert config.output_dir == "F:/output"
    assert config.dtype == "uint16"
    assert config.compression == "blosc-zstd"
    assert config.zenapi_config == "gateway.ini"
    assert not config.start_from_script
    assert config.use_hcs_layout
    assert not config.keep_source_tiles
    assert config.pyramid_levels == 2
    assert config.channel_index is None
    assert config.czi_name == PLUGIN_DEFAULT_CZI_NAME
    assert config.overwrite_czi is PLUGIN_DEFAULT_OVERWRITE_CZI
    assert config.overwrite_zarr is PLUGIN_DEFAULT_OVERWRITE_ZARR
    assert config.spatial_shard_size_chunks == PLUGIN_DEFAULT_SPATIAL_SHARD_SIZE_CHUNKS
    assert PLUGIN_DEFAULT_PYRAMID_LEVELS == 0

    viewer.chk_create_pyramid.checked = False
    disabled_config = viewer._create_plugin_omezarr_config("B2_C3")

    assert disabled_config.pyramid_levels == 0
