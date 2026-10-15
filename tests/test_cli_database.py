"""'ivert database dump', 'restore' and 'download', run as a user runs them.

Each command opens the configured database, which here is a StandInDatabase (see
conftest.py) made the default. 'download' never reaches the network: the download
itself is replaced with a stand-in that records what it was asked for.
"""

import datetime
import zipfile
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
import rasterio.transform
from click.testing import CliRunner

from ivert import cli
from ivert import database_archive as da
from ivert import icesat2_database_v2 as is2db

DATES = (20230101, 20230201)
WEST = (0.0, 2.0, 0.0, 2.0, *DATES)
EAST = (2.0, 4.0, 0.0, 2.0, *DATES)


def _run(*args: str, stdin=None):
    """Run 'ivert database ...' in this process; return the click Result."""
    return CliRunner().invoke(cli.ivert_cli, ["database", *args], input=stdin)


@pytest.fixture
def standin(make_standin_db):
    """The default database: two granules side by side, under one landmask."""
    standin = make_standin_db("db")
    standin.add_granule(WEST, [(0.5, 0.5), (1.5, 1.5)])
    standin.add_granule(EAST, [(2.5, 0.5), (3.5, 1.5)])
    standin.add_landmask((0.0, 4.0, 0.0, 2.0))
    standin.reindex()
    standin.use_as_default()
    return standin


# ---------------------------------------------------------------------------
# dump
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("standin")
def test_dump_adds_zip_to_an_output_name_without_a_suffix(tmp_path):
    """'-o archive' writes archive.zip, and says how big it is."""
    result = _run("dump", "-o", str(tmp_path / "archive"))

    assert result.exit_code == 0, result.output
    assert (tmp_path / "archive.zip").is_file()
    assert f"Wrote {tmp_path / 'archive.zip'} (" in result.output


@pytest.mark.usefixtures("standin")
def test_dump_into_a_directory_uses_a_dated_name(tmp_path):
    """An existing directory gets ivert_database_<today>.zip written into it."""
    today = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d")

    result = _run("dump", "-o", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert (tmp_path / f"ivert_database_{today}.zip").is_file()


@pytest.mark.usefixtures("standin")
def test_dump_overwrites_an_archive_only_when_told_to(tmp_path):
    """An existing archive is kept unless -ow/--overwrite is given."""
    out = tmp_path / "archive.zip"
    out.write_text("not a zip")

    refused = _run("dump", "-o", str(out))
    replaced = _run("dump", "-o", str(out), "-ow")

    assert refused.exit_code == 1
    assert "already exists. Use -ow/--overwrite" in refused.output
    assert replaced.exit_code == 0, replaced.output
    assert zipfile.is_zipfile(out)


@pytest.mark.usefixtures("standin")
def test_dump_of_a_region_and_dates_records_them_in_the_manifest(tmp_path):
    """The bbox and -ds/-de limit the dump; open-ended dates are filled in."""
    out = tmp_path / "part.zip"

    result = _run("dump", "-o", str(out), "-ds", "2023.01.01", "--", "1/3/0/2")

    assert result.exit_code == 0, result.output
    region = da.read_manifest(out)["region"]
    assert region == {
        "rectangles": [[1.0, 3.0, 0.0, 2.0]],
        "date_range": [20230101, cli._EXPORT_DATE_MAX],
    }


@pytest.mark.usefixtures("standin")
@pytest.mark.parametrize(
    ("dates", "message"),
    [
        (["-ds", "2023.02.01", "-de", "2023.01.01"], "--start-date (20230201) must"),
        (["-de", "not a date at all"], "Could not parse date"),
    ],
)
def test_dump_rejects_bad_dates(tmp_path, dates, message):
    """Dates that can't be read, or are in the wrong order, stop the dump."""
    result = _run("dump", "-o", str(tmp_path / "out.zip"), *dates)

    assert result.exit_code == 1
    assert message in result.output
    assert not (tmp_path / "out.zip").exists()


def test_dump_of_one_granule_file_is_a_usage_error(standin, tmp_path):
    """A single .nc file is something to convert, not a region to dump."""
    granule = next(standin.db.granules_dir.glob("ATL03_*.nc"))

    result = _run("dump", "-o", str(tmp_path / "out.zip"), str(granule))

    assert result.exit_code == 2
    assert "not a single .nc file" in result.output


@pytest.mark.usefixtures("standin")
def test_dump_with_nothing_in_the_region_reports_it(tmp_path):
    """The archive's own error reaches the user as a plain message."""
    result = _run("dump", "-o", str(tmp_path / "out.zip"), "--", "10/11/10/11")

    assert result.exit_code == 1
    assert "No photons in the database fall in the requested region" in result.output


@pytest.fixture
def large(monkeypatch):
    """Make the four-photon stand-in count as a very large database."""
    monkeypatch.setattr(cli, "_EXPORT_WARN_PHOTON_THRESHOLD", 1)


@pytest.mark.usefixtures("standin", "large")
def test_dumping_a_large_database_asks_first(tmp_path):
    """Declining the prompt writes nothing."""
    result = _run("dump", "-o", str(tmp_path / "out.zip"), stdin="n\n")

    assert result.exit_code == 1
    assert "the archive may be very large" in result.output
    assert "Continue with the dump anyway?" in result.output
    assert not (tmp_path / "out.zip").exists()


@pytest.mark.usefixtures("standin", "large")
@pytest.mark.parametrize("args", [["-f"], ["--", "1/3/0/2"]])
def test_a_forced_or_regional_dump_of_a_large_database_does_not_ask(tmp_path, args):
    """-f skips the question, and a region is assumed to be smaller."""
    result = _run("dump", "-o", str(tmp_path / "out.zip"), *args)

    assert result.exit_code == 0, result.output
    assert "Continue with the dump anyway?" not in result.output


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------
@pytest.fixture
def archive(make_standin_db, tmp_path):
    """An archive of one granule at x 1-2, inside the default database's west one."""
    other = make_standin_db("other")
    other.add_granule((1.0, 2.0, 0.0, 2.0, *DATES), [(1.25, 1.25), (1.75, 1.75)])
    other.add_landmask((1.0, 2.0, 0.0, 2.0))
    other.reindex()
    return da.dump(tmp_path / "other.zip", db=other.db).path


def test_restore_without_overlap_imports_everything(make_standin_db, archive):
    """Into a database it doesn't overlap, the archive goes in without a question."""
    make_standin_db("empty").use_as_default()

    result = _run("restore", str(archive))

    assert result.exit_code == 0, result.output
    assert (
        "Restored other.zip (all): 1 granule files added; 1 landmask tiles added. "
        "The database index now lists 1 granule files."
    ) in result.output


@pytest.mark.usefixtures("standin")
def test_restore_lists_the_overlap_and_obeys_on_overlap(archive):
    """With -oo there is no question; what was done is reported."""
    result = _run("restore", str(archive), "-oo", "keep")

    assert result.exit_code == 0, result.output
    assert "1 of the archive's query boxes overlap data" in result.output
    assert "W 1 to E 2, S 0 to N 2, dates 20230101 to 20230201" in result.output
    assert "0 granule files added; 1 skipped as already present" in result.output


@pytest.mark.usefixtures("standin")
def test_restore_replace_reports_the_existing_files_it_replaced(archive):
    """The existing file is cut around the imported one, and that is reported."""
    result = _run("restore", str(archive), "-oo", "replace")

    assert result.exit_code == 0, result.output
    assert (
        "1 granule files added; 1 existing files replaced (1 of them kept outside "
        "the archive's area)"
    ) in result.output


def test_restore_dry_run_changes_nothing(standin, archive):
    """--dry-run shows the overlap and stops."""
    before = sorted(standin.db.granules_dir.iterdir())

    result = _run("restore", str(archive), "--dry-run")

    assert result.exit_code == 0, result.output
    assert "1 of the archive's query boxes overlap" in result.output
    assert "Dry run: nothing was changed." in result.output
    assert sorted(standin.db.granules_dir.iterdir()) == before


def test_restore_cancel_imports_nothing(standin, archive):
    """'-oo cancel' leaves the database as it was."""
    before = sorted(standin.db.granules_dir.iterdir())

    result = _run("restore", str(archive), "-oo", "cancel")

    assert result.exit_code == 0, result.output
    assert "Nothing was imported." in result.output
    assert sorted(standin.db.granules_dir.iterdir()) == before


@pytest.mark.usefixtures("standin")
def test_restore_needs_on_overlap_when_it_cannot_ask(archive):
    """With no terminal to ask on, an overlap without -oo is a usage error."""
    result = _run("restore", str(archive))

    assert result.exit_code == 2
    assert "Rerun with -oo/--on-overlap all|keep|replace|cancel" in result.output


@pytest.mark.usefixtures("standin")
def test_restore_lists_only_the_first_overlaps(archive, monkeypatch):
    """A long list of overlaps is cut short, with a count of the rest."""
    monkeypatch.setattr(cli, "_MAX_OVERLAPS_LISTED", 0)

    result = _run("restore", str(archive), "--dry-run")

    assert "W 1 to E 2, S 0 to N 2, dates 20230101" not in result.output
    assert "  ... and 1 more" in result.output


def test_restore_refuses_another_vertical_datum(make_standin_db, archive):
    """The archive's error reaches the user as a plain message."""
    other = make_standin_db("ngvd")
    other.add_granule(WEST, [(0.5, 0.5)], vertical_datum="EPSG:5702")
    other.reindex()
    other.use_as_default()

    result = _run("restore", str(archive), "-oo", "all")

    assert result.exit_code == 1
    assert "Mixing them would give wrong validations" in result.output


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------
@pytest.fixture
def downloads(monkeypatch):
    """Record each download request instead of making it; returns the records.

    Set the 'summary' attribute of the list to change what the download reports.
    """

    class Requests(list):
        summary = SimpleNamespace(parts_failed=0, parts_attempted=1, granules_added=1)

    requests = Requests()

    def download(_self, bbox, **kwargs: object):
        requests.append((bbox, kwargs))
        return requests.summary

    monkeypatch.setattr(is2db.IS2Database, "download_new_granules", download)
    monkeypatch.setattr(cli, "_setup_earthdata_credentials", lambda **_kw: None)
    return requests


DOWNLOAD_DATES = ["-ds", "2023.01.01", "-de", "2023.02.01"]


@pytest.mark.usefixtures("standin")
@pytest.mark.parametrize(
    ("args", "bbox"),
    [
        (["--", "-120/-119/35/36"], (-120.0, -119.0, 35.0, 36.0)),
        (["--wsen", "--", "-120/35/-119/36"], (-120.0, -119.0, 35.0, 36.0)),
        (["--", "-120", "-119", "35", "36"], (-120.0, -119.0, 35.0, 36.0)),
    ],
)
def test_download_of_a_bbox(downloads, args, bbox):
    """A bbox in either order, slashed or not, is one request with the dates."""
    result = _run("download", *DOWNLOAD_DATES, *args)

    assert result.exit_code == 0, result.output
    [(requested, kwargs)] = downloads
    assert requested == (*bbox, *DATES)
    assert kwargs["geometry"] is None


@pytest.mark.usefixtures("standin")
def test_download_passes_the_options_on(downloads):
    """Classes, confidence levels and --replace reach the download."""
    result = _run(
        "download",
        *DOWNLOAD_DATES,
        "-c",
        "1/40",
        "-cl",
        "3",
        "-bc",
        "0.5",
        "-r",
        "--",
        "-120/-119/35/36",
    )

    assert result.exit_code == 0, result.output
    [(_, kwargs)] = downloads
    assert kwargs == {
        "classes_to_keep": (1, 40),
        "min_confidence_level": 3,
        "min_bathy_confidence": 0.5,
        "replace": True,
        "geometry": None,
    }


@pytest.mark.usefixtures("standin")
def test_download_of_a_projected_bbox_is_converted_to_wgs84(downloads):
    """-p gives the CRS of the bbox's coordinates."""
    result = _run(
        "download",
        *DOWNLOAD_DATES,
        "-p",
        "EPSG:26910",
        "--",
        "500000/510000/4000000/4010000",
    )

    assert result.exit_code == 0, result.output
    [(requested, _)] = downloads
    expected = (-123.0, -122.8887, 36.1447, 36.2349)
    assert requested[:4] == pytest.approx(expected, abs=1e-4)


@pytest.mark.usefixtures("standin")
def test_download_of_a_dem_covers_its_footprint(downloads, tmp_path):
    """A DEM file's extent is the region, sent as a geometry as well as a bbox."""
    dem = tmp_path / "dem.tif"
    with rasterio.open(
        dem,
        "w",
        driver="GTiff",
        width=5,
        height=4,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=rasterio.transform.from_origin(-105.0, 40.3, 0.1, 0.1),
    ) as dst:
        dst.write(np.zeros((1, 4, 5), dtype="f4"))

    result = _run("download", *DOWNLOAD_DATES, str(dem))

    assert result.exit_code == 0, result.output
    [(requested, kwargs)] = downloads
    assert requested[:4] == pytest.approx((-105.0, -104.5, 39.9, 40.3))
    assert kwargs["geometry"] is not None


@pytest.mark.usefixtures("standin")
def test_download_of_a_missing_file_is_an_error(downloads, tmp_path):
    """Something that is neither 4 numbers nor a file is reported, and not fetched."""
    result = _run("download", *DOWNLOAD_DATES, str(tmp_path / "missing.tif"))

    assert result.exit_code == 1
    assert "Files not found (and input is not a valid 4-value bbox)" in result.output
    assert downloads == []


@pytest.mark.usefixtures("standin")
@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            ["-ds", "2023.02.01", "-de", "2023.01.01", "--", "-120/-119/35/36"],
            "--date-start (20230201) must be before --date-end (20230101)",
        ),
        (
            ["-ds", "not a date at all", "--", "-120/-119/35/36"],
            "Could not parse date",
        ),
        (
            [*DOWNLOAD_DATES, "--", "-119/-120/35/36"],
            "Invalid bounding box",
        ),
    ],
)
def test_download_rejects_bad_requests(downloads, args, message):
    """Bad dates or a back-to-front bbox stop the download before it starts."""
    result = _run("download", *args)

    assert result.exit_code == 1
    assert message in result.output
    assert downloads == []


AFTER_ATL24 = ["-ds", "2025.01.01", "-de", "2025.02.01", "--", "-120/-119/35/36"]


@pytest.mark.usefixtures("standin")
def test_download_past_the_atl24_cutoff_asks_first(downloads):
    """Data after the cutoff lacks bathymetry classes; declining fetches nothing."""
    result = _run("download", *AFTER_ATL24, stdin="n\n")

    assert result.exit_code == 1
    assert "ATL24 bathymetry data is not available after 2024-11-07" in result.output
    assert "Your current request ends at 2025-02-01." in result.output
    assert downloads == []


@pytest.mark.usefixtures("standin")
def test_a_forced_download_past_the_atl24_cutoff_warns_but_goes_ahead(downloads):
    """-f skips the question, not the warning."""
    result = _run("download", "-f", *AFTER_ATL24)

    assert result.exit_code == 0, result.output
    assert "ATL24 bathymetry data is not available" in result.output
    assert len(downloads) == 1


@pytest.mark.usefixtures("standin")
@pytest.mark.parametrize(
    ("failed", "attempted", "message"),
    [
        (2, 2, "The download failed. No ICESat-2 data was retrieved."),
        (1, 2, "1 of 2 parts of the requested region failed to download."),
    ],
)
def test_failed_parts_of_a_download_are_an_error(downloads, failed, attempted, message):
    """A hole in the region asked for means the command didn't do what it was told."""
    downloads.summary = SimpleNamespace(
        parts_failed=failed,
        parts_attempted=attempted,
        granules_added=0,
    )

    result = _run("download", *DOWNLOAD_DATES, "--", "-120/-119/35/36")

    assert result.exit_code == 1
    assert message in result.output
