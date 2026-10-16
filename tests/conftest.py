"""Shared fixtures for the IVERT test suite.

The autouse fixtures below keep every test off the developer's real config and off
the network. make_standin_db builds small photon databases from photons given in the
test. write_dem writes small DEMs, and synthetic_region puts photons, a landmask and
a reference raster under them, so a real validation can run end to end offline.
"""

import ipaddress
import os
import socket
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import numpy.typing as npt
import pandas as pd
import pytest
import shapely


@pytest.fixture(autouse=True)
def isolate_ivert_state(tmp_path, monkeypatch):
    """Keep every test off the real ~/.ivert."""
    # Config.user_config_path honors IVERT_USER_CONFIG ahead of the packaged
    # default, so setting it redirects reads *and* writes into a per-test
    # temporary directory.
    monkeypatch.setenv(
        "IVERT_USER_CONFIG",
        str(tmp_path / "ivert_user_config.ini"),
    )

    # Config.__init__ switches to the [AWS] section based on this, so pin it
    # rather than letting the answer differ between a laptop and a CI runner.
    monkeypatch.setattr("ivert.utils.is_aws.is_aws", lambda: False)


_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo


def _is_loopback(host: object) -> bool:
    """Return True for localhost or a loopback address."""
    if host in {"localhost", b"localhost"}:
        return True
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def block_network(request, tmp_path, monkeypatch):
    """Fail any test not marked 'network' that tries to reach the internet.

    Each attempt is refused and written to a file, and the test fails at the end if
    there were any. Raising alone isn't enough: libraries such as fetchez catch the
    error and carry on, and attempts in forked worker processes are only seen through
    the file.
    """
    if request.node.get_closest_marker("network"):
        yield
        return

    attempts = tmp_path / "network_attempts.txt"

    def refuse(what: object) -> None:
        with attempts.open("a", encoding="utf-8") as f:
            f.write(f"{what}\n")
        msg = f"Network use is blocked in tests: {what}"
        raise OSError(msg)

    def connect(self: socket.socket, address: object) -> None:
        if self.family in {socket.AF_INET, socket.AF_INET6} and not _is_loopback(
            address[0],  # type: ignore[index]
        ):
            refuse(address)
        _real_connect(self, address)  # type: ignore[arg-type]

    def getaddrinfo(host: object, *args: object, **kwargs: object) -> list:
        if not _is_loopback(host):
            refuse(host)
        return _real_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
    if attempts.exists():
        pytest.fail(
            "The test tried to use the network (mark it 'network' if it must):\n"
            + attempts.read_text(encoding="utf-8"),
        )


class FakeGlobatoStream:
    """Stands in for the stream globato.read() returns.

    Yields ``chunks`` and records the hooks chained onto it as (name, kwargs)
    pairs in ``hooks``.
    """

    def __init__(self) -> None:
        """Start with no photons and nothing recorded."""
        self.chunks: list = []
        self.hooks: list[tuple[str, dict]] = []

    def read(self, *_args: object, **_kwargs: object) -> "FakeGlobatoStream":
        """Take globato.read()'s place."""
        return self

    def pipe(self, name: str, **kwargs: object) -> "FakeGlobatoStream":
        """Record a hook chained onto the stream."""
        self.hooks.append((name, kwargs))
        return self

    def __iter__(self) -> Iterator:
        """Yield the photon chunks set in ``chunks``."""
        return iter(self.chunks)


@pytest.fixture
def fake_globato_read(monkeypatch):
    """Replace globato.read() with a FakeGlobatoStream; its ``chunks`` feed photons."""
    from ivert import icesat2_database_v2 as is2db  # noqa: PLC0415 - slow import

    stream = FakeGlobatoStream()
    monkeypatch.setattr(is2db.globato, "read", stream.read)
    return stream


class StandInDatabase:
    """A small IVERT photon database in a directory of its own, built in the test.

    Granule files are written straight from the photons given, as a download would
    write them, so nothing is fetched or classified. Call reindex() after adding
    files to build the database index from them.
    """

    def __init__(self, root: Path, vertical_datum: str = "ellipsoid") -> None:
        """Set up an empty database, with its landmask store, under root."""
        from ivert import icesat2_database_v2 as is2db  # noqa: PLC0415 - slow import

        config = SimpleNamespace(
            ivert_database_directory=str(root / "db"),
            ivert_landmask_directory=str(root / "landmasks"),
            icesat2_download_directory=str(root / "cache"),
            icesat2_vertical_datum=vertical_datum,
            nsidc_atl_version="007",
        )
        self.db = is2db.IS2Database(ivert_config=config)
        self._granules_written = 0

    @staticmethod
    def photons(
        points: Iterable[tuple[float, ...]],
        *,
        date: int = 20230101,
        class_code: int = 1,
    ) -> pd.DataFrame:
        """Return a classified-photon table, one photon per (x, y) or (x, y, z).

        The photons are a second apart from the start of 'date', all of one class,
        with a height of 1.0 where none is given.
        """
        from ivert import icesat2_database_v2 as is2db  # noqa: PLC0415 - slow import

        points = [(*p, 1.0) if len(p) == 2 else tuple(p) for p in points]
        t0 = is2db._yyyymmdd_to_delta_time(date) + 1.0
        return pd.DataFrame(
            {
                "x": [p[0] for p in points],
                "y": [p[1] for p in points],
                "z": [p[2] for p in points],
                "class_code": [class_code] * len(points),
                "delta_time": [t0 + i for i in range(len(points))],
                "confidence": [4] * len(points),
                # 4-byte beam names, as globato hands them over.
                "laser": [b"gt1l"] * len(points),
            },
        )

    def add_granule(
        self,
        query_bbox: tuple,
        points: Iterable[tuple[float, ...]],
        *,
        date: int = 20230101,
        class_code: int = 1,
        vertical_datum: str = "EPSG:4979",
    ) -> Path:
        """Write a granule file of these photons, covering query_bbox.

        query_bbox is (xmin, xmax, ymin, ymax, tmin, tmax), the dates YYYYMMDD.
        Each granule gets a source granule name of its own. Returns the file's path.
        """
        self._granules_written += 1
        n = self._granules_written
        stem = f"ATL03_{date}{n:06d}_{n:04d}0101_007_01_subsetted"
        self.db.granules_dir.mkdir(parents=True, exist_ok=True)
        path = self.db.granules_dir / self.db._nc_filename(stem + ".h5", query_bbox)
        self.db._write_nc(
            self.photons(points, date=date, class_code=class_code),
            stem + ".h5",
            str(path),
            query_bbox,
            vertical_datum,
        )
        return path

    def add_landmask(self, bbox: tuple[float, float, float, float]) -> Path:
        """Store the landmask of an (xmin, xmax, ymin, ymax) box: land but its rim.

        The land stops 0.05 short of each edge. Returns the file's path.
        """
        from ivert import landmask  # noqa: PLC0415 - slow import

        xmin, xmax, ymin, ymax = bbox
        path = landmask.store_filename(self.db.landmask_dir, bbox)
        landmask.write_land(
            path,
            shapely.box(xmin + 0.05, ymin + 0.05, xmax - 0.05, ymax - 0.05),
        )
        return path

    def reindex(self) -> object:
        """Build the database index from the granule files; returns its GeoDataFrame."""
        return self.db.create_new_database(populate=True, overwrite=True)

    def use_as_default(self, **settings: object) -> None:
        """Make this the database IVERT opens by default, as a CLI command does.

        Writes its directories to the test's user config file, which
        isolate_ivert_state points IVERT at, so IS2Database() opens it. Its download
        cache goes there too, so nothing reads files cached under the real ~/.ivert.
        Any other settings given are written as well.
        """
        config = self.db.config
        lines = [
            "[DEFAULT]",
            f"ivert_database_directory = {config.ivert_database_directory}",
            f"ivert_landmask_directory = {config.ivert_landmask_directory}",
            f"icesat2_download_directory = {config.icesat2_download_directory}",
            *(f"{key} = {value}" for key, value in settings.items()),
        ]
        Path(os.environ["IVERT_USER_CONFIG"]).write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )


@pytest.fixture
def make_standin_db(tmp_path) -> Callable[..., StandInDatabase]:
    """Return a function making StandInDatabases, each in tmp_path/<name>."""

    def make(name: str = "db", vertical_datum: str = "ellipsoid") -> StandInDatabase:
        return StandInDatabase(tmp_path / name, vertical_datum)

    return make


# The CRS of DEMs from write_dem: WGS84 with EGM2008 heights, the vertical datum of
# synthetic_region's photons, so validating one transforms no heights.
DEM_CRS = "EPSG:4326+3855"
DEM_NODATA = -9999.0


def _write_dem(
    path: str | Path,
    values: npt.ArrayLike | None = None,
    *,
    west: float = -104.53,
    north: float = 39.53,
    cell_size: float = 0.01,
    crs: str | None = DEM_CRS,
    nodata: float | None = DEM_NODATA,
) -> Path:
    """Write a one-band float32 GeoTIFF DEM; returns its path.

    'values' is the grid of elevations, top row first (3 x 3 of 10.0 if None). The
    top-left corner is at (west, north), in degrees.
    """
    import rasterio  # noqa: PLC0415 - slow import
    from rasterio.transform import from_origin  # noqa: PLC0415 - slow import

    grid = np.full((3, 3), 10.0) if values is None else np.asarray(values, dtype=float)
    path = Path(path)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=grid.shape[1],
        height=grid.shape[0],
        count=1,
        dtype="float32",
        crs=crs,
        transform=from_origin(west, north, cell_size, cell_size),
        nodata=nodata,
    ) as ds:
        ds.write(grid.astype("float32"), 1)
    return path


@pytest.fixture
def write_dem() -> Callable[..., Path]:
    """Return a function writing a small GeoTIFF DEM (see _write_dem)."""
    return _write_dem


class SyntheticRegion:
    """Photons, a landmask and reference bathymetry for real validations, offline.

    One database storage tile (TILE) holds a 0.06-degree square of photons centred
    on COAST_X, 39.5: ground photons near LAND_Z west of COAST_X and seafloor
    photons near SEA_Z east of it, 900 of each, about 25 to a 0.01-degree cell. The
    landmask is land west of COAST_X, and the reference bathymetry is REF_Z all
    over the tile. Heights are EGM2008, so nothing needs a datum grid.

    The user config points IVERT at all of it, so validate_dem() and
    validate_list_of_dems() run against it unchanged. The square spans x
    -104.53..-104.47 and y 39.47..39.53: LAND_DEM and SEA_DEM are (west, north)
    corners of 3 x 3 DEMs, at the default 0.01-degree cells, inside it.
    """

    TILE = (-105.0, -104.0, 39.0, 40.0, 20230101, 20230131)
    COAST_X = -104.5
    LAND_Z = 10.0
    SEA_Z = -8.0
    REF_Z = -10.0
    LAND_DEM = (-104.53, 39.53)
    SEA_DEM = (-104.50, 39.53)

    def __init__(self, standin: StandInDatabase, root: Path) -> None:
        """Fill the stand-in database and point the user config at it."""
        from ivert import landmask  # noqa: PLC0415 - slow import

        self.standin = standin
        self.root = root
        rng = np.random.default_rng(0)
        y = np.linspace(39.4705, 39.5295, 30)
        for x0, z, class_code in (
            (self.COAST_X - 0.03, self.LAND_Z, 1),
            (self.COAST_X, self.SEA_Z, 40),
        ):
            xx, yy = np.meshgrid(np.linspace(x0 + 0.0005, x0 + 0.0295, 30), y)
            zz = z + rng.normal(0.0, 0.1, xx.size)
            standin.add_granule(
                self.TILE,
                zip(xx.ravel(), yy.ravel(), zz, strict=True),
                date=self.TILE[4],
                class_code=class_code,
                vertical_datum="EPSG:3855",
            )
        xmin, _, ymin, ymax = self.TILE[:4]
        landmask.write_land(
            landmask.store_filename(standin.db.landmask_dir, self.TILE[:4]),
            shapely.box(xmin, ymin, self.COAST_X, ymax),
        )
        standin.reindex()

        self.ref_raster = _write_dem(
            root / "reference_bathymetry.tif",
            np.full((100, 100), self.REF_Z),
            west=xmin,
            north=ymax,
            crs="EPSG:4326",
        )
        standin.use_as_default(bathy_ref_raster=self.ref_raster)

    def dem(
        self,
        name: str,
        corner: tuple[float, float] | None = None,
        values: npt.ArrayLike | None = None,
        **kwargs: object,
    ) -> Path:
        """Write a DEM to root/dems/name; returns its path.

        'corner' is its (west, north) corner, LAND_DEM if None. 'values' defaults to
        the height of the photons at that corner, LAND_Z or SEA_Z, in every cell.
        Other keyword arguments go to write_dem.
        """
        west, north = corner or self.LAND_DEM
        if values is None:
            z = self.LAND_Z if west < self.COAST_X else self.SEA_Z
            values = np.full((3, 3), z)
        dem_dir = self.root / "dems"
        dem_dir.mkdir(exist_ok=True)
        return _write_dem(dem_dir / name, values, west=west, north=north, **kwargs)


@pytest.fixture
def synthetic_region(make_standin_db, tmp_path) -> SyntheticRegion:
    """A SyntheticRegion: a filled stand-in database set as IVERT's default."""
    return SyntheticRegion(make_standin_db("db", vertical_datum="geoid"), tmp_path)
