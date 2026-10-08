"""The per-cell statistics, computed with numpy, must match pandas' to the last bit.

The cell validation used pandas for each cell's mean, standard deviation and 10th
and 90th percentiles; it now uses numpy, which is much faster. Any difference in
the arithmetic would change validation results.
"""

import numpy as np
import pandas as pd

from ivert import validate_dem


def _cells():
    rng = np.random.default_rng(1)
    for dtype in (np.float32, np.float64):
        for n in (*range(1, 12), 50, 199, 400):
            for _ in range(20):
                heights = rng.normal(rng.uniform(-50, 3000), rng.uniform(0.01, 30), n)
                if rng.random() < 0.3:  # tied heights
                    heights = np.round(heights, 1)
                yield heights.astype(dtype)


def test_mean_and_std_match_pandas():
    """Float32 means are summed in float32 and variances in float64, as pandas does."""
    for heights in _cells():
        series = pd.Series(heights)
        mean, std = (
            validate_dem._pandas_mean(heights),
            validate_dem._pandas_std(heights),
        )
        assert mean == series.mean()
        assert np.asarray(mean).dtype == np.asarray(series.mean()).dtype
        if len(heights) > 1:
            assert std == series.std()
        else:
            assert np.isnan(std)
            assert np.isnan(series.std())


def test_percentiles_match_describe():
    """np.percentile with pandas' percentages gives describe()'s 10% and 90% exactly."""
    for heights in _cells():
        if len(heights) < validate_dem.INTERDECILE_MIN_PHOTONS:
            continue
        described = pd.Series(heights).describe(percentiles=[0.10, 0.90])
        p10, p90 = np.percentile(heights, validate_dem._PERCENTILES)
        assert np.float64(p10) == described["10%"]
        assert np.float64(p90) == described["90%"]
