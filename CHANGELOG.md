# Changelog

## Unreleased

- Filter configured OME-ZARR frames by experiment ID before decoding.
- Report incomplete acquisitions with a persisted completion report, a CLI
  error, and a plugin warning; retain partial stores without auto-opening them.
- Preserve unrelated napari layers and dock widgets during plugin cleanup.
- Preserve requested display dtypes, including NumPy dtype instances.
- Initialize Qt application state on the main thread and cache worker mode reads.
- Support user-owned gateway configuration and optional dotenv settings outside
  the installed package. Add a non-overwriting configuration initializer.
- Target `main` in CI and enforce Ruff, Black, tests, and package builds.
- Remove the unavailable public wheel URL, unused code, and generated metadata;
  document authorized wheel installation and hardware release checks.