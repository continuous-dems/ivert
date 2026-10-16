"""The summary figure: which panels it draws, and how far its histograms reach."""

import warnings

import numpy as np
import pandas as pd
import pytest
from matplotlib.figure import Figure

from ivert import plot_validation_results as pvr


def _results(n_land, n_bathy, *, seed=0):
    """A results table like validate_dem writes, with land and bathy cells."""
    rng = np.random.default_rng(seed)
    n = n_land + n_bathy
    mean = rng.uniform(-20, 20, n)
    diff = rng.normal(0.5, 1.0, n)
    return pd.DataFrame(
        {
            "diff_mean": diff,
            "numphotons_bathy": np.r_[np.zeros(n_land), np.full(n_bathy, 5.0)],
            "dem_elev": mean + diff,
            "mean": mean,
        },
    )


@pytest.fixture
def drawn_panels(monkeypatch):
    """Record each saved figure's (title, texts) panels before the plot clears it."""
    saved = []
    real_savefig = Figure.savefig

    def savefig(fig, *args: object, **kwargs: object):
        saved.append(
            [(ax.get_title(), [t.get_text() for t in ax.texts]) for ax in fig.axes],
        )
        real_savefig(fig, *args, **kwargs)

    monkeypatch.setattr(Figure, "savefig", savefig)
    return saved


@pytest.mark.parametrize(
    ("n_land", "n_bathy", "titles"),
    [
        (50, 30, ["land", "bathy", "DEM vs. ICESat-2"]),
        (50, 0, ["land", "DEM vs. ICESat-2"]),
        (0, 30, ["bathy", "DEM vs. ICESat-2"]),
    ],
    ids=["both", "land-only", "bathy-only"],
)
def test_a_histogram_is_drawn_only_for_surfaces_with_cells(
    tmp_path,
    drawn_panels,
    n_land,
    n_bathy,
    titles,
):
    """An empty histogram panel is noise; the figure narrows to the panels it has."""
    pvr.plot_histograms_and_line(_results(n_land, n_bathy), tmp_path / "f.png", dpi=50)

    (panels,) = drawn_panels
    assert len(panels) == len(titles)
    for (panel_title, _), title in zip(panels, titles, strict=True):
        assert panel_title.endswith(title)


@pytest.mark.parametrize(
    ("uppercase", "letters"),
    [(True, ["A", "B", "C"]), (False, ["a", "b", "c"])],
)
def test_panels_are_lettered_in_order(tmp_path, drawn_panels, uppercase, letters):
    """Letters run left to right, whichever panels are present, so captions match."""
    pvr.plot_histograms_and_line(
        _results(50, 30),
        tmp_path / "f.png",
        dpi=50,
        labels_uppercase=uppercase,
    )

    (panels,) = drawn_panels
    assert [texts[-1] for _, texts in panels] == letters


def test_rmse_is_written_on_the_histograms_only_when_asked(tmp_path, drawn_panels):
    """The RMSE box is optional; the mean and std box is always there."""
    data = _results(50, 30)
    pvr.plot_histograms_and_line(data, tmp_path / "a.png", dpi=50)
    pvr.plot_histograms_and_line(
        data,
        tmp_path / "b.png",
        dpi=50,
        also_add_rmse_to_hist=True,
    )

    plain, with_rmse = drawn_panels
    for _, texts in plain[:2]:
        assert not any(t.startswith("RMSE") for t in texts)
    for _, texts in with_rmse[:2]:
        assert any(t.startswith("RMSE") for t in texts)
        assert any("$\\pm$" in t for t in texts)


def test_too_few_cells_write_no_figure(tmp_path):
    """Two cells make no meaningful histogram, so nothing is written."""
    out = tmp_path / "f.png"

    pvr.plot_histograms_and_line(_results(1, 1), out)

    assert not out.exists()


@pytest.mark.parametrize("num_stddevs", [1.5, 2.5])
def test_histogram_limits_are_num_stddevs_either_side_of_the_mean(num_stddevs):
    """The spread, not a few outliers, sets the width, so the std lines stay in view."""
    diffs = pd.Series(np.r_[np.random.default_rng(1).normal(0, 1, 990), [80.0] * 10])
    center, std = np.mean(diffs), np.std(diffs)

    left, right = pvr._histogram_xlim(center, std, num_stddevs)

    assert left == pytest.approx(center - num_stddevs * std)
    assert right == pytest.approx(center + num_stddevs * std)


@pytest.mark.parametrize(
    ("center", "limits"),
    [(2.0, (1.9, 2.1)), (-0.5, (-0.525, -0.475)), (0.0, (-0.05, 0.05))],
)
def test_identical_differences_get_a_narrow_window(center, limits):
    """With no spread, N std is no width; a narrow window keeps the one bar visible."""
    assert pvr._histogram_xlim(center, 0.0, 2.5) == pytest.approx(limits)


def test_identical_differences_draw_without_a_zero_width_axis(tmp_path):
    """This used to set equal x-limits, which matplotlib warns about."""
    data = _results(30, 0)
    data["diff_mean"] = 0.5
    data["dem_elev"] = data["mean"] + 0.5

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        pvr.plot_histograms_and_line(data, tmp_path / "f.png", dpi=50)

    assert not any("identical low and high" in str(w.message) for w in caught)


def test_histogram_limits_are_never_nan():
    """Limits that can't be computed fall back to a fixed range, never NaN."""
    assert pvr._histogram_xlim(np.nan, np.nan, 2.5) == (-1.0, 1.0)
