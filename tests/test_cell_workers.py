"""Tests that validate_dem's cell-validation workers don't outlive the process that runs them.

Each worker started here is a real process; every test kills whatever is left on the
way out, so a failure doesn't leave workers spinning on the test machine.
"""

import contextlib
import multiprocessing as mp
import os
import signal
import time
from multiprocessing import shared_memory

import numpy as np
import pandas as pd
import psutil
import pytest

from ivert import validate_dem

ARRAY_NAMES = ("heights", "i", "j", "codes")


def _is_gone(pid):
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _wait_until_gone(pids, timeout_s=15.0):
    """Return the pids still running after up to timeout_s."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        left = [p for p in pids if not _is_gone(p)]
        if not left:
            return []
        time.sleep(0.2)
    return [p for p in pids if not _is_gone(p)]


def _kill(pids):
    for pid in pids:
        with contextlib.suppress(psutil.NoSuchProcess):
            psutil.Process(pid).kill()


def _coordinator(pid_queue):
    """Start two workers that wait for chunks, report their pids, and wait to be killed."""
    values = np.zeros(10, dtype="float32")
    names = [f"{name}_{os.getpid()}" for name in ARRAY_NAMES]
    # Kept referenced until this process is killed.
    _smos = [
        shared_memory.SharedMemory(size=values.nbytes, name=name, create=True)
        for name in names
    ]
    dtype = values.dtype
    procs = [
        validate_dem.kick_off_new_child_process(
            names[0],
            dtype,
            names[1],
            dtype,
            names[2],
            dtype,
            names[3],
            dtype,
            values.shape,
        )
        for _ in range(2)
    ]
    pid_queue.put([proc.pid for proc, _, _ in procs])
    time.sleep(120)


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL") or "fork" not in mp.get_all_start_methods(),
    reason="Needs SIGKILL and the fork start method.",
)
def test_workers_exit_when_their_coordinator_is_killed():
    """The OS kills the coordinator when memory runs out, and nobody sends the workers "STOP".

    A forked worker holds copies of the coordinator's pipe ends, so it never saw its
    pipe close, and spun at full CPU until killed by hand.
    """
    ctx = mp.get_context("fork")
    pid_queue = ctx.Queue()
    coordinator = ctx.Process(target=_coordinator, args=(pid_queue,))
    coordinator.start()
    worker_pids = []
    try:
        worker_pids = pid_queue.get(timeout=60)
        os.kill(coordinator.pid, signal.SIGKILL)
        coordinator.join()

        assert _wait_until_gone(worker_pids) == []
    finally:
        _kill([coordinator.pid, *worker_pids])
        for name in ARRAY_NAMES:
            with contextlib.suppress(FileNotFoundError):
                shared_memory.SharedMemory(name=f"{name}_{coordinator.pid}").unlink()


class _WorkerThatWontStop(mp.Process):
    """A worker whose state can't be read, as if stopping it failed."""

    def is_alive(self):
        msg = "can't read this worker's state"
        raise RuntimeError(msg)


def test_cleanup_stops_every_worker_even_if_one_fails():
    """One worker that can't be stopped must not leave the others running."""
    broken = _WorkerThatWontStop(target=time.sleep, args=(60,))
    sleeper = mp.Process(target=time.sleep, args=(60,))
    sleeper.start()
    try:
        validate_dem.clean_procs_and_pipes([broken, sleeper], [], [], [])

        assert not sleeper.is_alive()
    finally:
        _kill([sleeper.pid])


class _InterruptedProgress:
    """A progress bar that raises KeyboardInterrupt at the first finished chunk."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.n = 0

    def update(self, _count):
        raise KeyboardInterrupt

    def close(self):
        pass


def test_workers_are_stopped_when_the_run_is_interrupted(monkeypatch):
    """Ctrl-C isn't an Exception, so stopping the workers only on Exception missed it."""
    started = []
    real_kick_off = validate_dem.kick_off_new_child_process

    def recording_kick_off(*args: object, **kwargs: object):
        proc, pipe_parent, pipe_child = real_kick_off(*args, **kwargs)
        started.append(proc)
        return proc, pipe_parent, pipe_child

    monkeypatch.setattr(validate_dem, "kick_off_new_child_process", recording_kick_off)
    monkeypatch.setattr(validate_dem.tqdm, "tqdm", _InterruptedProgress)

    n = 40
    photon_df = pd.DataFrame(
        {
            "i": np.arange(n, dtype=np.int64),
            "j": np.zeros(n, dtype=np.int64),
            "class_code": np.ones(n, dtype=np.int8),
            "h": np.ones(n, dtype=np.float32),
        },
    )

    try:
        with pytest.raises(KeyboardInterrupt):
            validate_dem._run_parallel_cell_validation(
                photon_df,
                photon_df["h"],
                dem_overlap_i=photon_df["i"].to_numpy(),
                dem_overlap_j=photon_df["j"].to_numpy(),
                dem_overlap_elevs=np.zeros(n, dtype=np.float32),
                n=n,
                max_photons_per_cell=None,
                min_photons_per_cell=1,
                measure_coverage=False,
                coverage_coords=None,
                numprocs=2,
                empty_val=-99999.0,
            )

        assert started
        assert not any(proc.is_alive() for proc in started)
    finally:
        _kill([proc.pid for proc in started if proc.pid is not None])
        leftover = []
        for name in ARRAY_NAMES:
            try:
                shared_memory.SharedMemory(name=f"{name}_{os.getpid()}").unlink()
                leftover.append(name)
            except FileNotFoundError:
                pass

    # The shared memory segments are released on the way out.
    assert leftover == []
