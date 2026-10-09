"""Subtract, merge and intersect axis-aligned cuboids (3D boxes)."""

import numpy as np


def subtract_cuboids(a, b, tol=1e-10, bbox_order="point"):
    """Subtract cuboid `b` from cuboid `a`.

    Returns the list of non-overlapping cuboids that exactly fill the remaining
    volume (a - b).

    Args:
        a: The cuboid to subtract from, as (xmin, ymin, zmin, xmax, ymax, zmax).
        b: The cuboid to subtract, in the same layout as `a`.
        tol: Numerical tolerance for floating-point comparisons. Differences in
            coordinates less than "tol" units shall be considered as the same
            point. Default: 1e-10
        bbox_order: "axis" or "point", or conversely "xxyyzz" or "xyzxyz"
            (respectively). Default is "point".
            Point order assumes bounding boxes are in (x1, y1, z1, x2, y2, z2) format.
            Axis order assumes bounding boxes are in (x1, x2, y1, y2, z1, z2) format.
            Applies to both inputs and outputs.

    Raises:
        ValueError: If the bounding-boxes are misordered (where the first point is
            greater than the second point).

    Returns:
        list[tuple]: List of cuboids (xmin, ymin, zmin, xmax, ymax, zmax)
            covering the entire volume of the difference without overlap.

    """
    bbox_order = bbox_order.lower().strip()
    if bbox_order in ("point", "xyzxyz"):
        ax1, ay1, az1, ax2, ay2, az2 = tuple(a)
        bx1, by1, bz1, bx2, by2, bz2 = tuple(b)
    elif bbox_order in ("axis", "xxyyzz"):
        ax1, ax2, ay1, ay2, az1, az2 = tuple(a)
        bx1, bx2, by1, by2, bz1, bz2 = tuple(b)
    else:
        msg = f"Invalid bbox_order parameter: {bbox_order}. Only 'axis' or 'point' are allowed."
        raise ValueError(msg)

    # Make sure in each case that the points are in the correct order (that x2 is not less than x1, etc)
    if (ax1 > ax2) or (ay1 > ay2) or (az1 > az2):
        msg = (
            f"Invalid bounding box: {a}. "
            "The first point must be less than or equal to the second point in each dimension. "
            "Double-check your 'bbox_order' parameter to make sure you choose the correct 'point' or 'axis' order."
        )
        raise ValueError(msg)
    if (bx1 > bx2) or (by1 > by2) or (bz1 > bz2):
        msg = (
            f"Invalid bounding box: {b}. "
            "The first point must be less than or equal to the second point in each dimension. "
            "Double-check your 'bbox_order' parameter to make sure you choose the correct 'point' or 'axis' order."
        )
        raise ValueError(msg)

    # --- Find intersection ---
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    iz1 = max(az1, bz1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iz2 = min(az2, bz2)

    # If no overlap, return A itself
    if ix1 >= (ix2 + tol) or iy1 >= (iy2 + tol) or iz1 >= (iz2 + tol):
        return [a]

    result = []

    # --- Split around intersection (up to 6 parts) ---
    # Left
    if ax1 < ix1 - tol:
        result.append((ax1, ay1, az1, ix1, ay2, az2))
    # Right
    if ix2 < ax2 - tol:
        result.append((ix2, ay1, az1, ax2, ay2, az2))
    # Front
    if ay1 < iy1 - tol:
        result.append((ix1, ay1, az1, ix2, iy1, az2))
    # Back
    if iy2 < ay2 - tol:
        result.append((ix1, iy2, az1, ix2, ay2, az2))
    # Bottom
    if az1 < iz1 - tol:
        result.append((ix1, iy1, az1, ix2, iy2, iz1))
    # Top
    if iz2 < az2 - tol:
        result.append((ix1, iy1, iz2, ix2, iy2, az2))

    # Filter degenerate (zero-volume) pieces
    clean = []
    for x1, y1, z1, x2, y2, z2 in result:
        if (x2 - x1 > tol) and (y2 - y1 > tol) and (z2 - z1 > tol):
            clean.append((x1, y1, z1, x2, y2, z2))

    # If the bboxes were given in axis-order, re-order them from point order (above) before returning.
    if bbox_order in ("axis", "xxyyzz"):
        clean = [(x1, x2, y1, y2, z1, z2) for (x1, y1, z1, x2, y2, z2) in clean]

    return clean


def _normalize_merge_preference(prefer):
    """Return None, "row" or "column" for a merge_cuboids ``prefer`` value."""
    if prefer is None:
        return None
    key = str(prefer).strip().lower()
    if key in ("row", "rows", "r"):
        return "row"
    if key in ("column", "columns", "col", "c"):
        return "column"
    msg = f"Invalid prefer: {prefer!r}. Must be None, 'row'/'r' or 'column'/'c'."
    raise ValueError(msg)


def merge_cuboids(cuboids, tol=1e-10, bbox_order="point", prefer=None):
    """Merge overlapping or face-adjacent axis-aligned cuboids into a minimal set.

    The merge is greedy, and pairs are tried in list order, so a set of cells that
    could be merged into either east-west strips or north-south strips comes out
    however the order of the input happens to fall. By default that is what
    happens. With ``prefer``, the cuboids are instead merged twice: once along x
    first (into rows, i.e. east-west strips) and once along y first (into columns,
    i.e. north-south strips), each followed by the full merge, and whichever yields
    fewer cuboids wins; when both yield the same number, the preferred one is
    returned.

    Args:
        cuboids (list[tuple]): Each cuboid as (xmin, ymin, zmin, xmax, ymax, zmax) if bbox_order="point"
            Each cuboid as (xmin, xmax, ymin, ymax, zmin, zmax) if bbox_order="axis"
        tol (float): Numerical tolerance for equality checks.
        bbox_order (str): Alignment of the bbox coordinates.
            If 'point' or 'xyzxyz', the coordinates are assumed to be in (x1, y1, z1, x2, y2, z2) format, defining the first point (0:3) and second point (3:6)
            if 'axis' or 'xxyyzz', the coordinates are asummed to be in (x1, x2, y1, y2, z1, z2) format, defining the coords in the x (0:2), y (2:4), and z (4:6) directions.
            Raise ValueError if bbox_order is not 'point' or 'axis'.
        prefer (str | None): None (the default) merges in the given order only.
            'row' (or 'r') and 'column' (or 'c'), in any capitalization and with
            surrounding whitespace ignored, try both orientations and break a tie in
            favour of that one.

    Raises:
        ValueError: If some other order besides "point" or "axis" is given, or
            ``prefer`` is not one of the accepted values.

    Returns:
        list[tuple]
            Simplified list of merged cuboids.

    """
    cuboids = [tuple(map(float, c)) for c in cuboids]
    prefer = _normalize_merge_preference(prefer)

    bbox_order = bbox_order.lower().strip()

    if bbox_order == "xyzxyz":
        bbox_order = "point"
    elif bbox_order == "xxyyzz":
        bbox_order = "axis"

    if bbox_order == "axis":
        # Switch all the cuboids from axis order to point order for processing
        cuboids = [(x1, y1, z1, x2, y2, z2) for (x1, x2, y1, y2, z1, z2) in cuboids]
    elif bbox_order != "point":
        msg = f"Invalid bbox_order: {bbox_order}. Must be 'point', 'axis', 'xyzxyz', or 'xxyyzz'."
        raise ValueError(msg)

    def can_merge(a, b, along=None):
        """Return merged cuboid if a and b are mergeable, else None.

        With ``along`` set to "x", "y" or "z", only a merge along that axis counts.
        """
        ax1, ay1, az1, ax2, ay2, az2 = a
        bx1, by1, bz1, bx2, by2, bz2 = b

        # Overlapping or touching check
        overlap_x = not (ax2 < bx1 - tol or bx2 < ax1 - tol)
        overlap_y = not (ay2 < by1 - tol or by2 < ay1 - tol)
        overlap_z = not (az2 < bz1 - tol or bz2 < az1 - tol)

        # Must be aligned exactly in 2 axes, and touching or overlapping in the third
        # Case 1: merge along X
        if (
            along in (None, "x")
            and abs(ay1 - by1) < tol
            and abs(ay2 - by2) < tol
            and abs(az1 - bz1) < tol
            and abs(az2 - bz2) < tol
            and (abs(ax2 - bx1) < tol or abs(bx2 - ax1) < tol or overlap_x)
        ):
            return (min(ax1, bx1), ay1, az1, max(ax2, bx2), ay2, az2)

        # Case 2: merge along Y
        if (
            along in (None, "y")
            and abs(ax1 - bx1) < tol
            and abs(ax2 - bx2) < tol
            and abs(az1 - bz1) < tol
            and abs(az2 - bz2) < tol
            and (abs(ay2 - by1) < tol or abs(by2 - ay1) < tol or overlap_y)
        ):
            return (ax1, min(ay1, by1), az1, ax2, max(ay2, by2), az2)

        # Case 3: merge along Z
        if (
            along in (None, "z")
            and abs(ax1 - bx1) < tol
            and abs(ax2 - bx2) < tol
            and abs(ay1 - by1) < tol
            and abs(ay2 - by2) < tol
            and (abs(az2 - bz1) < tol or abs(bz2 - az1) < tol or overlap_z)
        ):
            return (ax1, ay1, min(az1, bz1), ax2, ay2, max(az2, bz2))

        # Case 4: One completely supercedes the other, even if edges don't align.
        if (
            along is None
            and (
                ax1 <= bx1
                and ax2 >= bx2
                and ay1 <= by1
                and ay2 >= by2
                and az1 <= bz1
                and az2 >= bz2
            )
        ) or (
            along is None
            and bx1 <= ax1
            and bx2 >= ax2
            and by1 <= ay1
            and by2 >= ay2
            and bz1 <= az1
            and bz2 >= az2
        ):
            # Return the polygon with the greatest volume.
            return max(
                a,
                b,
                key=lambda c: (c[3] - c[0]) * (c[4] - c[1]) * (c[5] - c[2]),
            )

        return None

    def greedy_merge(cuboids, along=None):
        """Merge pairs in list order until nothing more merges (see can_merge)."""
        cuboids = cuboids[:]
        merged = True
        while merged:
            merged = False
            new_cuboids = []
            skip = set()

            for i in range(len(cuboids)):
                if i in skip:
                    continue
                a = cuboids[i]
                merged_with = False
                for j in range(i + 1, len(cuboids)):
                    if j in skip:
                        continue
                    b = cuboids[j]
                    merged_c = can_merge(a, b, along)
                    if merged_c:
                        new_cuboids.append(merged_c)
                        skip.add(j)
                        merged_with = True
                        merged = True
                        break
                if not merged_with and i not in skip:
                    new_cuboids.append(a)

            cuboids = new_cuboids
        return cuboids

    if prefer is None:
        cuboids = greedy_merge(cuboids)
    else:
        # Form the strips of the wanted orientation first, then let the full
        # merge join whatever strips still line up.
        by_row = greedy_merge(greedy_merge(cuboids, along="x"))
        by_column = greedy_merge(greedy_merge(cuboids, along="y"))
        if len(by_row) == len(by_column):
            cuboids = by_row if prefer == "row" else by_column
        else:
            cuboids = min(by_row, by_column, key=len)

    # Deduplicate & clean zero-volume
    result = []
    for c in cuboids:
        x1, y1, z1, x2, y2, z2 = c
        if (x2 - x1 > tol) and (y2 - y1 > tol) and (z2 - z1 > tol) and c not in result:
            result.append(c)

    # Convert back to axis order if needed
    if bbox_order == "axis":
        result = [(x1, x2, y1, y2, z1, z2) for (x1, y1, z1, x2, y2, z2) in result]

    return result


def cuboids_intersect(c1, c2, tol=1e-10, bbox_order="point"):
    """Return True if two 3D cuboids intersect by a positive volume (not just touch).

    Args:
        c1: First cuboid, defined as (xmin, ymin, zmin, xmax, ymax, zmax).
        c2: Second cuboid, in the same layout as `c1`.
        tol: Small tolerance for floating-point comparisons.
        bbox_order: Alignment of the bbox coordinates.
            If 'point' or 'xyzxyz', the coordinates are assumed to be in
            (x1, y1, z1, x2, y2, z2) format, defining the first point (0:3) and
            second point (3:6).
            If 'axis' or 'xxyyzz', the coordinates are assumed to be in
            (x1, x2, y1, y2, z1, z2) format, defining the coords in the x (0:2),
            y (2:4), and z (4:6) directions.
            Raise ValueError if bbox_order is not 'point' or 'axis'.

    Returns:
        bool: True if the cuboids intersect by nonzero volume, False otherwise.

    """
    bbox_order = bbox_order.lower().strip()
    if bbox_order == "xyzxyz":
        bbox_order = "point"
    elif bbox_order == "xxyyzz":
        bbox_order = "axis"

    if bbox_order == "point":
        x1_min, y1_min, z1_min, x1_max, y1_max, z1_max = tuple(c1)
        x2_min, y2_min, z2_min, x2_max, y2_max, z2_max = tuple(c2)
    elif bbox_order == "axis":
        x1_min, x1_max, y1_min, y1_max, z1_min, z1_max = tuple(c1)
        x2_min, x2_max, y2_min, y2_max, z2_min, z2_max = tuple(c2)
    else:
        msg = f"Invalid bbox_order: {bbox_order}. Must be 'point' or 'axis'."
        raise ValueError(msg)

    # Overlap along each axis (strict inequalities for positive volume)
    overlap_x = (x1_min < x2_max - tol) and (x1_max > x2_min + tol)
    overlap_y = (y1_min < y2_max - tol) and (y1_max > y2_min + tol)
    overlap_z = (z1_min < z2_max - tol) and (z1_max > z2_min + tol)

    return overlap_x and overlap_y and overlap_z


def cuboids_intersect_vectorized(
    *,
    xmin,
    xmax,
    ymin,
    ymax,
    zmin,
    zmax,
    query,
    tol=1e-10,
    bbox_order="axis",
):
    """Vectorized form of cuboids_intersect: test many cuboids against one query.

    Args:
        xmin: Per-cuboid lower x bounds (one entry per candidate cuboid).
        xmax: Per-cuboid upper x bounds, the same length as `xmin`.
        ymin: Per-cuboid lower y bounds, the same length as `xmin`.
        ymax: Per-cuboid upper y bounds, the same length as `xmin`.
        zmin: Per-cuboid lower z bounds, the same length as `xmin`. For the IVERT
            granule index the "z" axis carries time (tmin/tmax), but the math is
            identical for any third axis.
        zmax: Per-cuboid upper z bounds, the same length as `xmin`.
        query: A single cuboid to test every candidate against, in `bbox_order`
            layout.
        tol: Small tolerance for floating-point comparisons.
        bbox_order: Layout of `query` (see cuboids_intersect): 'axis'/'xxyyzz'
            (default) is (xmin, xmax, ymin, ymax, zmin, zmax); 'point'/'xyzxyz'
            is (xmin, ymin, zmin, xmax, ymax, zmax).

    Returns:
        numpy.ndarray: Boolean mask, True wherever the candidate intersects
            `query` by positive volume (same strict-inequality, tolerance-based
            test as cuboids_intersect).

    """
    bbox_order = bbox_order.lower().strip()
    if bbox_order == "xyzxyz":
        bbox_order = "point"
    elif bbox_order == "xxyyzz":
        bbox_order = "axis"

    if bbox_order == "point":
        qxmin, qymin, qzmin, qxmax, qymax, qzmax = tuple(query)
    elif bbox_order == "axis":
        qxmin, qxmax, qymin, qymax, qzmin, qzmax = tuple(query)
    else:
        msg = f"Invalid bbox_order: {bbox_order}. Must be 'point' or 'axis'."
        raise ValueError(msg)

    xmin = np.asarray(xmin)
    xmax = np.asarray(xmax)
    ymin = np.asarray(ymin)
    ymax = np.asarray(ymax)
    zmin = np.asarray(zmin)
    zmax = np.asarray(zmax)

    return (
        (xmin < qxmax - tol)
        & (xmax > qxmin + tol)
        & (ymin < qymax - tol)
        & (ymax > qymin + tol)
        & (zmin < qzmax - tol)
        & (zmax > qzmin + tol)
    )
