"""Bounding-box types shared across IVERT.

Most of IVERT orders a box (xmin, xmax, ymin, ymax[, tmin, tmax]). A few places use
(xmin, ymin, xmax, ymax) instead, and their docstrings say so. Dates are YYYYMMDD
integers.

This module imports nothing heavy, so cli.py can use it without slowing start-up.
"""

from collections.abc import Sequence

# A box in two dimensions, with no time bounds.
BBox = tuple[float, float, float, float]
# A box with a date range: (xmin, xmax, ymin, ymax, tmin, tmax).
BBoxWithDates = tuple[float, float, float, float, int, int]


def with_int_dates(box: Sequence[float]) -> BBoxWithDates:
    """Return a 6-value box with its dates as integers.

    The cuboid functions (ivert.utils.cuboid_funcs) work in floats on every axis,
    dates included, so a box they return has dates like 20230101.0.
    """
    xmin, xmax, ymin, ymax, tmin, tmax = box
    return (float(xmin), float(xmax), float(ymin), float(ymax), int(tmin), int(tmax))
