"""File sizes in human-readable form, as 'ivert database size' shows."""

import pytest

from ivert.utils.sizeof_format import sizeof_fmt

KIB = 1024


@pytest.mark.parametrize(
    ("num", "expected"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (KIB, "1.0 KB"),
        (1.5 * KIB, "1.5 KB"),
        (5 * KIB**3, "5.0 GB"),
        (2.25 * KIB**4, "2.2 TB"),
    ],
)
def test_sizes_are_shown_in_the_largest_unit_under_1024(num, expected):
    """Bytes as a whole number; larger units with one decimal place."""
    assert sizeof_fmt(num) == expected


@pytest.mark.parametrize(
    ("num", "expected"),
    [
        (2047, "2.0 KB"),  # 1.999 KB rounds up into the whole number
        (1.96 * KIB, "2.0 KB"),
        (1023.97 * KIB, "1.0 MB"),  # rounds to 1024 KB, so it moves up a unit
        (1023.97, "1.0 KB"),
    ],
)
def test_rounding_carries_into_the_whole_number(num, expected):
    """A value that rounds up isn't shown with its whole number written twice."""
    assert sizeof_fmt(num) == expected


def test_the_decimal_places_and_suffix_can_be_chosen():
    """decimal_digits and suffix change the format, not the value."""
    assert sizeof_fmt(1.5 * KIB, decimal_digits=2) == "1.50 KB"
    assert sizeof_fmt(1.5 * KIB, suffix="iB") == "1.5 KiB"
    assert sizeof_fmt(500, suffix="iB") == "500 iB"
