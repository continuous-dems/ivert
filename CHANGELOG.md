# Changelog

All notable changes to IVERT are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.9] - 2026-08-27

### Added
- `CITATION.cff`, so GitHub offers a "Cite this repository" entry and reference managers can read IVERT's authorship, version, and license metadata (#82). The `version` and `date-released` fields are maintained by hand and are bumped as part of each release. A `cffconvert` pre-commit hook validates the file against the CFF 1.2.0 schema.

### Fixed
- `ivert validate` now reports a readable error when no photon database exists where IVERT is looking, instead of crashing with `TypeError: object of type 'NoneType' has no len()` inside a validation child process (#78, fixes #77). The message names both the configured index file and granules directory, since `ivert_database_index` and `ivert_database_directory` are separate settings and need not agree. When the index is missing but granule files are present, IVERT warns and rebuilds the index in place rather than failing.
- Config values of `NaN` and `inf` are now read as floats rather than strings (#79). `dem_default_ndv` defaults to `NaN`, so validating a DEM whose header declares no NoData value — without passing `--ndv` — died with `TypeError: ufunc 'isnan' not supported for the input types`; the workaround was to pass `--ndv nan` explicitly. `ast.literal_eval()` cannot parse `NaN` and `inf`, which are names rather than literals. Only non-finite values are converted, so a zero-padded setting such as `nsidc_atl_version = 007` is still read as the string it has to be.
- `ivert database download` no longer fails with `RuntimeError: NetCDF: HDF error` when IVERT is installed with `pip` into a virtual environment (#81). `netCDF4` and `h5py` ship binary wheels carrying conflicting `libhdf5` builds; importing `netCDF4` at the top of `icesat2_database_v2.py` settles which one is loaded first. Conda installs were never affected, because conda supplies a single shared `libhdf5` for both.

### Changed
- The user guide moved from `docs/` to `docs/source/user_guide/`, and the repository gained a Sphinx and Read the Docs configuration (#80).

## [0.6.8] - 2026-08-11

### Added
- `ivert database download` now offers to save your NASA Earthdata credentials to `~/.netrc` when a download needs them (#74). The credential prompt comes from `fetchez`, which only reads `.netrc` and never writes one, so a download that needed credentials asked for them again on every run; saving them was reachable only through `ivert setup`. The prompt is suppressed when there is no tty.

### Fixed
- IVERT now warns, and offers to `chmod 600`, when `~/.netrc` is group- or world-readable (#74). The standard library's `netrc` parser — which `fetchez` uses — refuses to read such a file and reports *no* credentials, while IVERT's own check parsed the tokens by hand and reported "all set", so the user was prompted for credentials over and over with no explanation.
- `ivert options <key>=<value> -y`/`--yes` no longer prints the warning about settings that inherit the changed value (#74). That warning exists to say those settings would silently stay behind, which is not what `--yes` does — it updated them all immediately afterwards. The `Updated: ...` summary still reports everything that changed, and the interactive and non-interactive paths are unchanged.

## [0.6.7] - 2026-08-10

### Added
- `-y`/`--yes` flag on the `ivert options` command group, accepting the prompt to propagate a setting change to the settings that inherit from it (#71).
- `ivert database export` now accepts a single IVERT `.nc` photon granule (exported in its entirety) or the IVERT database index `.nc` file (exported as a polygon layer of per-granule `data_bbox` footprints), auto-detected from the file's contents (#59).
- `-c`/`--classes` option for `ivert validate`, selecting which ICESat-2 photon classes the validation runs against (default `1/6/40`), matching the option of the same name on `ivert database download`.
- `-mp`/`--min-photons` option for `ivert validate`, setting the minimum number of photons a grid cell must contain to be validated (default `3`, previously hard-coded). Cells below the threshold are omitted from the results entirely.

### Changed
- The IVERT database index is now a single NetCDF (`.nc`) file, replacing the previous GeoPackage plus a Blosc-compressed cache of the same information (#60). The `icesat2_granules_gpkg` and `icesat2_granules_blosc` settings are replaced by `ivert_database_index`, and `icesat2_granules_directory` by `ivert_database_directory`. The index is a plain table of numeric columns carrying no geometry, so granule lookups are vectorized bounding-box comparisons.
- `ivert options <key>=<value>` now finds the settings that inherit their value from `<key>` — transitively, via configparser's `%(...)s` interpolation — and offers to copy them into the user config (#71). Overriding `user_data_directory` previously left `cache_directory`, `ivert_database_directory`, `ivert_database_index`, `icesat2_download_directory` and `icesat2_requests_csv` pointing at the old tree, because those defaults resolve inside whichever file defines them. Values are copied verbatim with `%(...)s` intact, so they resolve against the new value and follow any later change automatically. Settings you have already set yourself are never listed or overwritten, and declining the prompt can never abort the primary change.
- `user_configfile` now defaults to `~/.ivert/user_config.ini`, independent of `user_data_directory`, and is listed but not settable through `ivert options` (#71). IVERT has to resolve the user config's location before it can open that file, so a user config naming its own location made reads and writes disagree. A `user_configfile` found inside a user config is now ignored, warned about, and commented out with a dated note. This is a no-op for existing installs: every user config already lives at exactly that path.
- `ivert setup` now derives the directories it creates from the config's path settings rather than from a hand-maintained list, so new path settings are picked up automatically (#72). The set of directories created is unchanged; only the order in which they are reported differs.
- Progress bars are now drawn with `tqdm` rather than IVERT's homegrown progress bar (#70). `tqdm` is a new dependency.
- `ivert database export` now clips photons to the actual polygons of a polygon-vector region file, rather than only to that file's rectangular extent (#59).
- Photon classes are now filtered once, before the per-cell arrays are copied into shared memory, rather than inside every validation child process.
- Grid cells below the minimum photon count are now omitted from validation results entirely. They were previously emitted with empty (NaN) statistics and discarded further downstream.
- Interdecile outlier trimming is now applied only to cells holding at least 5 photons. Cells with 1-4 photons use every photon they contain: a lone photon's height becomes the cell mean, and 2-4 photons are averaged. Previously every cell was trimmed to its 10th-90th percentile band, which discarded most or all photons in sparse cells.
- Validation results no longer carry a hard-coded `numphotons_intd >= 3` filter, which dropped sparse cells regardless of the requested minimum. Cell inclusion is now governed solely by `-mp`/`--min-photons`. Runs at the default `-mp 3` will report more cells than before, since 3- and 4-photon cells now produce statistics.

### Removed
- The `blosc2` dependency, along with `utils/pickle_blosc.py` (#60).
- `utils/progress_bar.py`, superseded by `tqdm` (#70).
- `median` and `diff_median` columns from the per-cell validation results, and the corresponding `is2_med` and `error_med` fields from the GeoPackage/Shapefile error exports. Reporting is based on the mean.

### Fixed
- Cached vertical shift grids are no longer reused for regions they do not cover (#67). The cache file name was built from bounds rounded to 0.1°, but the grid was generated over the exact (smaller) region, so a later run over a wider area reused a grid that fell short — and points outside it were silently given a **0.0 m** shift, leaving them unconverted and wrong by the full datum separation (~24 m for NAVD88 on the Oregon coast). Grids are now generated over the region snapped outward to 0.1°, so the file name describes exactly the extent covered; a cached grid is checked against the region being transformed before use and regenerated if it falls short; and a point outside the grid now raises instead of silently returning an unconverted height. Grids cached by earlier versions use the old naming and are simply ignored — delete any `vshift_*.tif` in your cache directory to reclaim the space.
- `ivert validate` no longer silently restricts its elevation statistics to classes 1 and 40. Requested classes other than ground and bathy floor (e.g. land ice, and the `-b`/`--buildings` flag's class 7) were previously counted in `numphotons` but dropped before the mean/median were computed.
- Photon-level output (`-ph`/`--include-photons`) computed `dem_minus_is2_m` by subtracting a Series carrying a different index from the joined frame, yielding misaligned differences; it now uses the frame's own `dem_z` column.
- Collection-level output names (`_summary_stats.txt`, `_plot.png`, `_individual_results.csv`, and the per-DEM `_results_EMPTY.txt`) are now derived by swapping only the trailing `_results`. They used `str.replace("_results", …)`, which rewrites every occurrence, so a region name containing "results" was mangled mid-name — `-n "oregon results 2024"` wrote `oregon_summary_stats_2024_summary_stats.txt` instead of `oregon_results_2024_summary_stats.txt`. Names without "results" in them are unaffected.
- The `ivert validate` progress bar now runs to 100%. It counted rows returned by each child process, but cells below `-mp`/`--min-photons` are omitted from those results, so the bar stalled at the number of validated cells instead of the number examined (e.g. 361/1389). Progress is now measured in cells handed out to the child processes.
- `ivert validate -ph`/`--include-photons` now writes `<dem>_photons.h5` into the same `ivert_results/` directory as the rest of a DEM's validation output. The photon path was derived with `str.replace("_results", "_photons")`, which also rewrote the `ivert_results` directory name and sent the file to a sibling `ivert_photons/` directory that is never created, so `-ph` crashed with a `FileNotFoundError`. The same mis-derived path also made the resume check never find existing photon output, re-running the full pipeline on every invocation.
- IVERT no longer crashes with a `configparser.NoOptionError` when the user config file (`~/.ivert/user_config.ini`) contains a setting that is not in `ivert_defaults.ini`, which happened after upgrading to a version where a setting had been renamed or removed (#65). Unrecognized settings are now reported in a warning, commented out of the user config file with a dated note explaining why, and ignored.
- Boolean settings written in configparser's non-literal forms (`yes`/`no`, `on`/`off`) are now read from the value actually given. A boolean in the `[AWS]` section or in the user config file previously took its value from the `[DEFAULT]` section of `ivert_defaults.ini` instead of from the override.
- `ivert options` no longer writes to a user config file that IVERT never reads back, and `ivert options reset` no longer reports that no config file exists when one does (#71). Both happened whenever a user config defined its own `user_configfile`, which is now ignored. `ivert options list`/`info` also report the file actually in use, labelled `[--config]` when it has been overridden.
- `ivert setup` no longer creates a stray relative directory in the current working directory when a path setting has been set to a non-path value such as `none` (#72).

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

[0.6.9]: https://github.com/continuous-dems/ivert/compare/0.6.8...0.6.9
[0.6.8]: https://github.com/continuous-dems/ivert/compare/0.6.7...0.6.8
[0.6.7]: https://github.com/continuous-dems/ivert/compare/0.6.6...0.6.7
[0.6.6]: https://github.com/continuous-dems/ivert/compare/0.6.5...0.6.6
[0.6.5]: https://github.com/continuous-dems/ivert/compare/0.6.4...0.6.5
[0.6.4]: https://github.com/continuous-dems/ivert/compare/0.6.3...0.6.4
[0.6.3]: https://github.com/continuous-dems/ivert/compare/0.6.2...0.6.3
[0.6.2]: https://github.com/continuous-dems/ivert/compare/0.6.1...0.6.2
[0.6.1]: https://github.com/continuous-dems/ivert/compare/0.6.0...0.6.1
[0.6.0]: https://github.com/continuous-dems/ivert/releases/tag/0.6.0
