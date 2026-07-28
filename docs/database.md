# ivert database

Manage the local IVERT ICESat-2 photon database. IVERT stores downloaded photon data as NetCDF granule files (`.nc`) indexed by a single NetCDF index file (`.nc`) for fast spatial lookup. The database location is set by `ivert_database_directory` (and the index file by `ivert_database_index`) in your config (see [ivert options](options.md)).

---

## Subcommands

- [`ivert database download`](#download) — download new ICESat-2 data
- [`ivert database export`](#export) — export photons to GIS vector formats
- [`ivert database list`](#list) — list what's already downloaded
- [`ivert database size`](#size) — check disk usage
- [`ivert database rebuild`](#rebuild) — rebuild the index from existing files
- [`ivert database delete`](#delete) — remove data from disk

---

## download

Download ICESat-2 photon data for a geographic region and time range.

```
ivert database download BBOX_OR_FILES [OPTIONS]
```

### Specifying the area

Pass either a bounding box or one or more DEM file paths (IVERT reads their extents):

```
# Bounding box: W/E/S/N (default order)
ivert database download -74.0/-73.0/40.5/41.0

# Use --wsen if your numbers are in W/S/E/N order
ivert database download -74.0/40.5/-73.0/41.0 --wsen

# Use DEM extents
ivert database download mydem.tif
ivert database download /data/dems/*.tif
```

Bounding box values are in the projection given by `-p` (default EPSG:4326, i.e. decimal degrees longitude/latitude).

### Date range options

| Flag | Default | Description |
|------|---------|-------------|
| `-ds, --date-start TEXT` | one year and one week ago | Start of the search window. Accepts dateparser formats: `2023-01-01`, `"1 year ago"`, `20230101` |
| `-de, --date-end TEXT` | one week ago | End of the search window |

> **Note:** ATL24 (bathymetry) data is only available through approximately November 2024. For bathymetric validation, use a date range ending at or before `2024-11-07`.

### Photon class options

| Flag | Default | Description |
|------|---------|-------------|
| `-c, --classes TEXT` | `1/6/7/9/40/41/42` | Slash-separated list of photon class codes to download |

Photon class codes:

| Code | Class |
|------|-------|
| `-1` | Unclassified |
| `0` | Noise |
| `1` | Ground |
| `2` | Canopy |
| `3` | Canopy top |
| `6` | Land ice |
| `7` | Buildings |
| `9` | Inland water |
| `40` | Bathymetry floor |
| `41` | Bathymetry / nearshore water surface |
| `42` | Lake surface |

**Note:** Not all photons saved to disk are necessarily used for validations. See the "[ivert validate](./validate.md)" command for additional filters applied during validations.

### Quality filtering options

| Flag | Default | Description |
|------|---------|-------------|
| `-cl, --confidence-level N` | `1` | Minimum ATL03 signal confidence (1=keep all, 2=medium, 3=high, 4=very-high) |
| `-bc, --bathy-confidence F` | `0.01` | Minimum ATL24 bathymetry confidence for bathy-floor photons (0.0–1.0) |

**Note:** Not all photons saved to disk are necessarily used for validations. See the "[ivert validate](./validate.md)" command for additional filters applied during validations. By default, the validate command uses photons with more-stringent confidence bounds. This just defines what is saved to disk and available for potential use after initial download.

### Other options

| Flag | Description |
|------|-------------|
| `-p, --projection TEXT` | Horizontal CRS of the bounding box (default: `EPSG:4326`) |
| `-r, --replace` | Replace any previously downloaded data overlapping this region |
| `-f, --force` | Skip the interactive prompt when the date range extends beyond the ATL24 data cutoff |

---

## export

Export data from the database to common GIS vector formats. Each exported photon carries its full set of fields: `x`, `y`, `z`, `class_code`, `class_name`, `confidence`, `delta_time`, `granule_id`, and (where present) `bathy_confidence`.

```
ivert database export [BBOX_OR_FILE] [OPTIONS]
```

### Choosing what to export

The positional argument says what to export. With no argument, the **entire database** is exported.

```
# Export the whole database (default output: ./ivert_photons.gpkg)
ivert database export

# Restrict to a bounding box: W/E/S/N (default order)
ivert database export -- -74.0/-73.0/40.5/41.0

# Use --wsen if your numbers are in W/S/E/N order
ivert database export --wsen -- -74.0/40.5/-73.0/41.0

# Use the extent of a georeferenced raster
ivert database export mydem.tif

# Export only the photons inside the polygon(s) of a vector file
ivert database export coastline.gpkg

# Export one IVERT photon granule in its entirety
ivert database export ~/.ivert/database/granules/ATL24_20230419_x-74.00y40.50.nc

# Export the database index as polygon footprints, one per granule
ivert database export ~/.ivert/database/granules/_ivert_database_index.nc
```

A **polygon-vector file** (`.shp`, `.gpkg`, `.geojson`, `.gml`, `.kml`) defines the area — or set of areas — to export: photons are clipped to the polygons themselves, not just to their combined rectangular extent, so disjoint polygons export only the data inside each one. Files with no polygonal geometry fall back to their bounding-box extent.

A **`.nc` file** is exported directly off disk, with no database lookup, and IVERT detects which kind it is from the file's contents:

- an **IVERT photon granule** exports in its entirety, as a point layer. This is the way to pull one specific granule out of the database — and, looping over the granule files, to export the whole database one granule at a time. The `--classes` and date options still apply.
- the **IVERT database index** (`_ivert_database_index.nc`) exports as a *polygon* layer: one rectangle per granule, drawn from its `data_bbox`, carrying every index field. Because it holds polygons rather than points, it cannot be exported as `xyz`, and the photon filters (`--classes`, `--start-date`, `--end-date`) do not apply to it.

> **Note:** Because the positional argument is variable-length, put any options *before* a `--` delimiter when the bounding box begins with a negative number (e.g. `ivert database export -c 40/41 -- -74.0/-73.0/40.5/41.0`).

### Output format options

| Flag | Default | Description |
|------|---------|-------------|
| `-of, --output-format FORMATS` | `gpkg` | Vector format(s): `gpkg`, `shp`, `xyz`, or a comma-separated combination (e.g. `gpkg,shp`) |
| `-o, --output PATH` | `./ivert_photons` | Output file path. The correct extension is added per format, so multiple formats share this base name. When exporting a single `.nc` file, the default is the input file's name (`./ivert_database_index` for the index) |
| `-ow, --overwrite` | | Overwrite existing output files (otherwise formats whose file already exists are skipped) |

Shapefiles (`shp`) drop the `class_name` and `granule_id` photon fields, which exceed the format's field-name/length limits; `gpkg` and `xyz` retain all fields. Database-index exports to `shp` keep every field, under shortened names (e.g. `numphotons_bathy_floor` → `n_bathyflr`, `data_bbox_xmin` → `d_xmin`).

### Filtering options

| Flag | Default | Description |
|------|---------|-------------|
| `-c, --classes TEXT` | all classes | Slash-separated photon class codes to include (e.g. `40/41`). See [class codes](#photon-class-options) above, or run `ivert classes` |
| `-ds, --start-date TEXT` | no lower bound | Only export photons on or after this date. Accepts dateparser formats: `2023-01-01`, `"1 year ago"`, `20230101` |
| `-de, --end-date TEXT` | no upper bound | Only export photons before this date |

These filter individual photons, so they apply to every export except the database index, where they are ignored (with a note printed).

### Other options

| Flag | Description |
|------|-------------|
| `-p, --projection TEXT` | Horizontal CRS of the bounding box (default: `EPSG:4326`) |
| `-f, --force` | Skip the confirmation prompt when the export is estimated to be large |

---

## list

Show granules currently in the database.

```
ivert database list
ivert database list --all
ivert database list --boxes
```

| Flag | Description |
|------|-------------|
| `-a, --all` | Show all fields instead of the default summary columns |
| `-bo, --boxes` | Print the unique bounding boxes used when building the database |

---

## size

Report the number of files and disk space used by each part of the database.

```
ivert database size
```

Output shows: the NetCDF index file (`.nc`) and the raw granule files (`.nc`).

---

## rebuild

Reconstruct the database index by scanning existing `.nc` granule files on disk.

```
ivert database rebuild
```

Use this if the index file becomes corrupted or out of sync with the granule files — for example after an interrupted download.

---

## delete

Delete the database index files.

```
ivert database delete
ivert database delete --all
```

| Flag | Description |
|------|-------------|
| `-a, --all` | Also delete all `.nc` granule data files (full removal) |
| `-y, --yes` | Skip the confirmation prompt |

Without `--all`, only the index file is deleted; the granule `.nc` files remain on disk and can be re-indexed with `ivert database rebuild`.

---

## Examples

**Download data for a coastal region (last year):**
```
ivert database download -74.0/-73.0/40.5/41.0
```

**Download for a specific date range:**
```
ivert database download -74.0/-73.0/40.5/41.0 -ds 2023-01-01 -de 2024-01-01
```

**Download only ground photons at high confidence:**
```
ivert database download -74.0/-73.0/40.5/41.0 -c 1 -cl 3
```

**Match the extent of a DEM:**
```
ivert database download mydem.tif
```

**Check what's been downloaded:**
```
ivert database list
ivert database size
```

**Export bathymetry photons for a region to GeoPackage and Shapefile:**
```
ivert database export -of gpkg,shp -c 40/41 -o bahamas_bathy -- -78.5/-77.0/24.0/25.5
```

**Export the whole database (all photons) to an XYZ text file:**
```
ivert database export -of xyz -o all_photons
```

**Export the photons inside a set of polygons:**
```
ivert database export study_areas.gpkg -o study_area_photons
```

**Export one granule, and the database index as granule footprints:**
```
ivert database export ~/.ivert/database/granules/ATL24_20230419_x-74.00y40.50.nc
ivert database export ~/.ivert/database/granules/_ivert_database_index.nc -of gpkg,shp
```
