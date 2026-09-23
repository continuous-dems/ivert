# tests/test_classify_workers.py
"""How many processes classify a request's granules, and that the pool works."""

import logging
import multiprocessing
import os
import sys
from types import SimpleNamespace

import pytest

from ivert import icesat2_database_v2 as is2db

GiB = 1 << 30


def _files(tmp_path, *sizes: int):
    """Sparse files of the given sizes, standing in for ATL03 subsets."""
    paths = []
    for i, size in enumerate(sizes):
        path = tmp_path / f"ATL03_{i}_subsetted.h5"
        with open(path, "wb") as f:
            f.truncate(size)
        paths.append(str(path))
    return paths


def _auto(tmp_path, sizes=(100 << 20, 448 << 20), **machine: object):
    machine = {
        "cpu_count": 40,
        "available_bytes": 200 * GiB,
        "fork_available": True,
        **machine,
    }
    return is2db.classify_worker_count("auto", _files(tmp_path, *sizes), **machine)


def test_an_integer_setting_is_taken_as_given(tmp_path):
    assert is2db.classify_worker_count(3, [], cpu_count=1)[0] == 3
    assert is2db.classify_worker_count("3", [], cpu_count=1)[0] == 3
    assert is2db.classify_worker_count(0, [], cpu_count=1)[0] == 1


def test_a_setting_that_is_not_a_number_falls_back_to_auto(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        count, _ = is2db.classify_worker_count(
            "lots",
            _files(tmp_path, 1 << 20),
            cpu_count=4,
            available_bytes=64 * GiB,
            fork_available=True,
        )
    assert count == 3
    assert "icesat2_classify_workers" in caplog.text


def test_auto_leaves_one_core_free(tmp_path):
    assert _auto(tmp_path, cpu_count=4)[0] == 3
    assert _auto(tmp_path, cpu_count=1)[0] == 1


def test_auto_is_capped_on_a_big_machine(tmp_path):
    assert _auto(tmp_path)[0] == is2db._CLASSIFY_MAX_WORKERS


def test_auto_fits_the_pool_to_the_memory_available(tmp_path):
    # A 448 MiB subset is budgeted at 6x its size, so ~2.6 GB per worker, and the
    # pool may plan on 60% of what is available: 5 GB free is one worker, 20 GB
    # is four.
    count, reason = _auto(tmp_path, available_bytes=5 * GiB)
    assert count == 1
    assert "GB per worker" in reason
    assert _auto(tmp_path, available_bytes=20 * GiB)[0] == 4


def test_small_files_still_budget_a_gigabyte_each(tmp_path):
    assert _auto(tmp_path, sizes=(1 << 20,), available_bytes=5 * GiB)[0] == 3
    assert _auto(tmp_path, sizes=(), available_bytes=5 * GiB)[0] == 3


def test_auto_is_serial_where_fork_is_unavailable(tmp_path):
    count, reason = _auto(tmp_path, fork_available=False)
    assert count == 1
    assert "fork" in reason


# ---------------------------------------------------------------------------
# The pool itself
# ---------------------------------------------------------------------------
def _fake_classify(self, h5_fn, query_bbox, tiles, **kwargs: object):
    """Stands in for _process_h5_to_nc_tiles: says which process did the work."""
    if "empty" in h5_fn:
        return []
    return [
        {
            "filename": os.path.basename(h5_fn),
            "pid": os.getpid(),
            "granule_num": kwargs["granule_num"],
            "total_granules": kwargs["total_granules"],
        },
    ]


def _db(tmp_path):
    config = SimpleNamespace(
        ivert_database_index=str(tmp_path / "db" / "_ivert_database_index.nc"),
        ivert_database_directory=str(tmp_path / "db"),
        icesat2_download_directory=str(tmp_path / "cache"),
        icesat2_vertical_datum="ellipsoid",
        icesat2_classify_workers="auto",
    )
    return is2db.IS2Database(ivert_config=config)


@pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or "fork" not in multiprocessing.get_all_start_methods(),
    reason="the pool forks its workers",
)
def test_granules_are_classified_by_forked_workers(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(is2db.IS2Database, "_process_h5_to_nc_tiles", _fake_classify)
    monkeypatch.setattr(is2db, "classify_worker_count", lambda *_a, **_k: (3, "test"))
    files = _files(tmp_path, 5 << 20, 9 << 20, 1 << 20, 3 << 20, 7 << 20)
    files.append(str(tmp_path / "ATL03_empty_subsetted.h5"))
    open(files[-1], "wb").close()
    work = [(f, [((0, 1, 0, 1), f + ".nc")]) for f in files]

    with caplog.at_level(logging.INFO):
        records = _db(tmp_path)._classify_files(work, (0, 1, 0, 1, 20200101, 20210101))

    # Every file with photons gave one record, numbered largest file first.
    by_name = {r["filename"]: r for r in records}
    assert sorted(by_name) == sorted(os.path.basename(f) for f in files[:-1])
    assert by_name["ATL03_1_subsetted.h5"]["granule_num"] == 1  # the 9 MiB file
    assert {r["total_granules"] for r in records} == {6}
    # The largest was done here, the rest by other processes.
    assert by_name["ATL03_1_subsetted.h5"]["pid"] == os.getpid()
    other_pids = {r["pid"] for r in records if r["filename"] != "ATL03_1_subsetted.h5"}
    assert other_pids
    assert os.getpid() not in other_pids
    assert "No valid classified photons in ATL03_empty_subsetted.h5" in caplog.text
    assert "3 worker processes" in caplog.text


def test_one_worker_means_everything_runs_here(tmp_path, monkeypatch):
    monkeypatch.setattr(is2db.IS2Database, "_process_h5_to_nc_tiles", _fake_classify)
    monkeypatch.setattr(is2db, "classify_worker_count", lambda *_a, **_k: (1, "test"))
    files = _files(tmp_path, 5 << 20, 9 << 20, 1 << 20)
    work = [(f, [((0, 1, 0, 1), f + ".nc")]) for f in files]

    records = _db(tmp_path)._classify_files(work, (0, 1, 0, 1, 20200101, 20210101))

    assert len(records) == 3
    assert {r["pid"] for r in records} == {os.getpid()}
    assert [r["granule_num"] for r in records] == [1, 2, 3]


def test_no_files_means_no_records(tmp_path):
    assert _db(tmp_path)._classify_files([], (0, 1, 0, 1, 20200101, 20210101)) == []
