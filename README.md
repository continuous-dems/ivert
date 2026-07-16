# IVERT
**ICESat-2 Validation of Elevations Reporting Tool**

IVERT validates Digital Elevation Models (DEMs) by comparing their elevations against ICESat-2 satellite photon data. It supports topographic, bathymetric, and mixed coastal DEMs, runs fully offline on any machine, and handles vertical datum conversions automatically.

Developed by the [CIRES Coastal DEM Team](https://ciresdem.github.io). Primary authors: [Mike MacFerrin](https://github.com/mmacferrin) (IVERT) and [Matthew Love](https://github.com/matth-love) ([continuous-dems](https://github.com/continuous-dems) utilities).

> **Note:** This repository will soon be ported over to the [continuous-dems](https://github.com/continuous-dems) ecosystem.

---

(TODO: Add some pretty pictures here)

---

## Capabilities

- Validate topographic, bathymetric, and coastal DEMs against ICESat-2 ATL03/ATL24 photons
- Combines [ATL03](https://nsidc.org/data/atl03/)/[ATL08](https://nsidc.org/data/atl08/)/[ATL13](https://nsidc.org/data/atl13/)/[ATL24](https://nsidc.org/data/atl24/) for photon-level classifications, plus external datasets (such as the [Global Buildings Atlas](https://tubvsig-so2sat-vm1.srv.mwn.de/)) to classify built structures.
- Additional coastline filtering to minimize false-positive ATL08 "ground" photons appearing offshore over water, and unreasonably-shallow "bathy floor" photons with large errors over deep water.
- Automatic vertical datum conversions (NAVD88, EGM2008, MLLW, and many more)
- Configurable photon confidence and quality filtering
- Statistical outputs: bias, RMSE, NMAD, per-cell error maps
- Automatically-generated plots of DEM accuracies compared to ICESat-2.
- Export errors to GeoTIFF, GeoPackage, Shapefile, or XYZ text
- Local photon database management — download once, validate many times

---

## Installation

```bash
pip install ivert
```
(^ coming soon)

For development (editable install from this repo):

```bash
git clone https://github.com/ciresdem/IVERT.git
cd IVERT
pip install -e .
```

Three dependencies — `fetchez`, `globato`, and `transformez` — are pulled automatically from the [continuous-dems](https://github.com/continuous-dems) GitHub organization and do not need to be installed separately.

---

## Quick start

**1. Download ICESat-2 photon data for your area** (bounding box in W/E/S/N order):

```bash
ivert database download -- -74.0/-73.0/40.5/41.0
```

**2. Validate your DEM:**

```bash
ivert validate mydem.tif
```

**3. Check the output directory** for `mydem_results.h5`, a validation plot (`.png`), and error exports (`.tif`, `.gpkg`).

---

## Documentation

| Command | Description |
|---------|-------------|
| [ivert validate](docs/validate.md) | Validate DEMs against ICESat-2 data |
| [ivert database](docs/database.md) | Download and manage the local photon database |
| [ivert cache](docs/cache.md) | View and clear the local file cache |
| [ivert options](docs/options.md) | View and change configuration settings |
| [ivert upgrade](docs/upgrade.md) | Upgrade IVERT to the latest version |
