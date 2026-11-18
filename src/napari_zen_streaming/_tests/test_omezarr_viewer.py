"""Tests for opening completed OME-ZARR collections in napari."""

from pathlib import Path
from types import SimpleNamespace

import ngff_zarr as nz
import numpy as np
import pytest
import zarr

from napari_zen_streaming.ZEN_omezarr import (
    ExperimentChannelMetadata,
    open_in_napari_viewer,
    read_omezarr_axis_names,
    read_omezarr_axis_scales,
    write_omezarr_channel_metadata,
)
from napari_zen_streaming.ZEN_stream2omezarr import generate_omezarr_pyramids

reader = pytest.importorskip("napari_ome_zarr.ome_zarr_reader")


@pytest.mark.parametrize("source_channels", [[0, 1], [1]])
@pytest.mark.parametrize("missing_color", [False, True])
def test_reader_uses_persisted_channel_colors_after_pyramids(
    tmp_path: Path, source_channels: list[int], missing_color: bool
) -> None:
    """The real napari reader gets names/colors without viewer overrides."""
    from napari.utils.colormaps import ensure_colormap

    channels = (
        ExperimentChannelMetadata("AF568", "FF1800"),
        ExperimentChannelMetadata(
            "AF488", None if missing_color else "00FF33"
        ),
    )
    shape = (2, 8, 8) if len(source_channels) == 2 else (8, 8)
    dims = ("c", "y", "x") if len(source_channels) == 2 else ("y", "x")
    image = nz.to_ngff_image(
        np.full(shape, 3100, dtype=np.uint16),
        dims=dims,
        name="scene",
    )
    path = tmp_path / "colored.ome.zarr"
    multiscales = nz.to_multiscales(image, scale_factors=[])
    dataset = multiscales.metadata.datasets[0]
    dataset.path = "0"
    for transform in dataset.coordinateTransformations:
        if transform.input is not None:
            transform.input.path = "0"
    nz.to_ome_zarr(
        path,
        multiscales,
        version="0.6",
    )
    write_omezarr_channel_metadata(path, channels, source_channels)
    original_omero = zarr.open_group(path).attrs["ome"]["omero"]

    generate_omezarr_pyramids(path, 1, None)

    assert zarr.open_group(path).attrs["ome"]["omero"] == original_omero
    layer_data = reader.read_ome_zarr(zarr.open_group(path, mode="r"))()
    assert len(layer_data) == len(source_channels)
    for (pyramid, props, kind), source_index in zip(
        layer_data, source_channels, strict=True
    ):
        channel = channels[source_index]
        assert kind == "image"
        assert [array.shape for array in pyramid] == [(8, 8), (4, 4)]
        assert props["name"].endswith(channel.name)
        color = channel.color or "FFFFFF"
        expected = [int(color[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        colormap = ensure_colormap(props["colormap"])
        np.testing.assert_allclose(colormap.map([1.0])[0, :3], expected)
        assert props["contrast_limits"] == [0, 65535]
        assert props["visible"]


def _create_image(root_path: Path, name: str) -> None:
    """Create a small two-channel pyramid with physical axis metadata."""
    image = nz.to_ngff_image(
        np.full((2, 4, 4), 3100, dtype=np.uint16),
        dims=("c", "y", "x"),
        scale={"y": 0.4, "x": 0.4},
        axes_units={"y": "micrometer", "x": "micrometer"},
        name=name,
    )
    multiscales = nz.to_multiscales(
        image, scale_factors=[2], method=nz.Methods.DASK_IMAGE_NEAREST
    )
    nz.to_ome_zarr(root_path / name, multiscales, version="0.6")


@pytest.mark.parametrize("series", [["scene_0"], ["scene_1", "scene_0"]])
def test_open_collection_without_xml_uses_advertised_series(
    tmp_path: Path, series: list[str]
) -> None:
    """Collection loading retains channels and pyramids without reading XML."""
    path = tmp_path / "collection.ome.zarr"
    root = zarr.open_group(path, mode="w")
    root.attrs["ome"] = {"version": "0.6", "bioformats2raw.layout": 3}
    root.create_group("OME").attrs["ome"] = {
        "version": "0.6",
        "series": series,
    }
    for name in [*series, "raw_source"]:
        _create_image(path, name)
    opened = []
    user_layer = SimpleNamespace(name="Existing image")
    viewer = SimpleNamespace(layers=[user_layer])

    def open_image(image_path: str, *, plugin: str) -> None:
        assert plugin == "napari-ome-zarr"
        opened.append(Path(image_path).name)
        layer_data = reader.read_ome_zarr(
            zarr.open_group(image_path, mode="r")
        )()
        assert len(layer_data) == 2
        for pyramid, properties, kind in layer_data:
            assert kind == "image"
            assert [array.shape for array in pyramid] == [(4, 4), (2, 2)]
            assert all(array.dtype == np.dtype(np.uint16) for array in pyramid)
            assert properties["scale"] == [0.4, 0.4]
            assert properties["axis_labels"] == ("y", "x")

    viewer.open = open_image

    open_in_napari_viewer(path, viewer=viewer)

    assert opened == series
    assert user_layer.name == "Existing image"
    assert read_omezarr_axis_names(path) == ("C", "Y", "X")
    assert read_omezarr_axis_scales(path) == {"C": 1.0, "Y": 0.4, "X": 0.4}


@pytest.mark.parametrize("layout", ["image", "plate"])
def test_noncollection_roots_are_opened_unchanged(
    tmp_path: Path, layout: str
) -> None:
    """Direct multiscales and HCS plates still use the reader at their root."""
    path = tmp_path / "output.ome.zarr"
    root = zarr.open_group(path, mode="w")
    root.attrs["ome"] = (
        {"version": "0.6", "multiscales": []}
        if layout == "image"
        else {"version": "0.6", "bioformats2raw.layout": 3, "plate": {}}
    )
    opened = []
    viewer = SimpleNamespace(open=lambda *args, **kwargs: opened.append(args))

    open_in_napari_viewer(path, viewer=viewer)

    assert opened == [(str(path),)]


@pytest.mark.parametrize(
    "series", [[], "scene_0", [str(Path("..") / "external")]]
)
def test_invalid_collection_series_is_reported(tmp_path: Path, series) -> None:
    """Malformed collection metadata must not report a successful opening."""
    path = tmp_path / "invalid.ome.zarr"
    root = zarr.open_group(path, mode="w")
    root.attrs["ome"] = {"version": "0.6", "bioformats2raw.layout": 3}
    root.create_group("OME").attrs["ome"] = {
        "version": "0.6",
        "series": series,
    }
    opened = []
    viewer = SimpleNamespace(open=lambda *args, **kwargs: opened.append(args))

    with pytest.raises(RuntimeError, match="OME series"):
        open_in_napari_viewer(path, viewer=viewer)

    assert not opened


@pytest.mark.parametrize("version", ["0.4", "0.5", "0.6rc0"])
def test_readers_reject_older_and_prerelease_stores(
    tmp_path: Path, version: str
) -> None:
    """Old acquisitions require explicit conversion rather than silent loading."""
    path = tmp_path / "old.ome.zarr"
    zarr.open_group(path, mode="w").attrs["ome"] = {"version": version}
    opened = []
    viewer = SimpleNamespace(open=lambda *args, **kwargs: opened.append(args))

    with pytest.raises(RuntimeError, match="0.6 or newer"):
        open_in_napari_viewer(path, viewer=viewer)
    with pytest.raises(ValueError, match="0.6 or newer"):
        read_omezarr_axis_names(path)
    assert not opened
