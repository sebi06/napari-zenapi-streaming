# Changelog

## Unreleased

### Added

- Stream-only viewing in both Display workflows: show incoming native pixels
  with channel filtering, without retaining acquisition history or assembling
  a final mosaic. The final frame remains visible.

### Changed

- Require OME-Zarr 0.6 or newer for reading; reject older and prerelease
  metadata with an explicit conversion message.
- Finalize all streaming output as OME-Zarr 0.6 using a real coordinate-model
  conversion, including collection/HCS containers and retained source tiles.
  Finalization preserves pixel chunks and custom provenance.
- Generate 0.6 pyramids and read axes/scales from coordinate-system metadata.
- Require `ngff-zarr>=0.51.0` for released 0.6 conversion and validation.

### Fixed

- Read Display-mode pixel types from the API's nested
  `frame_data.pixel_data.pixel_type` field. Regression tests now use actual
  ZEN API frame messages for both GRAY8 and GRAY16.
- Open completed OME-ZARR collections from their advertised image paths,
  avoiding a `napari-ome-zarr` collection-reader crash when companion XML is
  absent. Preserve reader-provided channel-aware axis labels and existing
  user-layer names.
- Experiment controls scroll automatically when the plugin dock is too small
  to show all settings.
- Display mode preserves runtime GRAY8/GRAY16 pixel types and original
  intensities through live preview and final scene assembly, instead of
  independently normalizing tiles to 8-bit.
- Final display contrast uses the whole channel mosaic, and full-history
  live updates no longer reset contrast for each incoming tile.

## 0.0.1 - 2026-11-01

Initial alpha release.

### Added

- Live ZEN API streaming into napari, with watch and start-selected modes.
- Latest-frame preview and per-channel scene mosaics.
- Configured OME-Zarr streaming through the plugin and standalone CLI.
- Automatic experiment startup or an armed writer for manual startup in ZEN.
- HCS plate output from exported well metadata, with generic layout fallback.
- Raw GRAY8/GRAY16 output, compression, scene mosaics, and optional pyramids.
- Acquisition completeness reports and preservation of partial output.
- User-owned gateway configuration and logging.
- Output-folder access checks before connecting or starting acquisition.
- Python streaming example with experiment selection and startup overrides.
- Python 3.12-3.14 support and PyQt6-based cross-platform CI.

### Known Limitations

- Live acquisition requires ZEN Blue and its ZEN API Gateway on Windows.
- Compatible `zen_api` and `zen_api_utils` wheels must be installed separately.
- OME-Zarr streaming supports only 2D GRAY8 and GRAY16 frames.
- Display mode retains frames in memory and normalizes preview intensities;
  use configured OME-Zarr output for raw quantitative data.
- Remote Linux clients are experimental; automated tests do not establish
  acquisition throughput or completeness on real hardware.
