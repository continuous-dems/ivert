# ivert database

Manage the local IVERT ICESat-2 photon database. IVERT stores downloaded photon data as NetCDF granule files (`.nc`) indexed by a single NetCDF index file (`.nc`) for fast spatial lookup. The database location is set by `ivert_database_directory` (and the index file by `ivert_database_index`) in your config (see [ivert options](options.md)).

---

## Subcommands

- [`ivert database download`](#download) — download new ICESat-2 data
- [`ivert database convert`](#convert) — convert photons to GIS vector formats (formerly `export`)
- [`ivert database dump`](#dump) — pack the database, or part of it, into a zip archive
- [`ivert database restore`](#restore) — unpack a dump into the database
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

Pass a bounding box, one or more DEM file paths (IVERT reads their extents), or one or more polygon vector files (`.gpkg`, `.shp`, `.geojson`, `.json`, `.gml`, `.kml`):

```
# Bounding box: W/E/S/N (default order)
ivert database download -74.0/-73.0/40.5/41.0

# Use --wsen if your numbers are in W/S/E/N order
ivert database download -74.0/40.5/-73.0/41.0 --wsen

# Use DEM extents
ivert database download mydem.tif
ivert database download /data/dems/*.tif

# Use the polygons in a vector file
ivert database download survey_tiles.gpkg
```

Bounding box values are in the projection given by `-p` (default EPSG:4326, i.e. decimal degrees longitude/latitude). Vector files are read in their own coordinate system, and `-p` does not apply to them.

Whatever defines the area — a bounding box, DEM extents, or vector polygons, the last two dissolved into a single region — IVERT works out the fewest rectangular requests that cover it: the region's bounding box is cut into 2° squares, the squares the region does not reach are dropped, and the rest are merged back into rectangles (a clipped edge row or column narrower than half a degree is folded into its neighbour, so no request is a thin sliver). Each rectangle is one request to NASA's Harmony service, because one large subset of a granule costs less than several small ones. A vector file holding hundreds of adjacent tile outlines therefore turns into a handful of requests rather than one per outline, and the empty space between scattered outlines is not fetched. Data is still retrieved for the whole of each 2° square the region reaches into, so some photons just outside the polygons come along.

Storage goes the other way. Each downloaded subset is classified once and then stored as one file per tile of roughly 2°, on the same grid as the requests, because a query reads every database file that touches it in full, and small files keep small queries cheap.

Classification uses the machine it is on. Each subset also needs its ATL08 (land classes) and ATL24 (bathymetry) granules, which a background process fetches ahead of the classification, several at a time; its progress bars are the ones you see. The subsets are then classified as their files arrive, by a pool of worker processes sized at run time by the `icesat2_classify_workers` setting: `auto` (the default) uses one fewer than the machine's cores, no more than the memory currently available allows (about six times the size of the largest subset file per process), and at most 8 — so a laptop with a few gigabytes free runs one or two workers while a large server runs eight. Set it to a number to force that many, or to `1` to classify one subset at a time. Workers are only forked on Linux; elsewhere `auto` is 1.

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

## convert

Convert photons from the database to common GIS vector formats. Each converted photon carries its full set of fields: `x`, `y`, `z`, `class_code`, `class_name`, `confidence`, `delta_time`, `granule_id`, and (where present) `bathy_confidence`.

```
ivert database convert [BBOX_OR_FILE] [OPTIONS]
```

> **Renamed from `export`.** This command was called `ivert database export` before IVERT 0.7.0. `ivert database export` still works for now, as an alias for `convert` with the same options, but prints a deprecation warning and will be removed in a later release. To copy the database itself rather than convert it, see [`dump`](#dump).

### Choosing what to convert

The positional argument says what to convert. With no argument, the **entire database** is converted.

```
# Convert the whole database (default output: ./ivert_photons.gpkg)
ivert database convert

# Restrict to a bounding box: W/E/S/N (default order)
ivert database convert -- -74.0/-73.0/40.5/41.0

# Use --wsen if your numbers are in W/S/E/N order
ivert database convert --wsen -- -74.0/40.5/-73.0/41.0

# Use the extent of a georeferenced raster
ivert database convert mydem.tif

# Convert only the photons inside the polygon(s) of a vector file
ivert database convert coastline.gpkg

# Convert one IVERT photon granule in its entirety
ivert database convert ~/.ivert/database/granules/ATL24_20230419_x-74.00y40.50.nc

# Convert the database index to polygon footprints, one per granule
ivert database convert ~/.ivert/database/granules/_ivert_database_index.nc
```

A **polygon-vector file** (`.shp`, `.gpkg`, `.geojson`, `.gml`, `.kml`) defines the area — or set of areas — to convert: photons are clipped to the polygons themselves, not just to their combined rectangular extent, so disjoint polygons convert only the data inside each one. Files with no polygonal geometry fall back to their bounding-box extent.

A **`.nc` file** is converted directly off disk, with no database lookup, and IVERT detects which kind it is from the file's contents:

- an **IVERT photon granule** converts in its entirety, to a point layer. This is the way to pull one specific granule out of the database — and, looping over the granule files, to convert the whole database one granule at a time. The `--classes` and date options still apply.
- the **IVERT database index** (`_ivert_database_index.nc`) converts to a *polygon* layer: one rectangle per granule, drawn from its `data_bbox`, carrying every index field. Because it holds polygons rather than points, it cannot be converted to `xyz`, and the photon filters (`--classes`, `--start-date`, `--end-date`) do not apply to it.

> **Note:** Because the positional argument is variable-length, put any options *before* a `--` delimiter when the bounding box begins with a negative number (e.g. `ivert database convert -c 40/41 -- -74.0/-73.0/40.5/41.0`).

### Output format options

| Flag | Default | Description |
|------|---------|-------------|
| `-of, --output-format FORMATS` | `gpkg` | Vector format(s): `gpkg`, `shp`, `xyz`, or a comma-separated combination (e.g. `gpkg,shp`) |
| `-o, --output PATH` | `./ivert_photons` | Output file path. The correct extension is added per format, so multiple formats share this base name. When converting a single `.nc` file, the default is the input file's name (`./ivert_database_index` for the index) |
| `-ow, --overwrite` | | Overwrite existing output files (otherwise formats whose file already exists are skipped) |

Shapefiles (`shp`) drop the `class_name` and `granule_id` photon fields, which exceed the format's field-name/length limits; `gpkg` and `xyz` retain all fields. Database-index converts to `shp` keep every field, under shortened names (e.g. `numphotons_bathy_floor` → `n_bathyflr`, `data_bbox_xmin` → `d_xmin`).

### Filtering options

| Flag | Default | Description |
|------|---------|-------------|
| `-c, --classes TEXT` | all classes | Slash-separated photon class codes to include (e.g. `40/41`). See [class codes](#photon-class-options) above, or run `ivert classes` |
| `-ds, --start-date TEXT` | no lower bound | Only convert photons on or after this date. Accepts dateparser formats: `2023-01-01`, `"1 year ago"`, `20230101` |
| `-de, --end-date TEXT` | no upper bound | Only convert photons before this date |

These filter individual photons, so they apply to every convert except the database index, where they are ignored (with a note printed).

### Other options

| Flag | Description |
|------|-------------|
| `-p, --projection TEXT` | Horizontal CRS of the bounding box (default: `EPSG:4326`) |
| `-f, --force` | Skip the confirmation prompt when the convert is estimated to be large |

---

## dump

Pack the database, or part of it, into a single compressed zip archive: its ICESat-2 photon granule files and its [landmasks](bathy_filters.md#openstreetmap-landmask-land-water-and-the-coastline), as they are, ready to [`restore`](#restore) on this or another machine. Use it to move a database, back it up, or share a region of it.

```
ivert database dump [BBOX_OR_FILE] [OPTIONS]
```

The positional argument limits the dump to a region, just as for [`convert`](#convert): nothing (the whole database), a W/E/S/N bounding box, a raster's extent, or the polygons of a vector file (covered with rectangles). `-ds`/`-de` limit it to a date range. Granule files and landmasks that reach outside the region or dates are **clipped** to it, so the archive holds, and claims to cover, only what was asked for.

```
# The whole database
ivert database dump -o my_database.zip

# One region, one year
ivert database dump -o monterey_2022.zip -ds 2022.01.01 -de 2023.01.01 -- -122.5/-121.5/36/37.2
```

| Flag | Default | Description |
|------|---------|-------------|
| `-o, --output PATH` | `./ivert_dump_<YYYYMMDD>.zip` | The archive to write (`.zip` is added if missing), or an existing directory to write the default name into |
| `-ds, --start-date TEXT` | no lower bound | Only dump photons on or after this date |
| `-de, --end-date TEXT` | no upper bound | Only dump photons before this date |
| `-p, --projection TEXT` | `EPSG:4326` | Horizontal CRS of the bounding box |
| `--wsen` | | Read the bounding box as W/S/E/N |
| `-ow, --overwrite` | | Replace the archive if it already exists |
| `-f, --force` | | Skip the confirmation prompt before dumping a very large database |

The archive holds:

- `granules/`: the photon granule files, as stored in the database;
- `landmasks/`: the landmask tiles;
- `summary.txt`: what the archive holds: the region and dates asked for, the area and dates covered, the number of granules, photon counts by class, the vertical datum, and the landmask tiles;
- `manifest.json`: the same, in machine-readable form, for `restore`.

The summary is also printed when the dump finishes (at the default `info` verbosity). The archive does not hold the database index, which `restore` rebuilds.

---

## restore

Unpack an archive written by [`dump`](#dump) into the local database, then rebuild the database index.

```
ivert database restore ARCHIVE [OPTIONS]
```

The archive's summary is printed first (at `info` verbosity). If the archive covers places and dates the database already holds, IVERT lists the overlapping areas and asks what to do:

| Choice | What happens |
|--------|--------------|
| `all` | Import everything, even where it duplicates photons already in the database |
| `keep` | Keep the existing data; import only the parts of the archive outside it |
| `replace` | Import everything, and remove the existing data in the archive's area and dates |
| `cancel` | Import nothing (the default at the prompt) |

`keep` and `replace` work by clipping granule files to what is left once the other side's area and dates are cut out, so no photon is stored twice and none is lost. A file whose photons don't reach the other side's area is left whole. Landmask tiles follow the same choice. An archive with no overlap is imported without asking.

| Flag | Description |
|------|-------------|
| `-oo, --on-overlap [all\|keep\|replace\|cancel]` | Make the choice up front instead of being asked. Required when there is overlap and IVERT is not running in a terminal |
| `--dry-run` | Show the summary and any overlap, and change nothing |

The archive's photon heights must be in the same vertical datum as the database's (see `icesat2_vertical_datum` in [`ivert options`](options.md)); an archive that isn't is refused rather than mixed in.

```
# See what an archive holds and where it overlaps
ivert database restore monterey_2022.zip --dry-run

# Add it, keeping whatever the database already has
ivert database restore monterey_2022.zip -oo keep
```

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

**Cover every polygon in a vector file, in one pass:**
```
ivert database download survey_tiles.gpkg -ds 2023-01-01 -de 2024-01-01
```

**Check what's been downloaded:**
```
ivert database list
ivert database size
```

**Convert bathymetry photons for a region to GeoPackage and Shapefile:**
```
ivert database convert -of gpkg,shp -c 40/41 -o bahamas_bathy -- -78.5/-77.0/24.0/25.5
```

**Convert the whole database (all photons) to an XYZ text file:**
```
ivert database convert -of xyz -o all_photons
```

**Convert the photons inside a set of polygons:**
```
ivert database convert study_areas.gpkg -o study_area_photons
```

**Convert one granule, and the database index as granule footprints:**
```
ivert database convert ~/.ivert/database/granules/ATL24_20230419_x-74.00y40.50.nc
ivert database convert ~/.ivert/database/granules/_ivert_database_index.nc -of gpkg,shp
```

**Move a region of the database to another machine:**
```
ivert database dump -o bahamas.zip -- -78.5/-77.0/24.0/25.5
# ...then, on the other machine:
ivert database restore bahamas.zip
```
