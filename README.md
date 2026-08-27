# IVERT
**ICESat-2 Validation of Elevations Reporting Tool**

IVERT validates Digital Elevation Models (DEMs) by comparing their elevations against ICESat-2 satellite photon data. It supports topographic, bathymetric, and mixed coastal DEMs, runs fully offline on any machine, and handles vertical datum conversions automatically.

Developed by the [continuous-dems team](https://github.com/continuous-dems). Primary authors: [Mike MacFerrin](https://github.com/mmacferrin) (IVERT) and [Matthew Love](https://github.com/matth-love) ([continuous-dems](https://github.com/continuous-dems) utilities).

---
## Capabilities

- Validate topographic, bathymetric, and mixed coastal DEMs against ICESat-2 photons — a single DEM, or a whole directory/glob of DEMs with combined collection-level summaries.
- Classifies photons by surface type — ground, canopy, land ice, buildings, seafloor, and water surfaces — by combining multiple ICESat-2 products ([ATL03](https://nsidc.org/data/atl03/), [ATL06](https://nsidc.org/data/atl06/), [ATL08](https://nsidc.org/data/atl08/), [ATL12](https://nsidc.org/data/atl12/), [ATL13](https://nsidc.org/data/atl13/), [ATL24](https://nsidc.org/data/atl24/)) with a Bing-derived building-footprint mask to flag built structures.
- Coastline/water filtering to minimize false-positive "ground" photons appearing offshore over water and unreasonably-shallow "bathy floor" photons with large errors over deep water.
- Automatic vertical datum conversion (NAVD88, EGM2008, MLLW, ellipsoid, and many more, by EPSG code or short name) — no manual reprojection needed.
- Configurable photon filtering at both download and validation: signal confidence, bathymetry confidence, photon class selection, and outlier rejection.
- Statistical outputs: mean bias, RMSE, standard deviation, a full percentile breakdown of per-cell errors, and optional per-cell photon coverage.
- Automatically-generated validation plots (per-DEM and collection-wide).
- Export per-cell errors to GeoTIFF, GeoPackage, Shapefile, or XYZ text.
- Local, spatially-indexed photon database — download once, validate many times — with commands to list, size, rebuild, and delete cached data.
- Export classified photons from the database to GeoPackage, Shapefile, or XYZ text.
- Runs fully offline on any machine, with configurable data directories and per-project config profiles.

---

## Installation

Available on [PyPI](https://pypi.org/project/ivert/):

```bash
pip install ivert
```

or on [conda-forge](https://anaconda.org/conda-forge/ivert):

```bash
conda install ivert
```

For development (editable install from this repo):

```bash
git clone https://github.com/continuous-dems/ivert.git
cd ivert
pip install -e .
```

Three dependencies — `fetchez`, `globato`, and `transformez` — are pulled automatically from the [continuous-dems](https://github.com/continuous-dems) GitHub organization and do not need to be installed separately.

---

## Quick start

**1. Set up IVERT's data directories and credentials** (run once on a new machine):

```bash
ivert setup
```

This creates the local `~/.ivert` data directories and checks your `~/.netrc` for NASA Earthdata Login credentials, offering to save them if they are not already present. Earthdata credentials are required to download ICESat-2 data ([register for a free account](https://urs.earthdata.nasa.gov/)).

**2. Download ICESat-2 photon data for your area** (bounding box in W/E/S/N order):

```bash
ivert database download -- -74.0/-73.0/40.5/41.0
```

**3. Validate your DEM:**

```bash
ivert validate mydem.tif
```

**4. Check the output directory** for `mydem_results.h5`, a validation plot (`.png`), and error exports (`.tif`, `.gpkg`).

---

## Documentation

| Command | Description |
|---------|-------------|
| [ivert setup](docs/setup.md) | Create data directories and set up NASA Earthdata credentials |
| [ivert validate](docs/validate.md) | Validate DEMs against ICESat-2 data |
| [ivert database](docs/database.md) | Download, export, and manage the local photon database |
| [ivert classes](docs/classes.md) | List the ICESat-2 photon classification codes |
| [ivert cache](docs/cache.md) | View and clear the local file cache |
| [ivert options](docs/options.md) | View and change configuration settings |
