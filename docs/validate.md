# ivert validate

Validate one or more DEMs against ICESat-2 photon data downloaded to the local database.

For each input DEM, IVERT samples the nearest ICESat-2 photons within each grid cell, filters outliers, computes error statistics (bias, RMSE, NMAD, etc.), and writes plots and optionally GIS exports. Bathymetric and topographic DEMs are both supported.

---

## Prerequisites

You must have ICESat-2 data covering the DEM area already downloaded into the local database. If you haven't done this yet, see [ivert database](database.md).

---

## Basic usage

```
ivert validate mydem.tif
```

For a directory or glob pattern:

```
ivert validate /data/dems/survey_*.tif
ivert validate /data/dems/
```

---

## Options

### Input / output

| Flag | Default | Description |
|------|---------|-------------|
| `FILES_OR_DIRECTORY` | *(required)* | One or more GeoTIFF paths, a directory, or a glob pattern |
| `-o, --outdir DIR` | DEM's own directory | Output directory for results files |
| `-bn, --band-num N` | `1` | Raster band to validate (1-indexed) |
| `--ndv VALUE` | *(from file)* | No-data value to exclude; use `nan` for NaN |

### Vertical datum

| Flag | Default | Description |
|------|---------|-------------|
| `-V, --vdatum NAME` | *(DEM's embedded CRS)* | Vertical datum of the DEM. Accepts EPSG code (`EPSG:5703`, `5703`) or short name (`navd88`, `egm2008`, `mllw`, …) |
| `--list-vdatums` | — | Print all recognized vertical datum names and EPSG codes, then exit |

ICESat-2 photons are stored either in WGS84 'ellipsoid' (EPSG:4979) or EGM2008 'geoid' (EPSG:3855) elevations, depending on the setting used (see 'ivert options' for more information). IVERT automatically transforms them to the DEM's vertical datum before comparing. If the DEM's CRS already encodes the vertical datum, `-V` is not needed.

### Photon filtering

| Flag | Default | Description                                                                     |
|------|---------|---------------------------------------------------------------------------------|
| `-c, --classes CLASSES` | `1/6/40` | Slash-separated photon class codes to validate against (e.g. `1/40`). Photons in any other class are excluded before the elevation statistics are computed. See [class codes](classes.md), or run `ivert classes` |
| `-cl, --confidence-level N` | `4` | Minimum ATL03 signal confidence (1=low/keep all, 2=medium, 3=high, 4=very-high) |
| `-bc, --bathy-confidence F` | `0.90` | Minimum ATL24 bathymetry confidence for bathy-floor photons (0.0–1.0)           |
| `-b, --buildings` | off | Include building-classed photons (class 7) in validation, on top of `-c/--classes` |
| `-sd, --outlier-sd F` | `2.5` | Outlier threshold in standard deviations (use `-1` to disable)                  |

### Output options

| Flag | Default | Description |
|------|---------|-------------|
| `-ph, --include-photons` | off | Also write a point database of individual ICESat-2 photons used |
| `-mc, --measure-coverage` | off | Measure relative photon coverage per grid cell |
| `-ef, --export-formats FORMATS` | `tif,gpkg` | Comma-separated list of GIS error-export formats: `tif`, `gpkg`, `shp`, `xyz`. Use `none` or `""` to disable exports. |

### Labeling

| Flag | Default | Description |
|------|---------|-------------|
| `-n, --name TEXT` | *(DEM filename)* | Region name shown on plots |

---

## Output files

### Per-DEM output files

For a DEM named `survey.tif`, IVERT writes these files to the output directory:

| File | Description |
|------|-------------|
| `survey_results.h5` | Full validation results (statistics, per-cell errors, photon matches) |
| `survey_results.png` | Multi-panel validation plot |
| `survey_summary_stats.txt` | Human-readable summary statistics (see below) |
| `survey_errors.tif` | GeoTIFF raster of mean per-cell error (if `tif` in export formats) |
| `survey_errors.gpkg` | GeoPackage point file of per-cell errors (if `gpkg` in export formats) |
| `survey_errors.shp` | Shapefile of per-cell errors (if `shp` in export formats) |
| `survey_errors.xyz` | Whitespace-delimited `x y error` text file (if `xyz` in export formats) |
| `survey_photons.gpkg` | Individual ICESat-2 photons used (if `-ph` flag given) |

The `_summary_stats.txt` file contains:

- Number of DEM cells validated
- Total and mean number of ICESat-2 photons used
- Mean bias error (DEM − ICESat-2) and RMSE
- Full percentile breakdown of per-cell errors (0, 1, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 99, 100th percentiles)
- Number of cells with bathymetry photons
- Mean roughness (standard deviation of photon elevations within each cell)

### Multi-DEM collection output files

When validating multiple DEMs (a directory or glob pattern), IVERT also writes a set of collection-level summary files. These are named after the value passed to `-n`/`--name`; if no name is given, the prefix defaults to `summary`.

| File | Description |
|------|-------------|
| `{name}_results.h5` | Combined HDF5 file of all per-cell results across every DEM in the collection |
| `{name}_individual_results.csv` | Per-DEM summary table: RMSE, mean bias, standard deviation, cell count, and mean photons per cell for each DEM |
| `{name}_plot.png` | Combined validation plot across all DEMs in the collection |
| `{name}_summary_stats.txt` | Same format as the per-DEM summary stats file, aggregated over all cells across all DEMs |

---

## Examples

**Basic validation:**
```
ivert validate mydem.tif
```

**Specify vertical datum:**
```
ivert validate mydem.tif -V navd88
```

**Validate all DEMs in a directory, write results to a separate folder:**
```
ivert validate /data/dems/ -o /data/results/
```

**Export errors to all GIS formats:**
```
ivert validate mydem.tif -ef tif,gpkg,shp,xyz
```

**Disable error exports entirely:**
```
ivert validate mydem.tif -ef none
```

**List available vertical datum names:**
```
ivert validate --list-vdatums
```
