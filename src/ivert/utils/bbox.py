"""Bounding-box types shared across IVERT.

Most of IVERT orders a box (xmin, xmax, ymin, ymax). A few places use
(xmin, ymin, xmax, ymax) instead, and their docstrings say so.

This module imports nothing, so cli.py can use it without slowing start-up.
"""

# A box in two dimensions, with no time bounds.
BBox = tuple[float, float, float, float]
