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


# ---------------------------------------------------------------------------
# merge_cuboids and its helpers
# ---------------------------------------------------------------------------
def _cell(x, y, z=0):
    """A unit cube with its low corner at (x, y, z), in point order."""
    return (x, y, z, x + 1, y + 1, z + 1)


@pytest.mark.parametrize(
    ("b", "merged"),
    [
        (_cell(1, 0), (0, 0, 0, 2, 1, 1)),  # east
        (_cell(0, 1), (0, 0, 0, 1, 2, 1)),  # north
        (_cell(0, 0, 1), (0, 0, 0, 1, 1, 2)),  # above
    ],
    ids=["x", "y", "z"],
)
def test_cubes_sharing_a_face_merge_along_that_axis(b, merged):
    """Two boxes that line up exactly in two axes and touch in the third are one."""
    assert cf._merge_pair(_cell(0, 0), b, 1e-10) == merged
    assert cf._merge_pair(b, _cell(0, 0), 1e-10) == merged


def test_along_only_merges_on_that_axis():
    """Asking for rows must not join a cell to the one north of it."""
    assert cf._merge_pair(_cell(0, 0), _cell(0, 1), 1e-10, along="x") is None
    assert cf._merge_pair(_cell(0, 0), _cell(1, 0), 1e-10, along="x") is not None


def test_a_box_inside_another_is_absorbed_only_in_a_full_merge():
    """The larger box wins, but a one-axis strip pass leaves both alone."""
    big = (0, 0, 0, 4, 4, 4)
    small = (1, 1, 1, 2, 2, 2)

    assert cf._merge_pair(small, big, 1e-10) == big
    assert cf._merge_pair(small, big, 1e-10, along="x") is None


def test_boxes_that_only_share_an_edge_do_not_merge():
    """Diagonal neighbours aren't a box together."""
    assert cf._merge_pair(_cell(0, 0), _cell(1, 1), 1e-10) is None


def test_a_gap_smaller_than_tol_counts_as_touching():
    """Floating-point noise between neighbours must not stop a merge."""
    b = (1 + 1e-12, 0, 0, 2, 1, 1)

    assert cf._merge_pair(_cell(0, 0), b, 1e-10) == (0, 0, 0, 2, 1, 1)


def test_greedy_merge_repeats_until_nothing_more_merges():
    """A 3 x 2 block of cells, in any order, ends up as one box."""
    cells = [_cell(x, y) for y in (1, 0) for x in (2, 0, 1)]

    assert cf._greedy_merge(cells, 1e-10) == [(0, 0, 0, 3, 2, 1)]


@pytest.mark.parametrize(
    ("prefer", "expected"),
    [
        ("row", [(0, 0, 0, 2, 1, 1), (0, 1, 0, 1, 2, 1)]),
        ("column", [(0, 0, 0, 1, 2, 1), (1, 0, 0, 2, 1, 1)]),
    ],
)
def test_prefer_breaks_a_tie_between_rows_and_columns(prefer, expected):
    """An L of three cells is two boxes either way; prefer picks the orientation."""
    cells = [_cell(0, 0), _cell(1, 0), _cell(0, 1)]

    assert sorted(cf.merge_cuboids(cells, prefer=prefer)) == expected


def test_merge_drops_flat_and_repeated_boxes():
    """A box with no volume isn't coverage, and one box listed twice is one box."""
    flat = (5, 5, 5, 6, 6, 5)

    assert cf.merge_cuboids([_cell(0, 0), _cell(0, 0), flat]) == [_cell(0, 0)]


def test_merge_keeps_axis_order():
    """(x1, x2, y1, y2, z1, z2) in, the same layout out."""
    cells = [(0, 1, 0, 1, 0, 1), (1, 2, 0, 1, 0, 1)]

    assert cf.merge_cuboids(cells, bbox_order="axis") == [(0, 2, 0, 1, 0, 1)]


# ---------------------------------------------------------------------------
# cuboids_intersect_vectorized agrees with cuboids_intersect
# ---------------------------------------------------------------------------
_QUERY = (0.0, 0.0, 0.0, 4.0, 4.0, 4.0)  # point order
_CANDIDATES = [
    (1.0, 1.0, 1.0, 2.0, 2.0, 2.0),  # inside
    (3.0, 3.0, 3.0, 6.0, 6.0, 6.0),  # overlapping a corner
    (-1.0, -1.0, -1.0, 5.0, 5.0, 5.0),  # around it
    (4.0, 0.0, 0.0, 6.0, 4.0, 4.0),  # touching a face
    (5.0, 5.0, 5.0, 6.0, 6.0, 6.0),  # apart
    (4.0 - 1e-11, 0.0, 0.0, 6.0, 4.0, 4.0),  # overlap thinner than tol
    (0.0, 0.0, 4.0, 4.0, 4.0, 5.0),  # sharing the top only
    (1.0, -2.0, 1.0, 2.0, -1.0, 2.0),  # apart in y alone
]


@pytest.mark.parametrize("bbox_order", ["point", "axis"])
def test_the_vectorized_form_agrees_with_one_at_a_time(bbox_order):
    """Testing many cuboids at once gives what testing each one would."""
    query = (
        _QUERY
        if bbox_order == "point"
        else (_QUERY[0], _QUERY[3], _QUERY[1], _QUERY[4], _QUERY[2], _QUERY[5])
    )
    columns = list(zip(*_CANDIDATES, strict=True))
    mask = cf.cuboids_intersect_vectorized(
        xmin=columns[0],
        ymin=columns[1],
        zmin=columns[2],
        xmax=columns[3],
        ymax=columns[4],
        zmax=columns[5],
        query=query,
        bbox_order=bbox_order,
    )
    one_at_a_time = [cf.cuboids_intersect(c, _QUERY) for c in _CANDIDATES]
    assert mask.tolist() == one_at_a_time
    assert one_at_a_time == [True, True, True, False, False, False, False, False]
