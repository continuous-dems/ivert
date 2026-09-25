# Bathymetry filters

`ivert validate` runs four tests on ICESat-2 bathymetry-floor photons (class 40) before it compares them with a DEM. A photon that fails any enabled test is left out of the validation. This page explains why the tests exist, what each one removes, which settings control it, and which extra files IVERT downloads and keeps to run them.

## Why these filters exist

IVERT takes its class-40 photons from NASA's ATL24 bathymetry product. ATL24 labels some photons as seafloor that are not seafloor at all. The two common kinds are:

- **Surface returns over deep water.** Photons 0.5–3 m below the sea surface, labelled as seafloor, where the water is hundreds or thousands of metres deep. ICESat-2 cannot see the seafloor past about 30–40 m, even in the clearest water.
- **Noise under dry land.** Photons 15–24 m below sea level, labelled as seafloor, under ground that is plainly land.

These false positives are a small share of the photons in most validations. Their errors, though, are enormous: a "seafloor" photon near the surface over 1,000 m of water disagrees with an accurate DEM by 1,000 m. A handful of them in a cell is enough to dominate that cell's statistics. Across a validation they swamp the bathymetry results and inflate the overall bias and RMSE. In the CRM Vol 6 North validation, the land cells agreed with the DEM at +1.00 ± 6.59 m. The 326 bathymetry cells read −288 ± 552 m, with the worst at −2,145 m, and almost all of that came from false seafloor photons. With the filters on, the bathymetry cells that remain read −1.7 ± 3.2 m.

ATL24's own per-photon fields (`confidence`, `low_confidence_flag`, `sigma_tvu` and the rest) do not separate the false photons from the real ones. Raising `-bc/--bathy-confidence` doesn't help much. What does work is checking each photon against independent information about where it is:

- a **reference bathymetry**: how deep the water there actually is, from ETOPO 2022 by default.
- the **OpenStreetMap landmask**: whether the photon is over land or water, and how far it is from the coast.

## The four tests

| Test | Keyword | Filters out | Thresholds (default) | `ivert validate` flag | `ivert options` setting |
|------|---------|-------------|----------------------|-----------------------|-------------------------|
| A | `deep` | Photons over water too deep for ICESat-2 to see the seafloor | Max depth (30 m) | `--bathy-max-depth M` | `bathy_max_depth_m` |
| | | | Search radius (1000 m) | `--bathy-ref-window M` | `bathy_ref_window_m` |
| B | `offshore` | Near-surface photons in open water, far from the coast | Near-surface depth (5 m) | `--bathy-near-surface M` | `bathy_near_surface_m` |
| | | | Distance from the coast (500 m) | `--bathy-min-coast-dist M` | `bathy_min_coast_dist_m` |
| | | | Optional shallow-bank guard (off) | `--bathy-offshore-min-ref-depth M` | `bathy_offshore_min_ref_depth_m` |
| C | `reference` | Photons far shallower than the reference bathymetry | Tolerance (30 m) | `--bathy-ref-tolerance M` | `bathy_ref_tolerance_m` |
| D | `land` | Photons well below sea level on dry land | Max depth below sea level (10 m) | `--bathy-land-max-below-sl M` | `bathy_land_max_below_sl_m` |

Two more settings apply to all four tests:

| What it does | Default | `ivert validate` flag | `ivert options` setting |
|--------------|---------|-----------------------|-------------------------|
| Which tests to run: a comma-separated list of keywords, or `none` | `deep,offshore,reference,land` | `-bf, --bathy-filters RULES` | `bathy_filters` |
| Reference bathymetry raster, used in place of ETOPO 2022 | ETOPO 2022 | `--bathy-ref-raster FILE` | `bathy_ref_raster` |

A flag changes a value for one run only. To change the default for every run, use `ivert options <setting>=<value>`; for example, `ivert options bathy_max_depth_m=40`. A flag always overrides the setting.

### A: `deep`, water too deep to see the bottom

**Fires when:** the *shallowest* reference cell within `bathy_ref_window_m` of the photon is deeper than `bathy_max_depth_m`.

ICESat-2's green laser reaches the seafloor only in shallow, clear water; about 30 m is the best case. If even the shallowest water nearby is deeper than that, the photon cannot be seafloor. The test uses the shallowest cell in a window, not the one cell under the photon. ETOPO's cells are about 450 m across and smooth out steep slopes, so without the window, a real photon near a steep shelf edge could sit in a cell that reads deep and be removed wrongly.

- Raise `bathy_max_depth_m` for exceptionally clear water where ICESat-2 reaches deeper.
- Widen `bathy_ref_window_m` to be more cautious near steep slopes. This keeps more photons.
- Shrink it to remove more photons close to shelf edges.

In the CRM Vol 6 North data, this test alone removed 97.7% of the false offshore photons and no good ones.

### B: `offshore`, surface returns in open water

**Fires when** all of the following are true:
- the photon is outside the landmask;
- it is less than `bathy_near_surface_m` below sea level;
- it is more than `bathy_min_coast_dist_m` from the coastline;
- if `bathy_offshore_min_ref_depth_m` is set, the reference bathymetry is also deeper than that.

A real seafloor photon a couple of metres deep means very shallow water, which is almost always close to shore. A photon that shallow, far from any coast, is most likely a return from the sea surface itself. Test B catches the surface returns that test A misses: those in water only moderately deep, or over areas where the reference is coarse.

"Sea level" here is the EGM2008 geoid, which does not include the tide, storm surge, or the ocean's dynamic topography. Where tides are small (about 2 m in southern California) that makes little difference. In macrotidal areas, raise `bathy_near_surface_m` so that photons at low tide aren't mistaken for surface returns. (Issue #112 tracks storing the actual sea surface per photon to replace this approximation.)

Shallow banks far from any coast, such as the Bahamas Banks, are where test B can go wrong: there, real seafloor photons are genuinely shallow and far offshore. Setting `bathy_offshore_min_ref_depth_m` (for example, to 10 m) makes test B fire only where the reference also says the water is deep. In the southern California data, a 10 m guard kept 84% of what test B caught and lost no good photons.

The landmask counts lakes as land, so test B never fires on lakes. Photons in areas with no landmask coverage are skipped by B and D, with a warning.

### C: `reference`, much shallower than the reference

**Fires when:** the photon is more than `bathy_ref_tolerance_m` shallower than the reference bathymetry at its location.

This is a coarse sanity check against an independent source. The default tolerance is deliberately wide. ETOPO's roughly 450 m cells can't follow a rugged nearshore seafloor; along the Monterey–Carmel coast, a 10 m tolerance removed 155 good photons, and 30 m removed none. Lower the tolerance if you supply a finer reference raster with `--bathy-ref-raster`.

The test only removes photons that are too *shallow*. A photon deeper than the reference is left alone, since that is exactly the kind of real disagreement a DEM validation is meant to find.

### D: `land`, below sea level on land

**Fires when:** the photon is inside the landmask and more than `bathy_land_max_below_sl_m` below sea level.

Estuaries, lagoons and sloughs sit inside the landmask, but their water surface is at sea level and they are rarely more than a few metres deep, so their photons pass this test. Photons tens of metres below sea level under dry land are noise. In the CRM Vol 6 North data this test removed 93.5% of the false inland photons, more than half of them from a single bad granule (`20220616194013_13091506`). With all four tests on, every photon in Elkhorn Slough was kept, and 10 of 13 in Morro Bay.

Raise `bathy_land_max_below_sl_m` in places with land that is genuinely below sea level and has deep water within the landmask. Lower it to be stricter.

## How the tests combine

- **Only class-40 photons are tested.** Ground, canopy, building and every other class pass through untouched, and the tests only run when 40 is among the validated classes (`-c/--classes`).
- **The tests run after the confidence filter** (`-bc/--bathy-confidence`) and before any photon is compared with the DEM.
- **A photon failing any enabled test is removed.** The per-test counts overlap: a photon failing both A and C is counted under each, and once in the total.
- **All four tests are on by default.** `-bf none` turns them all off and reproduces the results of IVERT versions without these filters exactly.
- **Removing photons can remove cells.** If every photon in a DEM cell is removed, the cell drops out of the results. If every photon in a DEM is removed, the DEM is marked `_EMPTY` like any DEM with no ICESat-2 data. This is the correct outcome for a tile of open deep water, where every "seafloor" photon was false.

## Recording what was filtered

Each `_summary_stats.txt` file ends with a section listing:
- the tests that ran and their thresholds;
- the reference bathymetry used;
- how many class-40 photons there were;
- how many each test removed.

```
== Bathymetry photon filters (class 40) ===
    Rules applied: deep, offshore, reference, land
    Reference bathymetry: ETOPO 2022 15" surface elevation (EGM2008)
    'deep' removes photons where the shallowest reference cell within 1000 m is deeper than 30 m
    'offshore' removes photons outside the landmask, less than 5 m below the EGM2008 geoid, and more than 500 m from the coastline
    'reference' removes photons more than 30 m shallower than the reference
    'land' removes photons inside the landmask and more than 10 m below the EGM2008 geoid
    Bathymetry photons before filtering (photons): 393
    Removed by 'deep' (photons): 23
    Removed by 'offshore' (photons): 293
    Removed by 'reference' (photons): 155
    Removed by 'land' (photons): 0
    Removed in total, each photon counted once (photons): 294
```

The same record is stored as an attribute of the `_results.h5` file. A summary file regenerated later from the `.h5` still lists what was filtered. When several DEMs are validated together, the collection summary sums the counts over the DEMs that produced results.

## Files the filters download and keep

### ETOPO 2022: the reference bathymetry

| | |
|---|---|
| **Used by** | Tests A and C, and B when `bathy_offshore_min_ref_depth_m` is set |
| **Where** | `<cache_directory>/etopo/` (default `~/.ivert/cache/etopo/`) |
| **What** | ETOPO 2022 15-arc-second surface elevation tiles, in metres relative to EGM2008. Each tile covers 15° × 15° and is 20–35 MB. |
| **When** | Downloaded from NOAA NCEI the first time a validation needs a tile, then reused. Validations read only the part of a tile around their photons. |

This is a public, fixed dataset that downloads in seconds, so it is kept only in the cache. Clearing the cache (`ivert cache delete`) is safe: the next validation downloads it again. Validating offline right after a cache clear fails, with a message saying to reconnect or turn off tests A and C.

To use your own reference instead, pass `--bathy-ref-raster FILE`, or set `bathy_ref_raster`. The raster must hold heights in metres, positive up, relative to the EGM2008 geoid; IVERT does not convert it from another vertical datum. Any horizontal coordinate system works.

### OpenStreetMap landmask: land, water and the coastline

| | |
|---|---|
| **Used by** | Tests B and D |
| **Where** | `ivert_landmask_directory` (default `~/.ivert/database/landmasks/`) |
| **What** | One GeoJSON file of land polygons per storage tile of the photon database, e.g. `landmask_-122.0_-120.0_36.0_37.0.geojson` (west, east, south, north). A file with no polygons is open water. |
| **When** | Written during `ivert database download`, and filled in by `ivert validate` for any tile still missing. |

Landmasks cost minutes of OpenStreetMap queries to build, so IVERT keeps its own copy with the photon database, not in the cache. Clearing the cache does not lose it. The files come from two places:

1. **The download cache.** While building the database, `ivert database download` fetches an OSM landmask for each area it requests, using fetchez's `osm_landmask` module. It caches these in `<icesat2_download_directory>/osm_landmask/` (default `~/.ivert/cache/osm_landmask/`). After each part of a download, IVERT clips that landmask into one file per storage tile in the landmask store. No extra query is needed.
2. **OpenStreetMap, only if needed.** When a validation needs a landmask the store doesn't have, IVERT first builds it from the cached files if they cover the area. Only if they don't does it query OpenStreetMap, then store the result. This covers databases built before the store existed, a cleared cache, and the buffer around a DEM that reaches past the edge of the database. Areas past the database's edge are stored in 1° cells.

Once a validation has run over an area, later validations there build their landmask from local files in a few seconds.

Stored tiles are merged before the coastline is traced, so the boundaries between tiles and the edges of the area being validated are never mistaken for coastline. Only the real shoreline counts toward test B's distance.

### Vertical datum shift grids

When the photon database stores ellipsoid heights (the `icesat2_vertical_datum="ellipsoid"` default), the tests first convert class-40 heights to EGM2008. They use the same cached shift grids (`<cache_directory>/vshift_*.tif`) that validation uses to convert photons to the DEM's datum.

## Examples

```bash
# Default: all four tests with their default thresholds
ivert validate mydem.tif

# Turn the filters off (reproduces results from before they existed)
ivert validate mydem.tif -bf none

# Only the two tests that need no landmask
ivert validate mydem.tif -bf deep,reference

# Very clear water, where ICESat-2 can see the seafloor to about 40 m
ivert validate mydem.tif --bathy-max-depth 40

# Macrotidal coast: allow for up to about 6 m of tide below the geoid
ivert validate mydem.tif --bathy-near-surface 8

# Shallow banks far from any coast: only run 'offshore' where the reference is deeper than 10 m
ivert validate mydem.tif --bathy-offshore-min-ref-depth 10

# Use a finer local reference bathymetry, with a tighter tolerance to match
ivert validate mydem.tif --bathy-ref-raster local_bathy_egm2008.tif --bathy-ref-tolerance 10

# Change a default for all future runs
ivert options bathy_near_surface_m=8
```
