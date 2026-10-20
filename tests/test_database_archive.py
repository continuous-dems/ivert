"""'ivert database dump' and 'restore': a database to a zip archive, and back.

Each database is a StandInDatabase (see conftest.py), built from photons given
here, so nothing is downloaded. Every granule's query box spans January 2023, and
every landmask is a single box of land.
"""

import logging
import zipfile

import pytest

from ivert import database_archive as da
from ivert import landmask

DATES = (20230101, 20230201)
# The source database: two granules side by side, under one landmask.
WEST = (0.0, 2.0, 0.0, 2.0, *DATES)
EAST = (2.0, 4.0, 0.0, 2.0, *DATES)
SOURCE_LANDMASK = (0.0, 4.0, 0.0, 2.0)
# The region dumped from it, across the two granules' common edge.
REGION = (1.0, 3.0, 0.0, 2.0)


def _index(db):
    """(query xmin, query xmax, photon count) of every granule in the index."""
    gdf = db.open_gdf()
    return sorted(
        zip(
            gdf["query_bbox_xmin"].tolist(),
            gdf["query_bbox_xmax"].tolist(),
            gdf["numphotons"].tolist(),
            strict=True,
        ),
    )


def _landmask_boxes(db):
    return sorted(landmask.stored_landmasks(db.landmask_dir).values())


@pytest.fixture
def source(make_standin_db):
    """Two granules, at x 0-2 and 2-4, with photons either side of x = 1 and 3.

    The region 1-3 holds two of the west granule's photons and one of the east's.
    """
    standin = make_standin_db("source")
    standin.add_granule(WEST, [(0.5, 0.5), (1.25, 1.25), (1.75, 1.75)])
    standin.add_granule(EAST, [(2.5, 0.5), (3.5, 1.5)])
    standin.add_landmask(SOURCE_LANDMASK)
    standin.reindex()
    return standin.db


@pytest.fixture
def region_zip(source, tmp_path):
    """The source cut to x 1-3."""
    return da.dump(tmp_path / "region.zip", region_rects=[REGION], db=source).path


@pytest.fixture
def make_target(make_standin_db):
    """Return a function making a database of one granule and one landmask."""

    def make(query_bbox, points, landmask_bbox):
        standin = make_standin_db("target")
        standin.add_granule(query_bbox, points)
        standin.add_landmask(landmask_bbox)
        standin.reindex()
        return standin.db

    return make


# ---------------------------------------------------------------------------
# dump
# ---------------------------------------------------------------------------
def test_a_full_dump_holds_every_granule_and_landmask_whole(source, tmp_path):
    """With no region, every file is archived as it is, beside a manifest."""
    result = da.dump(tmp_path / "all.zip", db=source)

    stored = sorted(source.open_gdf()["filename"])
    assert [g["filename"] for g in result.manifest["granules"]] == stored
    assert result.manifest["landmasks"] == [
        {"filename": "landmask_0.0_4.0_0.0_2.0.geojson", "bbox": list(SOURCE_LANDMASK)},
    ]
    with zipfile.ZipFile(result.path) as zf:
        assert sorted(zf.namelist()) == sorted(
            [
                da.MANIFEST_NAME,
                da.SUMMARY_NAME,
                *(f"{da.GRANULES_ARCDIR}/{name}" for name in stored),
                f"{da.LANDMASKS_ARCDIR}/landmask_0.0_4.0_0.0_2.0.geojson",
            ],
        )
    # Nothing is left behind: no partial zip, no staging directory.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["all.zip", "source"]


def test_a_region_dump_cuts_granules_and_landmasks_to_the_region(region_zip):
    """Files reaching outside the region are clipped, keeping only what's inside."""
    manifest = da.read_manifest(region_zip)

    assert [(g["query_bbox"], g["numphotons"]) for g in manifest["granules"]] == [
        ([1.0, 2.0, 0.0, 2.0, *DATES], 2),
        ([2.0, 3.0, 0.0, 2.0, *DATES], 1),
    ]
    assert manifest["landmasks"] == [
        {"filename": "landmask_1.0_3.0_0.0_2.0.geojson", "bbox": list(REGION)},
    ]
    assert manifest["region"] == {"rectangles": [list(REGION)], "date_range": None}


def test_dumping_an_empty_database_is_an_error(make_standin_db, tmp_path):
    """There is nothing to archive."""
    db = make_standin_db("empty").db

    with pytest.raises(da.ArchiveError, match="is empty"):
        da.dump(tmp_path / "empty.zip", db=db)


def test_a_dump_with_nothing_in_its_dates_is_an_error(source, tmp_path):
    """No photons in the dates asked for: nothing is written."""
    with pytest.raises(da.ArchiveError, match="No photons"):
        da.dump(tmp_path / "out.zip", date_range=(20240101, 20240201), db=source)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["source"]


def test_a_missing_granule_file_is_skipped_with_a_warning(source, tmp_path, caplog):
    """The index can name a file that has since gone; the rest are still dumped."""
    missing = source.granules_dir / min(source.open_gdf()["filename"])
    missing.unlink()

    with caplog.at_level(logging.WARNING):
        result = da.dump(tmp_path / "all.zip", db=source)

    assert f"Skipping missing granule file {missing.name}" in caplog.text
    assert len(result.manifest["granules"]) == 1


# ---------------------------------------------------------------------------
# plan_restore
# ---------------------------------------------------------------------------
def test_the_plan_lists_where_the_archive_overlaps_the_database(
    region_zip,
    make_target,
):
    """Only the imported query box that meets existing data is an overlap."""
    target = make_target(WEST, [(0.25, 0.25)], (0.0, 2.0, 0.0, 2.0))

    plan = da.plan_restore(region_zip, db=target)

    assert plan.overlaps == [(1.0, 2.0, 0.0, 2.0, *DATES)]


def test_restoring_into_an_empty_database_overlaps_nothing(
    region_zip,
    make_standin_db,
):
    """Every mode imports everything; there is nothing to keep or replace."""
    target = make_standin_db("empty").db

    plan = da.plan_restore(region_zip, db=target)
    result = da.restore(plan, "replace", db=target)

    assert plan.overlaps == []
    assert result.granules_added == 2
    assert result.landmasks_added == 1
    assert _index(target) == [(1.0, 2.0, 2), (2.0, 3.0, 1)]


def test_an_archive_in_another_vertical_datum_is_refused(region_zip, make_standin_db):
    """Mixing photon heights in two vertical datums would give wrong validations."""
    target = make_standin_db("target")
    target.add_granule(WEST, [(0.25, 0.25)], vertical_datum="EPSG:5703")
    target.reindex()

    with pytest.raises(da.ArchiveError, match="Mixing them would give wrong"):
        da.plan_restore(region_zip, db=target.db)


@pytest.mark.parametrize("manifest", ['{"format": "other"}', "[]", '"text"'])
def test_a_zip_that_is_not_a_dump_is_refused(manifest, tmp_path):
    """A manifest that isn't an IVERT dump's, or isn't a JSON object at all."""
    archive = tmp_path / "other.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(da.MANIFEST_NAME, manifest)

    with pytest.raises(da.ArchiveError, match="is not an IVERT database dump"):
        da.read_manifest(archive)


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------
def test_an_unknown_overlap_mode_is_an_error(region_zip, source):
    """Only the modes in OVERLAP_MODES are accepted."""
    plan = da.plan_restore(region_zip, db=source)

    with pytest.raises(ValueError, match="Unknown overlap mode"):
        da.restore(plan, "merge", db=source)


def test_cancel_changes_nothing(region_zip, make_target):
    """'cancel' imports nothing and doesn't rebuild the index."""
    target = make_target(WEST, [(0.25, 0.25)], (0.0, 2.0, 0.0, 2.0))
    before = sorted(target.granules_dir.iterdir())

    result = da.restore(da.plan_restore(region_zip, db=target), "cancel", db=target)

    assert result == da.RestoreResult()
    assert sorted(target.granules_dir.iterdir()) == before
    assert _index(target) == [(0.0, 2.0, 1)]


def test_all_imports_everything_beside_the_existing_data(region_zip, make_target):
    """'all' keeps both copies where they overlap."""
    target = make_target(WEST, [(0.25, 0.25), (1.5, 1.5)], (0.0, 2.0, 0.0, 2.0))

    result = da.restore(da.plan_restore(region_zip, db=target), "all", db=target)

    assert (result.granules_added, result.existing_removed) == (2, 0)
    assert result.n_index_records == 3
    assert _index(target) == [(0.0, 2.0, 2), (1.0, 2.0, 2), (2.0, 3.0, 1)]
    assert _landmask_boxes(target) == [(0.0, 2.0, 0.0, 2.0), REGION]


def test_keep_skips_what_the_database_already_covers(region_zip, make_target):
    """An imported file wholly inside existing data is skipped; one outside goes in."""
    target = make_target(WEST, [(0.25, 0.25), (1.5, 1.5)], (0.0, 2.0, 0.0, 2.0))

    result = da.restore(da.plan_restore(region_zip, db=target), "keep", db=target)

    assert (result.granules_added, result.granules_clipped) == (1, 0)
    assert result.granules_skipped == 1
    assert _index(target) == [(0.0, 2.0, 2), (2.0, 3.0, 1)]
    # The imported landmask is cut to the part the database didn't have.
    assert result.landmasks_added == 1
    assert _landmask_boxes(target) == [(0.0, 2.0, 0.0, 2.0), (2.0, 3.0, 0.0, 2.0)]


def test_keep_cuts_a_file_that_is_partly_covered(region_zip, make_target):
    """Only the part of the imported file outside the existing data goes in.

    The imported 1-2 file has photons at 1.25, inside the existing 0-1.5, and at
    1.75, outside it. A file with all its photons outside would go in whole.
    """
    target = make_target(
        (0.0, 1.5, 0.0, 2.0, *DATES),
        [(0.25, 0.25), (1.25, 1.25)],
        (0.0, 1.5, 0.0, 2.0),
    )

    result = da.restore(da.plan_restore(region_zip, db=target), "keep", db=target)

    assert (result.granules_added, result.granules_clipped) == (1, 1)
    assert _index(target) == [(0.0, 1.5, 2), (1.5, 2.0, 1), (2.0, 3.0, 1)]
    assert _landmask_boxes(target) == [(0.0, 1.5, 0.0, 2.0), (1.5, 3.0, 0.0, 2.0)]


def test_keep_skips_a_file_the_database_already_has(source, tmp_path):
    """Restoring a database's own dump into it adds nothing."""
    plan = da.plan_restore(da.dump(tmp_path / "all.zip", db=source).path, db=source)

    result = da.restore(plan, "keep", db=source)

    assert (result.granules_added, result.granules_skipped) == (0, 2)
    assert result.landmasks_added == 0
    assert _index(source) == [(0.0, 2.0, 3), (2.0, 4.0, 2)]


def test_replace_cuts_the_imported_area_out_of_the_existing_data(
    region_zip,
    make_target,
):
    """The existing file keeps only what lies outside the archive's region."""
    target = make_target(WEST, [(0.25, 0.25), (1.5, 1.5)], (0.0, 2.0, 0.0, 2.0))

    result = da.restore(da.plan_restore(region_zip, db=target), "replace", db=target)

    assert (result.granules_added, result.existing_clipped) == (2, 1)
    assert result.existing_removed == 1
    assert _index(target) == [(0.0, 1.0, 1), (1.0, 2.0, 2), (2.0, 3.0, 1)]
    assert _landmask_boxes(target) == [(0.0, 1.0, 0.0, 2.0), REGION]


def test_replace_overwrites_a_file_of_the_same_name(source, tmp_path):
    """A file the archive also holds is replaced outright, not cut."""
    plan = da.plan_restore(da.dump(tmp_path / "all.zip", db=source).path, db=source)

    result = da.restore(plan, "replace", db=source)

    assert (result.granules_added, result.existing_clipped) == (2, 0)
    assert result.existing_removed == 0
    assert _index(source) == [(0.0, 2.0, 3), (2.0, 4.0, 2)]
    assert _landmask_boxes(source) == [SOURCE_LANDMASK]
