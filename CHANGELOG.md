# Changelog

## 0.0.5 - 2026-11-25

### Fixed

- Fail Display acquisition startup promptly when the pixel reader fails or
  closes before becoming ready, rather than waiting indefinitely.
- Report reader and stream-close failures, including errors from readers that
  have already exited. Drain accepted frames and stop the frame processor
  even when reader cleanup fails.

### Changed

- Clarify Display-reader lifecycle types and remove an unused viewer callback.
  Add regression tests for failed subscriptions, early stream completion,
  stream-close failures, and completed-reader error reporting.
- Require `zen_api_utils>=0.2.0` and remove the plugin's `zen_api` upper bound.
  Gateway and installed-utilities compatibility constraints still apply.
- Align CI and installation examples with the public `zen_api==2026.11.1`
  and `zen_api_utils==0.2.0` wheels, and add OME-Zarr package keywords.

## 0.0.4 - 2026-11-18

### Fixed

- Carry active-channel names and actual ZEN XML colors through plugin and
  configured CLI OME-Zarr acquisition. Persist per-image OMERO display
  settings in stream/subset order for generic scenes, HCS wells, and retained
  source tiles, and preserve them when generating pyramids so
  `napari-ome-zarr` automatically restores channel colors. Missing or invalid
  XML colors warn and fall back to neutral grayscale.

## 0.0.3 - 2026-11-18

### Fixed

- Closing or removing the Experiment Selector dock now closes its pixel
  stream subscriptions and cancels owned background tasks, including the
  dedicated OME-ZARR writer. The ZEN acquisition and napari remain open.
- Start-selected Display mode no longer subscribes before Start is clicked.
  The selected experiment ID is armed before subscription and acquisition,
  retained through trailing-frame delivery, and cleared after finalization
  when the reader returns to an unsubscribed idle state.
- Mode and channel-filter changes no longer arm an idle selected-experiment
  reader. Passive watch mode continues to subscribe immediately.
- Serialize reader transitions to prevent duplicate listeners during
  overlapping shutdown, mode changes, and repeated acquisitions.

### Added

- Stream-lifecycle regression tests covering idle modes, mode and channel
  changes, experiment filtering, repeated runs, failed starts, dock closure
  and removal, dedicated-writer cancellation, and embedded worker shutdown.

## 0.0.2 - 2026-11-17

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
