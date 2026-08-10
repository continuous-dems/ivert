# Changelog

All notable changes to IVERT are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `ivert database export` now accepts a single IVERT `.nc` photon granule (exported in its entirety) or the IVERT database index `.nc` file (exported as a polygon layer of per-granule `data_bbox` footprints), auto-detected from the file's contents (#59).
- `-c`/`--classes` option for `ivert validate`, selecting which ICESat-2 photon classes the validation runs against (default `1/6/40`), matching the option of the same name on `ivert database download`.
- `-mp`/`--min-photons` option for `ivert validate`, setting the minimum number of photons a grid cell must contain to be validated (default `3`, previously hard-coded). Cells below the threshold are omitted from the results entirely.

### Changed
- `ivert database export` now clips photons to the actual polygons of a polygon-vector region file, rather than only to that file's rectangular extent (#59).
- Photon classes are now filtered once, before the per-cell arrays are copied into shared memory, rather than inside every validation child process.
- Grid cells below the minimum photon count are now omitted from validation results entirely. They were previously emitted with empty (NaN) statistics and discarded further downstream.
- Interdecile outlier trimming is now applied only to cells holding at least 5 photons. Cells with 1-4 photons use every photon they contain: a lone photon's height becomes the cell mean, and 2-4 photons are averaged. Previously every cell was trimmed to its 10th-90th percentile band, which discarded most or all photons in sparse cells.
- Validation results no longer carry a hard-coded `numphotons_intd >= 3` filter, which dropped sparse cells regardless of the requested minimum. Cell inclusion is now governed solely by `-mp`/`--min-photons`. Runs at the default `-mp 3` will report more cells than before, since 3- and 4-photon cells now produce statistics.

### Removed
- `median` and `diff_median` columns from the per-cell validation results, and the corresponding `is2_med` and `error_med` fields from the GeoPackage/Shapefile error exports. Reporting is based on the mean.

### Fixed
- `ivert validate` no longer silently restricts its elevation statistics to classes 1 and 40. Requested classes other than ground and bathy floor (e.g. land ice, and the `-b`/`--buildings` flag's class 7) were previously counted in `numphotons` but dropped before the mean/median were computed.
- Photon-level output (`-ph`/`--include-photons`) computed `dem_minus_is2_m` by subtracting a Series carrying a different index from the joined frame, yielding misaligned differences; it now uses the frame's own `dem_z` column.
- Collection-level output names (`_summary_stats.txt`, `_plot.png`, `_individual_results.csv`, and the per-DEM `_results_EMPTY.txt`) are now derived by swapping only the trailing `_results`. They used `str.replace("_results", …)`, which rewrites every occurrence, so a region name containing "results" was mangled mid-name — `-n "oregon results 2024"` wrote `oregon_summary_stats_2024_summary_stats.txt` instead of `oregon_results_2024_summary_stats.txt`. Names without "results" in them are unaffected.
- The `ivert validate` progress bar now runs to 100%. It counted rows returned by each child process, but cells below `-mp`/`--min-photons` are omitted from those results, so the bar stalled at the number of validated cells instead of the number examined (e.g. 361/1389). Progress is now measured in cells handed out to the child processes.
- `ivert validate -ph`/`--include-photons` now writes `<dem>_photons.h5` into the same `ivert_results/` directory as the rest of a DEM's validation output. The photon path was derived with `str.replace("_results", "_photons")`, which also rewrote the `ivert_results` directory name and sent the file to a sibling `ivert_photons/` directory that is never created, so `-ph` crashed with a `FileNotFoundError`. The same mis-derived path also made the resume check never find existing photon output, re-running the full pipeline on every invocation.
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
