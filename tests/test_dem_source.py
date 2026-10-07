"""Tests for picking the elevation variable out of NetCDF and HDF5 DEMs, and checking it is georeferenced."""

import h5py
import netCDF4
import numpy as np
import pandas as pd
import pytest
import rasterio

from ivert import plot_validation_results, validate_dem, validate_dem_collection
from ivert.utils import dem_source
from ivert.validate_dem_collection import _resolve_dem_list


def _make_netcdf(path, variables: list[str]):
    """Write a small lat/lon grid with one 2-D variable per name."""
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("lat", 4)
        ds.createDimension("lon", 5)
        lat = ds.createVariable("lat", "f8", ("lat",))
        lat[:] = np.linspace(40.0, 40.3, 4)
        lat.units = "degrees_north"
        lon = ds.createVariable("lon", "f8", ("lon",))
        lon[:] = np.linspace(-105.0, -104.6, 5)
        lon.units = "degrees_east"
        for name in variables:
            var = ds.createVariable(name, "f4", ("lat", "lon"))
            var[:] = np.arange(20, dtype="f4").reshape(4, 5)
    return str(path)


def test_single_variable_file_is_used_as_is(tmp_path):
    """GDAL opens a one-variable file directly, so it isn't made a subdataset string."""
    path = _make_netcdf(tmp_path / "one.nc", ["Band1"])

    assert dem_source.resolve_dem_source(path) == path


@pytest.mark.parametrize(
    ("variables", "expected"),
    [
        (["uncert", "z", "elev"], "elev"),
        (["uncert", "z", "elevation"], "elevation"),
        (["uncert", "z"], "z"),
    ],
)
def test_default_names_are_tried_in_order(tmp_path, variables, expected):
    """A file that also holds an uncertainty grid still has its elevations picked."""
    path = _make_netcdf(tmp_path / "multi.nc", variables)

    resolved = dem_source.resolve_dem_source(path)

    assert resolved == f'NETCDF:"{path}":{expected}'


def test_multi_variable_file_without_a_default_name_is_an_error(tmp_path):
    """Guessing could validate the wrong grid, so the error asks for --variable."""
    path = _make_netcdf(tmp_path / "multi.nc", ["a", "b"])

    with pytest.raises(dem_source.DEMVariableError, match="--variable"):
        dem_source.resolve_dem_source(path)


def test_named_variable_is_used(tmp_path):
    """--variable picks even a variable the default names would pass over."""
    path = _make_netcdf(tmp_path / "multi.nc", ["elev", "uncert"])

    assert dem_source.resolve_dem_source(path, "uncert") == f'NETCDF:"{path}":uncert'


def test_named_variable_that_is_missing_is_an_error(tmp_path):
    """The error lists the variables the file has, so the user can pick one."""
    path = _make_netcdf(tmp_path / "multi.nc", ["elev", "uncert"])

    with pytest.raises(dem_source.DEMVariableError, match="elev, uncert"):
        dem_source.resolve_dem_source(path, "depth")


def test_named_variable_matching_a_single_variable_file(tmp_path):
    """Naming the only variable is fine; naming another is still an error."""
    path = _make_netcdf(tmp_path / "one.nc", ["Band1"])

    assert dem_source.resolve_dem_source(path, "Band1") == path
    with pytest.raises(dem_source.DEMVariableError, match="Band1"):
        dem_source.resolve_dem_source(path, "elev")


def test_resolved_subdataset_opens_as_a_one_band_raster(tmp_path):
    """The subdataset opens north-up with one band, as validate_dem expects."""
    path = _make_netcdf(tmp_path / "multi.nc", ["elev", "uncert"])

    with rasterio.open(dem_source.resolve_dem_source(path)) as ds:
        assert ds.count == 1
        assert ds.transform.e < 0


def test_file_path_and_base_name_of_a_subdataset():
    """Output names include the variable, so two variables of one file don't overwrite each other."""
    sds = 'NETCDF:"/data/dems/coast.nc":elev'

    assert dem_source.dem_file_path(sds) == "/data/dems/coast.nc"
    assert dem_source.dem_base_name(sds) == "coast_elev"
    assert dem_source.dem_base_name("/data/dems/coast.tif") == "coast"


def test_collection_listing_opens_no_files(tmp_path):
    """A NetCDF file isn't opened until its turn to be validated."""
    (tmp_path / "not_really.nc").write_bytes(b"not a netcdf file")

    assert _resolve_dem_list(str(tmp_path), None, None) == [
        str(tmp_path / "not_really.nc"),
    ]


def test_possible_base_names():
    """Before a file is opened, its results may be under any of these names."""
    assert dem_source.possible_base_names("/d/coast.tif") == ["coast"]
    assert dem_source.possible_base_names("/d/coast.nc") == [
        "coast",
        "coast_elev",
        "coast_elevation",
        "coast_z",
    ]
    assert dem_source.possible_base_names("/d/coast.nc", "depth") == [
        "coast",
        "coast_depth",
    ]
    assert dem_source.possible_base_names('NETCDF:"/d/coast.nc":z') == ["coast_z"]


def test_needs_validation_checks_names_without_opening_the_file(tmp_path):
    """Collections find what is left without opening every DEM; another --variable is not done yet."""
    dem = tmp_path / "coast.nc"
    dem.write_bytes(b"not a netcdf file")
    out = tmp_path / "out"
    out.mkdir()

    assert validate_dem.dem_needs_validation(str(dem), str(out))
    (out / "coast_elevation_results.h5").write_bytes(b"")
    assert not validate_dem.dem_needs_validation(str(dem), str(out))
    assert validate_dem.dem_needs_validation(str(dem), str(out), variable="depth")


def test_collection_logs_no_errors_when_every_dem_runs(tmp_path, monkeypatch, caplog):
    """The closing ERROR that counts the DEMs that didn't run is logged only when some didn't."""
    _make_netcdf(tmp_path / "a.nc", ["elev", "uncert"])
    _make_netcdf(tmp_path / "b.nc", ["Band1"])

    class _FakeDatabase:
        def open_gdf(self):
            pass

    monkeypatch.setattr(
        validate_dem_collection.ivert.icesat2_database_v2,
        "IS2Database",
        _FakeDatabase,
    )
    monkeypatch.setattr(
        validate_dem_collection.validate_dem,
        "validate_dem",
        lambda *_args, **_kwargs: None,
    )

    validate_dem_collection.validate_list_of_dems(
        str(tmp_path),
        output_dir=str(tmp_path / "out"),
    )

    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_collection_skips_a_file_without_an_elevation_variable(
    tmp_path,
    monkeypatch,
    caplog,
):
    """One bad file is skipped and named, then and again at the end, without stopping the run."""
    good = _make_netcdf(tmp_path / "a.nc", ["elev", "uncert"])
    _make_netcdf(tmp_path / "b.nc", ["a", "b"])
    validated = []

    class _FakeDatabase:
        def open_gdf(self):
            pass

    monkeypatch.setattr(
        validate_dem_collection.ivert.icesat2_database_v2,
        "IS2Database",
        _FakeDatabase,
    )
    monkeypatch.setattr(
        validate_dem_collection.validate_dem,
        "validate_dem",
        lambda dem_path, *_args, **_kwargs: validated.append(dem_path),
    )

    validate_dem_collection.validate_list_of_dems(
        str(tmp_path),
        output_dir=str(tmp_path / "out"),
    )

    assert validated == [f'NETCDF:"{good}":elev']
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    # Once when the file is skipped, and again as the run's last word.
    assert "b.nc has several variables (a, b)" in errors[0]
    assert errors[-1].startswith("1 of 2 DEMs did not run because of errors")
    assert errors[-1].endswith(": b.nc")


def _make_hdf5(path, datasets: list[str]):
    """Write one 4x5 float dataset per path ('elev', 'grid/elev', ...)."""
    with h5py.File(path, "w") as f:
        for name in datasets:
            f[name] = np.arange(20, dtype="f4").reshape(4, 5)
    return str(path)


def test_hdf5_default_name_is_found(tmp_path):
    """HDF5 files get the same default names as NetCDF."""
    path = _make_hdf5(tmp_path / "dem.h5", ["uncert", "z", "elevation"])

    assert dem_source.resolve_dem_source(path) == f'HDF5:"{path}"://elevation'


def test_hdf5_dataset_in_a_group_is_found_by_name_or_path(tmp_path):
    """HDF5 DEMs often keep their grids in groups; the last part of the path is enough if unique."""
    path = _make_hdf5(tmp_path / "dem.h5", ["grid/elev", "grid/uncert"])
    expected = f'HDF5:"{path}"://grid/elev'

    assert dem_source.resolve_dem_source(path) == expected
    assert dem_source.resolve_dem_source(path, "elev") == expected
    assert dem_source.resolve_dem_source(path, "grid/elev") == expected
    assert dem_source.resolve_dem_source(path, "/grid/elev") == expected


def test_hdf5_name_in_several_groups_needs_the_full_path(tmp_path):
    """A short name found in two groups is ambiguous, so the error lists both paths."""
    path = _make_hdf5(tmp_path / "dem.h5", ["a/elev", "b/elev"])

    with pytest.raises(dem_source.DEMVariableError, match="a/elev, b/elev"):
        dem_source.resolve_dem_source(path)
    assert dem_source.resolve_dem_source(path, "b/elev") == f'HDF5:"{path}"://b/elev'


def test_hdf5_missing_variable_is_an_error(tmp_path):
    """As with NetCDF, the error lists what the file has."""
    path = _make_hdf5(tmp_path / "dem.h5", ["elev", "uncert"])

    with pytest.raises(dem_source.DEMVariableError, match="elev, uncert"):
        dem_source.resolve_dem_source(path, "depth")


def test_single_dataset_hdf5_file(tmp_path):
    """Like a one-variable NetCDF file, it opens directly unless a dataset is named."""
    path = _make_hdf5(tmp_path / "dem.h5", ["height"])

    assert dem_source.resolve_dem_source(path) == path
    assert dem_source.resolve_dem_source(path, "height") == f'HDF5:"{path}"://height'
    with pytest.raises(dem_source.DEMVariableError):
        dem_source.resolve_dem_source(path, "elev")


def test_hdf5_subdataset_base_name_and_possible_names(tmp_path):
    """In 'grid/elev', the slash between HDF5 group and dataset becomes an underscore.

    Kept as a slash, it would put every output file under a subdirectory.
    """
    sds = 'HDF5:"/d/dem.h5"://grid/elev'

    assert dem_source.dem_file_path(sds) == "/d/dem.h5"
    assert dem_source.dem_base_name(sds) == "dem_grid_elev"
    assert dem_source.possible_base_names("/d/dem.h5", "grid/elev") == [
        "dem",
        "dem_grid_elev",
    ]


@pytest.mark.parametrize(
    ("dem_name", "expected"),
    [
        ("/d/dem.tif", "dem.tif"),
        ('NETCDF:"/d/dem.nc4":elevation', "dem.nc4:elevation"),
        ("NETCDF:/d/dem.nc:z", "dem.nc:z"),
        ('HDF5:"/d/dem.h5"://grid/elev', "dem.h5:grid/elev"),
        ('NETCDF:"/d/dem.h5":grid/elev', "dem.h5:grid/elev"),
    ],
)
def test_dem_display_name(dem_name, expected):
    """The name in logs and the collection CSV, without GDAL's driver prefix and quotes."""
    assert dem_source.dem_display_name(dem_name) == expected


def test_collection_table_names_dems_without_gdal_prefixes(tmp_path):
    """Two variables of one file must keep apart: the summary CSV groups by this name."""
    h5_files = []
    for i in range(3):
        h5_files.append(str(tmp_path / f"r{i}_results.h5"))
        pd.DataFrame({"diff_mean": [float(i)]}).to_hdf(h5_files[-1], key="results")

    data = plot_validation_results.get_data_from_h5_or_list(
        h5_files,
        orig_filenames=[
            f'NETCDF:"{tmp_path}/a.nc":elev',
            f'NETCDF:"{tmp_path}/a.nc":z',
            f"{tmp_path}/b.tif",
        ],
        include_filenames=True,
    )

    assert data["filename"].tolist() == ["a.nc:elev", "a.nc:z", "b.tif"]


def test_possible_base_names_include_a_dataset_in_a_group(tmp_path):
    """The default names can't guess 'grid/elev', so the file itself is read."""
    path = _make_hdf5(tmp_path / "dem.h5", ["grid/elev", "grid/uncert"])

    assert dem_source.possible_base_names(path)[-1] == "dem_grid_elev"


def test_netcdf4_file_named_h5_opens_through_the_netcdf_driver(tmp_path):
    """GDAL's HDF5 driver reads no coordinates; its NetCDF driver does."""
    path = _make_netcdf(tmp_path / "dem.h5", ["elev", "uncert"])
    expected = f'NETCDF:"{path}":elev'

    assert dem_source.resolve_dem_source(path) == expected
    assert dem_source.resolve_dem_source(path, "elev") == expected
    dem_source.check_georeferenced(expected)
    with rasterio.open(expected) as ds:
        np.testing.assert_allclose(ds.bounds, (-105.05, 39.95, -104.55, 40.35))


def test_plain_hdf5_stays_hdf5_and_is_not_georeferenced(tmp_path):
    """A bare HDF5 array has no coordinates, so it is refused rather than validated at 0, 0."""
    path = _make_hdf5(tmp_path / "dem.h5", ["grid/elev", "grid/uncert"])
    sds = dem_source.resolve_dem_source(path)

    assert sds == f'HDF5:"{path}"://grid/elev'
    with pytest.raises(dem_source.DEMNotGeoreferencedError, match="no georeferencing"):
        dem_source.check_georeferenced(sds)


def test_validate_dem_refuses_an_ungeoreferenced_dem(tmp_path):
    """It would otherwise read pixel indices as coordinates and find no photons."""
    path = _make_hdf5(tmp_path / "dem.h5", ["elev", "uncert"])

    with pytest.raises(dem_source.DEMNotGeoreferencedError):
        validate_dem.validate_dem(path, str(tmp_path / "out"))


def test_collection_skips_an_ungeoreferenced_dem(tmp_path, monkeypatch, caplog):
    """In a collection it is skipped and named, like any DEM that can't run, without stopping the run."""
    good = _make_netcdf(tmp_path / "a.nc", ["elev", "uncert"])
    _make_hdf5(tmp_path / "b.h5", ["elev", "uncert"])
    validated = []

    class _FakeDatabase:
        def open_gdf(self):
            pass

    monkeypatch.setattr(
        validate_dem_collection.ivert.icesat2_database_v2,
        "IS2Database",
        _FakeDatabase,
    )
    monkeypatch.setattr(
        validate_dem_collection.validate_dem,
        "validate_dem",
        lambda dem_path, *_args, **_kwargs: validated.append(dem_path),
    )

    validate_dem_collection.validate_list_of_dems(
        str(tmp_path),
        output_dir=str(tmp_path / "out"),
    )

    assert validated == [f'NETCDF:"{good}":elev']
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert "b.h5 has no georeferencing" in errors[0]
    assert errors[-1].endswith(": b.h5")


def test_collection_lists_hdf5_dems_but_not_ivert_results(tmp_path):
    """IVERT writes its results and photons as .h5, which a rerun must not take for DEMs."""
    for name in ("a.h5", "b.HDF5", "a_results.h5", "a_photons.h5", "run_results.h5"):
        (tmp_path / name).write_bytes(b"")

    assert [
        p.rsplit("/", 1)[1] for p in _resolve_dem_list(str(tmp_path), None, None)
    ] == [
        "a.h5",
        "b.HDF5",
    ]


def test_collection_csv_names_an_empty_dem_like_the_others(tmp_path, monkeypatch):
    """A DEM with no photons is listed by its DEM name, not its _EMPTY.txt file."""
    _make_netcdf(tmp_path / "a.nc", ["elev", "uncert"])
    _make_netcdf(tmp_path / "b.nc", ["elev", "uncert"])
    out = tmp_path / "out"
    out.mkdir()
    csv_args = {}

    class _FakeDatabase:
        def open_gdf(self):
            pass

    def _fake_validate_dem(dem_path, output_dir, *_args: object, **_kwargs: object):
        base = dem_source.dem_base_name(dem_path)
        if base.startswith("a"):
            pd.DataFrame({"diff_mean": [1.0], "numphotons_intd": [5]}).to_hdf(
                f"{output_dir}/{base}_results.h5",
                key="results",
            )
        else:
            (out / f"{base}_results_EMPTY.txt").write_text("")

    def _fake_csv(_df, empty_dems, _csv_name):
        csv_args["empty"] = list(empty_dems)

    def _do_nothing(*_args: object, **_kwargs: object):
        return None

    collection = validate_dem_collection
    monkeypatch.setattr(
        collection.ivert.icesat2_database_v2,
        "IS2Database",
        _FakeDatabase,
    )
    monkeypatch.setattr(collection.validate_dem, "validate_dem", _fake_validate_dem)
    monkeypatch.setattr(collection, "write_summary_csv_file", _fake_csv)
    monkeypatch.setattr(
        collection.validate_dem,
        "write_summary_stats_file",
        _do_nothing,
    )
    monkeypatch.setattr(
        collection.plot_validation_results,
        "plot_histograms_and_line",
        _do_nothing,
    )
    monkeypatch.setattr(
        collection.ivert.bathy_filters,
        "read_report_from_h5",
        _do_nothing,
    )
    monkeypatch.setattr(
        collection.ivert.bathy_filters.BathyFilterReport,
        "combine",
        _do_nothing,
    )
    monkeypatch.setattr(
        collection.ivert.bathy_filters,
        "write_report_to_h5",
        _do_nothing,
    )

    collection.validate_list_of_dems(str(tmp_path), output_dir=str(out))

    assert csv_args["empty"] == ["b.nc:elev"]
