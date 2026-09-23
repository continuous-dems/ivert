# tests/test_classify_workers.py
"""How many processes classify a request's granules, and that the pool works."""

import logging
import multiprocessing
import os
import queue
import sys
import time
from types import SimpleNamespace
from typing import ClassVar

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
# The pipeline: prefetch, then the pool
# ---------------------------------------------------------------------------
class _RecordingReader:
    """Stands in for globato's ATL03Reader: records what was asked for."""

    asked: ClassVar[list] = []
    delay: ClassVar[dict] = {}

    def __init__(self, h5_fn, cache_dir=None, **kwargs: object) -> None:
        self.cache_dir = cache_dir

    def fetch_atlxx(self, h5_fn, short_name):
        time.sleep(self.delay.get(os.path.basename(h5_fn), 0.0))
        type(self).asked.append((os.path.basename(h5_fn), short_name, self.cache_dir))
        return None if short_name == "ATL24" else f"{short_name}_for_{h5_fn}"


@pytest.fixture
def fake_globato(monkeypatch):
    import globato.streams.readers.icesat2 as g

    monkeypatch.setattr(g, "ATL03Reader", _RecordingReader)
    _RecordingReader.asked = []
    _RecordingReader.delay = {}
    return _RecordingReader


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


BBOX = (0, 1, 0, 1, 20200101, 20210101)
forks = pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or "fork" not in multiprocessing.get_all_start_methods(),
    reason="the pipeline forks its processes",
)


def _work(files):
    return [(f, [((0, 1, 0, 1), f + ".nc")]) for f in files]


@forks
def test_granules_are_classified_by_forked_workers(
    tmp_path,
    monkeypatch,
    caplog,
    fake_globato,
):
    monkeypatch.setattr(is2db.IS2Database, "_process_h5_to_nc_tiles", _fake_classify)
    monkeypatch.setattr(is2db, "classify_worker_count", lambda *_a, **_k: (3, "test"))
    files = _files(tmp_path, 5 << 20, 9 << 20, 1 << 20, 3 << 20, 7 << 20)
    files.append(str(tmp_path / "ATL03_empty_subsetted.h5"))
    open(files[-1], "wb").close()

    with caplog.at_level(logging.INFO):
        records = _db(tmp_path)._classify_files(_work(files), BBOX)

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
    # The prefetch child logs through its own handlers, not this process's caplog.
    assert sorted(a[0] for a in fake_globato.asked) == []  # nothing fetched here


@forks
def test_granules_are_classified_as_their_aux_files_arrive(
    tmp_path,
    monkeypatch,
    fake_globato,
):
    monkeypatch.setattr(is2db.IS2Database, "_process_h5_to_nc_tiles", _fake_classify)
    monkeypatch.setattr(is2db, "classify_worker_count", lambda *_a, **_k: (2, "test"))
    files = _files(tmp_path, 5 << 20, 9 << 20, 1 << 20)
    # The largest file's aux granules take longest to fetch, so it is ready last.
    fake_globato.delay = {"ATL03_1_subsetted.h5": 1.5}

    records = _db(tmp_path)._classify_files(_work(files), BBOX)

    by_name = {r["filename"]: r for r in records}
    assert by_name["ATL03_1_subsetted.h5"]["granule_num"] == 3
    assert by_name["ATL03_1_subsetted.h5"]["pid"] != os.getpid()
    first = next(r for r in records if r["granule_num"] == 1)
    assert first["pid"] == os.getpid()


@forks
def test_one_worker_means_everything_runs_here(tmp_path, monkeypatch, fake_globato):
    monkeypatch.setattr(is2db.IS2Database, "_process_h5_to_nc_tiles", _fake_classify)
    monkeypatch.setattr(is2db, "classify_worker_count", lambda *_a, **_k: (1, "test"))
    files = _files(tmp_path, 5 << 20, 9 << 20, 1 << 20)

    records = _db(tmp_path)._classify_files(_work(files), BBOX)

    assert len(records) == 3
    assert {r["pid"] for r in records} == {os.getpid()}
    assert sorted(r["granule_num"] for r in records) == [1, 2, 3]


def test_no_files_means_no_records(tmp_path):
    assert _db(tmp_path)._classify_files([], BBOX) == []


def test_prefetch_asks_globato_for_both_aux_products(tmp_path, caplog, fake_globato):
    files = _files(tmp_path, 1 << 20, 2 << 20)
    ready = queue.Queue()

    with caplog.at_level(logging.INFO):
        is2db._prefetch_aux_granules(files, str(tmp_path / "cache"), 2, ready)

    assert sorted(fake_globato.asked) == sorted(
        (os.path.basename(f), name, str(tmp_path / "cache"))
        for f in files
        for name in ("ATL08", "ATL24")
    )
    reported = [ready.get_nowait() for _ in range(3)]
    assert sorted(reported[:2]) == sorted(files)
    assert reported[2] is None
    assert "Aux granules ready for 2 subsets: 2 ATL08, 0 ATL24." in caplog.text


@forks
def test_prefetch_runs_in_a_child_that_reports_each_subset(tmp_path, fake_globato):
    files = _files(tmp_path, 1 << 20, 3 << 20, 2 << 20)

    child, ready = _db(tmp_path)._start_aux_prefetch(files)
    reported = [ready.get(timeout=30) for _ in range(4)]
    child.join(timeout=30)

    assert child.exitcode == 0
    assert sorted(reported[:3]) == sorted(files)
    assert reported[3] is None


def test_prefetch_has_nothing_to_do_without_files(tmp_path):
    assert _db(tmp_path)._start_aux_prefetch([]) is None


def test_subsets_the_prefetch_never_reported_are_still_classified(
    tmp_path,
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(is2db, "_READY_POLL_SECONDS", 0.05)
    files = ["a.h5", "b.h5", "c.h5"]
    ready = queue.Queue()
    ready.put("b.h5")
    dead_child = SimpleNamespace(is_alive=lambda: False)

    with caplog.at_level(logging.WARNING):
        order = list(is2db.IS2Database._as_ready(files, (dead_child, ready)))

    assert order == ["b.h5", "a.h5", "c.h5"]
    assert "prefetch stopped early" in caplog.text


def test_without_a_prefetch_the_given_order_is_kept():
    assert list(is2db.IS2Database._as_ready(["a", "b"], None)) == ["a", "b"]
