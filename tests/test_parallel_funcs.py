"""Tests for counting the physical CPU cores IVERT sizes its worker pools by."""

from ivert.utils import parallel_funcs


def test_the_physical_core_count_comes_from_psutil(monkeypatch):
    """Physical cores, not logical ones, as psutil counts them."""
    asked = []

    def cpu_count(*, logical):
        asked.append(logical)
        return 6

    monkeypatch.setattr(parallel_funcs.psutil, "cpu_count", cpu_count)
    assert parallel_funcs.physical_cpu_count() == 6
    assert asked == [False]


def test_the_logical_count_is_used_when_psutil_cant_tell(monkeypatch):
    """Where psutil can't count physical cores, every logical core is used."""
    monkeypatch.setattr(
        parallel_funcs.psutil,
        "cpu_count",
        lambda *, logical: None,  # noqa: ARG005
    )
    monkeypatch.setattr(parallel_funcs.mp, "cpu_count", lambda: 12)
    assert parallel_funcs.physical_cpu_count() == 12


def test_this_machine_has_at_least_one_core():
    """Unpatched, the count is a whole number of at least one."""
    count = parallel_funcs.physical_cpu_count()
    assert isinstance(count, int)
    assert count >= 1
