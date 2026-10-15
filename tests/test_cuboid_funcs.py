"""Cuboid arithmetic: what's left of one cuboid after another is taken out of it."""

import itertools

import pytest

from ivert.utils import cuboid_funcs as cf

A = (0.0, 0.0, 0.0, 4.0, 4.0, 4.0)  # point order: (x1, y1, z1, x2, y2, z2)


def _volume(c):
    x1, y1, z1, x2, y2, z2 = c
    return (x2 - x1) * (y2 - y1) * (z2 - z1)


def _overlap(c, d):
    """Volume shared by two point-order cuboids."""
    dims = [max(0.0, min(c[i + 3], d[i + 3]) - max(c[i], d[i])) for i in range(3)]
    return dims[0] * dims[1] * dims[2]


def _check_difference(a, b, pieces):
    """The pieces fill a minus b exactly: inside a, outside b, not overlapping."""
    assert sum(_volume(p) for p in pieces) == pytest.approx(
        _volume(a) - _overlap(a, b),
    )
    for p in pieces:
        assert _overlap(p, a) == pytest.approx(_volume(p))
        assert _overlap(p, b) == pytest.approx(0.0)
    for p, q in itertools.combinations(pieces, 2):
        assert _overlap(p, q) == pytest.approx(0.0)


def test_a_cuboid_minus_one_it_does_not_meet_is_itself():
    """No overlap: the first cuboid comes back whole."""
    assert cf.subtract_cuboids(A, (5, 5, 5, 6, 6, 6)) == [A]


def test_a_cuboid_minus_one_it_only_touches_is_itself():
    """Sharing a face takes no volume away."""
    assert cf.subtract_cuboids(A, (4, 0, 0, 6, 4, 4)) == [A]


def test_a_cuboid_minus_one_covering_it_is_nothing():
    """Nothing is left."""
    assert cf.subtract_cuboids(A, (-1, -1, -1, 5, 5, 5)) == []


def test_a_hole_in_the_middle_leaves_six_pieces():
    """One piece on each side of the hole, together filling the rest exactly."""
    b = (1, 1, 1, 3, 3, 3)

    pieces = cf.subtract_cuboids(A, b)

    assert len(pieces) == 6
    _check_difference(A, b, pieces)


@pytest.mark.parametrize(
    "b",
    [
        (3, 3, 3, 6, 6, 6),  # a corner
        (-1, 1, -1, 5, 3, 5),  # a slot right through
        (0, 0, 0, 4, 4, 2),  # the bottom half
        (2, -1, -1, 6, 5, 5),  # the east half
    ],
)
def test_what_is_left_fills_the_difference_exactly(b):
    """However they overlap, the pieces cover a minus b, once."""
    _check_difference(A, b, cf.subtract_cuboids(A, b))


def test_slivers_thinner_than_the_tolerance_are_dropped():
    """A gap of less than tol is the same point, so no piece is left there."""
    assert cf.subtract_cuboids(A, (1e-12, 0, 0, 4, 4, 4), tol=1e-10) == []


@pytest.mark.parametrize("order", ["axis", "xxyyzz", " AXIS "])
def test_axis_order_goes_in_and_comes_out(order):
    """(x1, x2, y1, y2, z1, z2) in, the same layout out."""
    a = (0, 4, 0, 4, 0, 4)

    assert cf.subtract_cuboids(a, (2, 6, -1, 5, -1, 5), bbox_order=order) == [
        (0, 2, 0, 4, 0, 4),
    ]
    assert cf.subtract_cuboids(a, (5, 6, 5, 6, 5, 6), bbox_order=order) == [
        (0, 4, 0, 4, 0, 4),
    ]


def test_point_order_has_another_name():
    """'xyzxyz' is the same as 'point'."""
    b = (2, -1, -1, 6, 5, 5)

    assert cf.subtract_cuboids(A, b, bbox_order="xyzxyz") == cf.subtract_cuboids(A, b)


@pytest.mark.parametrize(
    ("a", "b", "message"),
    [
        ((4, 0, 0, 0, 4, 4), A, "Invalid bounding box"),
        (A, (0, 4, 0, 4, 0, 4), "Invalid bounding box"),  # axis order given as point
    ],
)
def test_back_to_front_cuboids_are_an_error(a, b, message):
    """A first point past the second usually means the wrong bbox_order."""
    with pytest.raises(ValueError, match=message):
        cf.subtract_cuboids(a, b)


# ---------------------------------------------------------------------------
# bbox_order, across the module
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "call",
    [
        lambda order: cf.subtract_cuboids(A, A, bbox_order=order),
        lambda order: cf.merge_cuboids([A], bbox_order=order),
        lambda order: cf.cuboids_intersect(A, A, bbox_order=order),
        lambda order: cf.cuboids_intersect_vectorized(
            xmin=[0.0],
            xmax=[4.0],
            ymin=[0.0],
            ymax=[4.0],
            zmin=[0.0],
            zmax=[4.0],
            query=A,
            bbox_order=order,
        ),
    ],
    ids=["subtract", "merge", "intersect", "intersect_vectorized"],
)
def test_an_unknown_bbox_order_is_an_error(call):
    """Only 'point'/'xyzxyz' and 'axis'/'xxyyzz' are layouts."""
    with pytest.raises(ValueError, match="Invalid bbox_order"):
        call("xyz")


@pytest.mark.parametrize(
    ("order", "a", "b"),
    [
        ("point", (0, 0, 0, 2, 2, 2), (1, 1, 1, 3, 3, 3)),
        ("xyzxyz", (0, 0, 0, 2, 2, 2), (1, 1, 1, 3, 3, 3)),
        ("axis", (0, 2, 0, 2, 0, 2), (1, 3, 1, 3, 1, 3)),
        ("xxyyzz", (0, 2, 0, 2, 0, 2), (1, 3, 1, 3, 1, 3)),
    ],
)
def test_every_bbox_order_name_is_read_by_cuboids_intersect(order, a, b):
    """Each name gives the layout it says."""
    assert cf.cuboids_intersect(a, b, bbox_order=order)
