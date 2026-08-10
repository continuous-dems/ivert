# Changelog

All notable changes to IVERT are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `ivert database export` now accepts a single IVERT `.nc` photon granule (exported in its entirety) or the IVERT database index `.nc` file (exported as a polygon layer of per-granule `data_bbox` footprints), auto-detected from the file's contents (#59).

### Changed
- `ivert database export` now clips photons to the actual polygons of a polygon-vector region file, rather than only to that file's rectangular extent (#59).

### Fixed
- IVERT no longer crashes with a `configparser.NoOptionError` when the user config file (`~/.ivert/user_config.ini`) contains a setting that is not in `ivert_defaults.ini`, which happened after upgrading to a version where a setting had been renamed or removed (#65). Unrecognized settings are now reported in a warning, commented out of the user config file with a dated note explaining why, and ignored.
- Boolean settings written in configparser's non-literal forms (`yes`/`no`, `on`/`off`) are now read from the value actually given. A boolean in the `[AWS]` section or in the user config file previously took its value from the `[DEFAULT]` section of `ivert_defaults.ini` instead of from the override.

## [0.6.6] - 2026-07-20

### Added
- `ivert classes` command listing ICESat-2 photon classification codes and their meanings (#55).
- `ivert database export` command to export photons to vector formats (GeoPackage, Shapefile, XYZ, and more), with filtering by bounding box, photon class, and date range; supports defining an export region from a vector file's extent and prompts before exporting very large photon counts (#56).
- Documentation: `docs/classes.md` and `docs/database.md` (#55).

### Changed
- Centralized photon-class definitions into `photon_classes.py`, removing duplicated constants across the codebase (#55).
- Refactored `export_vector.py` for a cleaner, smaller implementation (#55).
- Updated README installation and Capabilities sections (#55, #57).

## [0.6.5] - 2026-07-19

### Added
- Added `tables` (PyTables) to dependencies (#54).

## [0.6.4] - 2026-07-19

### Added
- `ivert setup` command for configuring data directories and Earthdata credentials (#46).
- Coverage summary stats and `--minimum-coverage-pct` filter for `ivert validate` (#50).
- Descriptions for `ivert options` commands (#49).

### Changed
- `ivert validate -ex` bounding boxes now default to W/E/S/N order, with a `--wsen` flag (#51).
- Kept `ivert_results_subdir` relative in the config attribute and options list (#47).
- Updated README (#52).

### Removed
- Redundant `_ICESat2_error_raster.tif` output (#48).

## [0.6.3] - 2026-07-17

### Changed
- Cross-platform compatibility fixes for Windows and macOS (#43).
- Replaced Linux-specific shell dependencies (#42).

### Removed
- `ivert upgrade` command (#42).
- Unused config keys and orphaned version module (#44).

## [0.6.2] - 2026-07-17

### Added
- `-ow`/`--overwrite` and `-ex`/`--exclude` flags for `ivert validate` (#36).
- `ice_surface` and `inland_water_surface` photon classes (#32).

### Changed
- Replaced `gdal`/`osgeo` dependencies with `rasterio` and `geopandas` (#38).
- Ported remaining standalone-script argparse parsers to click (#39).
- Enabled autofixable Ruff rules and applied autofixes/formatting (#41).
- Ruff formatting, lint fixes, and dead-code cleanup (#33).

### Fixed
- `icesat2_vertical_datum` config being silently ignored by globato (#35).

### Removed
- Validate-time landmask code (#34).
- `archive` directory (#37).
- Codacy workflow (#41).

## [0.6.1] - 2026-07-10

### Fixed
- Incompatibility with Windows (#26).

## [0.6.0] - 2026-07-10

- First release with a tracked changelog.

[0.6.6]: https://github.com/continuous-dems/ivert/compare/0.6.5...0.6.6
[0.6.5]: https://github.com/continuous-dems/ivert/compare/0.6.4...0.6.5
[0.6.4]: https://github.com/continuous-dems/ivert/compare/0.6.3...0.6.4
[0.6.3]: https://github.com/continuous-dems/ivert/compare/0.6.2...0.6.3
[0.6.2]: https://github.com/continuous-dems/ivert/compare/0.6.1...0.6.2
[0.6.1]: https://github.com/continuous-dems/ivert/compare/0.6.0...0.6.1
[0.6.0]: https://github.com/continuous-dems/ivert/releases/tag/0.6.0
