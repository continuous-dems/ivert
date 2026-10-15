"""Shared fixtures for the IVERT test suite.

The autouse fixture below keeps every test off the developer's real config.
make_standin_db builds small photon databases from photons given in the test.
"""

from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from types import SimpleNamespace

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


@pytest.fixture
def make_standin_db(tmp_path) -> Callable[..., StandInDatabase]:
    """Return a function making StandInDatabases, each in tmp_path/<name>."""

    def make(name: str = "db", vertical_datum: str = "ellipsoid") -> StandInDatabase:
        return StandInDatabase(tmp_path / name, vertical_datum)

    return make
