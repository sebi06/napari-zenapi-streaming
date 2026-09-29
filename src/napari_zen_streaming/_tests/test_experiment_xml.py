"""Tests for ZEN experiment acquisition metadata parsing."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from napari_zen_streaming import ZEN_stream2omezarr as streamer
from napari_zen_streaming.ZEN_omezarr import load_experiment_config, parse_experiment_xml


def test_tile_regions_map_to_scenes_and_per_scene_tiles() -> None:
    """Two active 3 by 2 regions describe two scenes of six tiles."""
    xml = """
    <Experiment>
      <ExperimentBlocks>
        <AcquisitionBlock IsActivated="true">
          <SubDimensionSetups>
            <RegionsSetup IsActivated="true">
              <SampleHolder>
                <TileRegions>
                  <TileRegion>
                    <IsUsedForAcquisition>true</IsUsedForAcquisition>
                    <Columns>3</Columns>
                    <Rows>2</Rows>
                  </TileRegion>
                  <TileRegion>
                    <IsUsedForAcquisition>true</IsUsedForAcquisition>
                    <Columns>3</Columns>
                    <Rows>2</Rows>
                  </TileRegion>
                </TileRegions>
              </SampleHolder>
            </RegionsSetup>
          </SubDimensionSetups>
        </AcquisitionBlock>
      </ExperimentBlocks>
    </Experiment>
    """

    metadata = parse_experiment_xml(xml)

    assert metadata.scenes == 2
    assert metadata.tiles == 6


def test_asymmetric_eight_tile_scenes_expect_48_frames() -> None:
    """A 4x2 plus 2x4 acquisition with three Z planes has 48 frames."""
    xml = """
    <Experiment>
      <ExperimentBlocks>
        <AcquisitionBlock IsActivated="true">
          <SubDimensionSetups>
            <RegionsSetup IsActivated="true">
              <SampleHolder>
                <TileRegions>
                  <TileRegion>
                    <IsUsedForAcquisition>true</IsUsedForAcquisition>
                    <Columns>4</Columns>
                    <Rows>2</Rows>
                  </TileRegion>
                  <TileRegion>
                    <IsUsedForAcquisition>true</IsUsedForAcquisition>
                    <Columns>2</Columns>
                    <Rows>4</Rows>
                  </TileRegion>
                </TileRegions>
              </SampleHolder>
            </RegionsSetup>
            <ZStackSetup IsActivated="true">
              <First><Distance><Value>-0.008003</Value></Distance></First>
              <Last><Distance><Value>-0.007997</Value></Distance></Last>
              <Interval><Distance><Value>0.000003</Value></Distance></Interval>
            </ZStackSetup>
          </SubDimensionSetups>
        </AcquisitionBlock>
      </ExperimentBlocks>
    </Experiment>
    """

    metadata = parse_experiment_xml(xml)

    assert metadata.scenes == 2
    assert metadata.tiles == 8
    assert metadata.z_planes == 3
    assert metadata.time_points * metadata.channels * metadata.z_planes * metadata.tiles * metadata.scenes == 48


def test_tile_regions_are_mapped_to_plate_wells() -> None:
    """Explicit ZEN tile-region well identifiers produce HCS positions."""
    xml = """
    <Experiment>
      <ExperimentBlocks>
        <AcquisitionBlock IsActivated="true">
          <SubDimensionSetups>
            <RegionsSetup IsActivated="true">
              <SampleHolder>
                <TileRegions>
                  <TileRegion Name="B2">
                    <CenterPosition>13500,17500</CenterPosition>
                    <Columns>4</Columns><Rows>2</Rows>
                    <TemplateShapeId>2-2</TemplateShapeId>
                    <IsUsedForAcquisition>true</IsUsedForAcquisition>
                  </TileRegion>
                  <TileRegion Name="C3">
                    <CenterPosition>22500,26500</CenterPosition>
                    <Columns>2</Columns><Rows>4</Rows>
                    <TemplateShapeId>3-3</TemplateShapeId>
                    <IsUsedForAcquisition>true</IsUsedForAcquisition>
                  </TileRegion>
                </TileRegions>
                <SingleTileRegionArrays />
                <Template Name="Multichamber 96">
                  <ShapeColumns>12</ShapeColumns>
                  <ShapeRows>8</ShapeRows>
                  <ShapeWidth>7250</ShapeWidth>
                  <ShapeHeight>7250</ShapeHeight>
                  <ShapeDistanceX>9000</ShapeDistanceX>
                  <ShapeDistanceY>9000</ShapeDistanceY>
                </Template>
              </SampleHolder>
            </RegionsSetup>
          </SubDimensionSetups>
        </AcquisitionBlock>
      </ExperimentBlocks>
    </Experiment>
    """

    metadata = parse_experiment_xml(xml)

    assert [position["well_id"] for position in metadata.positions] == [
        "B2",
        "C3",
    ]
    assert [position["scene_index"] for position in metadata.positions] == [
        0,
        1,
    ]


def test_configured_cli_exports_dimensions_before_streaming(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An INI without dimensions still subscribes to every exported channel."""
    config_path = tmp_path / "experiment.ini"
    config_path.write_text(
        "[experiment]\nname = Plate\n[zenapi]\nconfig = gateway.ini\n",
        encoding="utf-8",
    )
    config = load_experiment_config(config_path)
    assert config.channels == 1
    assert not config.positions

    closed: list[bool] = []

    class Channel:
        def close(self) -> None:
            closed.append(True)

    positions = (
        {"scene_index": 0, "well_row": 1, "well_column": 2, "field_index": 1},
        {"scene_index": 1, "well_row": 1, "well_column": 3, "field_index": 1},
    )
    acquisition = SimpleNamespace(
        time_points=2,
        channels=3,
        z_planes=4,
        z_spacing=0.5,
        tiles=2,
        scenes=2,
        positions=positions,
    )
    monkeypatch.setattr(streamer, "initialize_zenapi", lambda path: (Channel(), object()))
    monkeypatch.setattr(streamer, "ExperimentServiceStub", lambda **kwargs: object())

    async def load_metadata(service: object, experiment_name: str) -> SimpleNamespace:
        assert experiment_name == "Plate"
        return acquisition

    async def write_output(ecfg: streamer.ExperimentConfig, inactivity_timeout: float) -> Path:
        assert (ecfg.time_points, ecfg.channels, ecfg.z_planes, ecfg.tiles, ecfg.scenes) == (2, 3, 4, 2, 2)
        assert ecfg.z_spacing == 0.5
        assert ecfg.positions == list(positions)
        assert closed == [True]
        return tmp_path / "output.ome.zarr"

    monkeypatch.setattr(streamer, "load_experiment_acquisition", load_metadata)
    monkeypatch.setattr(streamer, "stream_to_omezarr_with_config", write_output)

    assert asyncio.run(streamer._run_configured_experiment(config, 30.0)) == tmp_path / "output.ome.zarr"
