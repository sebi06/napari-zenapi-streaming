# Changelog

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
