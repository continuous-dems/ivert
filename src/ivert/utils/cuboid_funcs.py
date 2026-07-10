# import numpy as np
# from itertools import product

# def decompose_axis_aligned_polyhedron_general(vertices, faces, tol=1e-9):
#     """
#     Decompose a general axis-aligned polyhedron (can have convex faces, concavities, or holes)
#     into the smallest possible set of non-overlapping cuboids that exactly fill its volume.
#
#     Parameters
#     ----------
#     vertices: (N, 3) np.ndarray
#         Vertex coordinates.
#     faces: list[list[int]]
#         Each face is a list of vertex indices (ordered counterclockwise).
#         Faces must form a closed, watertight polyhedron.
#     tol: float
#         Tolerance for numerical rounding.
#
#     Returns
#     -------
#     list[tuple]
#         List of cuboids as (xmin, ymin, zmin, xmax, ymax, zmax).
#     """
#     vertices = np.asarray(vertices, dtype=float)
#     if vertices.shape[1] != 3:
#         raise ValueError("Vertices must be Nx3 array.")
#
#     # Extract distinct axis-aligned coordinate planes
#     xs = np.unique(np.round(vertices[:, 0], 9))
#     ys = np.unique(np.round(vertices[:, 1], 9))
#     zs = np.unique(np.round(vertices[:, 2], 9))
#     nx, ny, nz = len(xs) - 1, len(ys) - 1, len(zs) - 1
#
#     # Precompute face plane equations (n·x + d = 0)
#     face_planes = []
#     for f in faces:
#         pts = vertices[np.array(f)]
#         if len(pts) < 3:
#             continue
#         n = np.cross(pts[1] - pts[0], pts[2] - pts[0])
#         n = n / np.linalg.norm(n)
#         d = -np.dot(n, pts[0])
#         face_planes.append((n, d, pts))
#
#     def point_in_polyhedron(p):
#         """Ray casting along +X direction."""
#         px, py, pz = p
#         crossings = 0
#         for n, d, pts in face_planes:
#             # Skip faces parallel to ray direction (nx ~ 0)
#             if abs(n[0]) < tol:
#                 continue
#
#             # Solve for x intersection: n·x + d = 0  →  x = -(n_y*y + n_z*z + d)/n_x
#             x_int = -(n[1] * py + n[2] * pz + d) / n[0]
#
#             if x_int < px:
#                 continue  # behind ray start
#
#             # Now check if intersection point is inside the projected polygon (YZ plane)
#             yzs = pts[:, 1:3]
#             inside = False
#             for i in range(len(yzs)):
#                 y1, z1 = yzs[i]
#                 y2, z2 = yzs[(i + 1) % len(yzs)]
#                 if ((z1 > pz) != (z2 > pz)) and \
#                    (py < (y2 - y1) * (pz - z1) / (z2 - z1 + tol) + y1):
#                     inside = not inside
#             if inside:
#                 crossings += 1
#         return crossings % 2 == 1
#
#     # Fill voxel grid
#     grid = np.zeros((nx, ny, nz), dtype=bool)
#     for i, j, k in product(range(nx), range(ny), range(nz)):
#         xm = (xs[i] + xs[i + 1]) / 2
#         ym = (ys[j] + ys[j + 1]) / 2
#         zm = (zs[k] + zs[k + 1]) / 2
#         if point_in_polyhedron((xm, ym, zm)):
#             grid[i, j, k] = True
#
#     # Merge filled voxels into maximal cuboids
#     visited = np.zeros_like(grid, dtype=bool)
#     cuboids = []
#
#     for i, j, k in product(range(nx), range(ny), range(nz)):
#         if not grid[i, j, k] or visited[i, j, k]:
#             continue
#
#         # Expand along X
#         dx = 1
#         while i + dx < nx and np.all(grid[i + dx, j, k] & ~visited[i + dx, j, k]):
#             dx += 1
#
#         # Expand along Y
#         dy = 1
#         expand_y = True
#         while expand_y and j + dy < ny:
#             for ii in range(i, i + dx):
#                 if not grid[ii, j + dy, k] or visited[ii, j + dy, k]:
#                     expand_y = False
#                     break
#             if expand_y:
#                 dy += 1
#
#         # Expand along Z
#         dz = 1
#         expand_z = True
#         while expand_z and k + dz < nz:
#             for ii in range(i, i + dx):
#                 for jj in range(j, j + dy):
#                     if not grid[ii, jj, k + dz] or visited[ii, jj, k + dz]:
#                         expand_z = False
#                         break
#                 if not expand_z:
#                     break
#             if expand_z:
#                 dz += 1
#
#         visited[i:i+dx, j:j+dy, k:k+dz] = True
#         cuboids.append((
#             xs[i], ys[j], zs[k],
#             xs[i+dx], ys[j+dy], zs[k+dz]
#         ))
#
#     return cuboids

def subtract_cuboids(a, b, tol=1e-10, bbox_order="point"):
    """
    Subtract cuboid `b` from cuboid `a`, returning the list of non-overlapping cuboids
    that exactly fill the remaining volume (a - b).

    Parameters
    ----------
    a, b : tuple
        Each as (xmin, ymin, zmin, xmax, ymax, zmax)
    tol : float
        Numerical tolerance for floating-point comparisons. Differences in coordinates less than "tol" units shall
        be considered as the same point. Default: 1e-10
    bbox_order : str
        "axis" or "point", or conversely "xxyyzz" or "xyzxyz" (respectively). Default is "point".
        Point order assumes bounding boxes are in (x1, y1, z1, x2, y2, z2) format.
        Axis order assumes bounding boxes are in (x1, x2, y1, y2, z1, z2) format.
        Applies to both inputs and outputs.

    Raises
    ------
    ValueError: If the bounding-boxes are misorderd (where the first point is greater than the second point).

    Returns
    -------
    list[tuple]
        List of cuboids (xmin, ymin, zmin, xmax, ymax, zmax)
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
        raise ValueError(f"Invalid bbox_order parameter: {bbox_order}. Only 'axis' or 'point' are allowed.")

    # Make sure in each case that the points are in the correct order (that x2 is not less than x1, etc)
    if (ax1 > ax2) or (ay1 > ay2) or (az1 > az2):
        raise ValueError(
            f"Invalid bounding box: {a}. "
            "The first point must be less than or equal to the second point in each dimension. "
            "Double-check your 'bbox_order' parameter to make sure you choose the correct 'point' or 'axis' order."
        )
    if (bx1 > bx2) or (by1 > by2) or (bz1 > bz2):
        raise ValueError(
            f"Invalid bounding box: {b}. "
            "The first point must be less than or equal to the second point in each dimension. "
            "Double-check your 'bbox_order' parameter to make sure you choose the correct 'point' or 'axis' order."
        )

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
    for (x1, y1, z1, x2, y2, z2) in result:
        if (x2 - x1 > tol) and (y2 - y1 > tol) and (z2 - z1 > tol):
            clean.append((x1, y1, z1, x2, y2, z2))

    # If the bboxes were given in axis-order, re-order them from point order (above) before returning.
    if bbox_order in ("axis", "xxyyzz"):
        clean = [(x1, x2, y1, y2, z1, z2) for (x1, y1, z1, x2, y2, z2) in clean]

    return clean

def merge_cuboids(cuboids, tol=1e-10, bbox_order="point"):
    """
    Merge overlapping or face-adjacent axis-aligned cuboids into a minimal set.

    Parameters
    ----------
    cuboids : list[tuple]
        Each cuboid as (xmin, ymin, zmin, xmax, ymax, zmax) if bbox_order="point"
        Each cuboid as (xmin, xmax, ymin, ymax, zmin, zmax) if bbox_order="axis"
    tol : float
        Numerical tolerance for equality checks.
    bbox_order : str
        Alignment of the bbox coordinates.
        If 'point' or 'xyzxyz', the coordinates are assumed to be in (x1, y1, z1, x2, y2, z2) format, defining the first point (0:3) and second point (3:6)
        if 'axis' or 'xxyyzz', the coordinates are asummed to be in (x1, x2, y1, y2, z1, z2) format, defining the coords in the x (0:2), y (2:4), and z (4:6) directions.
        Raise ValueError if bbox_order is not 'point' or 'axis'.
        
    Raises
    ------
    ValueError: If some other order besides "point" or "axis" is given.

    Returns
    -------
    list[tuple]
        Simplified list of merged cuboids.
    """
    cuboids = [tuple(map(float, c)) for c in cuboids]
    merged = True

    bbox_order = bbox_order.lower().strip()

    if bbox_order == "xyzxyz":
        bbox_order = "point"
    elif bbox_order == "xxyyzz":
        bbox_order = "axis"

    if bbox_order == "axis":
        # Switch all the cuboids from axis order to point order for processing
        cuboids = [(x1, y1, z1, x2, y2, z2) for (x1, x2, y1, y2, z1, z2) in cuboids]
    elif bbox_order != "point":
        raise ValueError(f"Invalid bbox_order: {bbox_order}. Must be 'point', 'axis', 'xyzxyz', or 'xxyyzz'.")

    def can_merge(a, b):
        """Return merged cuboid if a and b are mergeable, else None."""
        ax1, ay1, az1, ax2, ay2, az2 = a
        bx1, by1, bz1, bx2, by2, bz2 = b

        # Overlapping or touching check
        overlap_x = not (ax2 < bx1 - tol or bx2 < ax1 - tol)
        overlap_y = not (ay2 < by1 - tol or by2 < ay1 - tol)
        overlap_z = not (az2 < bz1 - tol or bz2 < az1 - tol)

        # Must be aligned exactly in 2 axes, and touching or overlapping in the third
        # Case 1: merge along X
        if abs(ay1 - by1) < tol and abs(ay2 - by2) < tol \
           and abs(az1 - bz1) < tol and abs(az2 - bz2) < tol:
            if abs(ax2 - bx1) < tol or abs(bx2 - ax1) < tol or overlap_x:
                return (min(ax1, bx1), ay1, az1, max(ax2, bx2), ay2, az2)

        # Case 2: merge along Y
        if abs(ax1 - bx1) < tol and abs(ax2 - bx2) < tol \
           and abs(az1 - bz1) < tol and abs(az2 - bz2) < tol:
            if abs(ay2 - by1) < tol or abs(by2 - ay1) < tol or overlap_y:
                return (ax1, min(ay1, by1), az1, ax2, max(ay2, by2), az2)

        # Case 3: merge along Z
        if abs(ax1 - bx1) < tol and abs(ax2 - bx2) < tol \
           and abs(ay1 - by1) < tol and abs(ay2 - by2) < tol:
            if abs(az2 - bz1) < tol or abs(bz2 - az1) < tol or overlap_z:
                return (ax1, ay1, min(az1, bz1), ax2, ay2, max(az2, bz2))

        # Case 4: One completely supercedes the other, even if edges don't align.
        if (ax1 <= bx1 and ax2 >= bx2 and ay1 <= by1 and ay2 >= by2 and az1 <= bz1 and az2 >= bz2) \
            or (bx1 <= ax1 and bx2 >= ax2 and by1 <= ay1 and by2 >= ay2 and bz1 <= az1 and bz2 >= az2):
            # Return the polygon with the greatest volume.
            return max(a, b, key=lambda c: (c[3] - c[0])*(c[4] - c[1])*(c[5] - c[2]))

        return None

    cuboids = cuboids[:]
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
                merged_c = can_merge(a, b)
                if merged_c:
                    new_cuboids.append(merged_c)
                    skip.add(j)
                    merged_with = True
                    merged = True
                    break
            if not merged_with and i not in skip:
                new_cuboids.append(a)

        cuboids = new_cuboids

    # Deduplicate & clean zero-volume
    result = []
    for c in cuboids:
        x1, y1, z1, x2, y2, z2 = c
        if (x2 - x1 > tol) and (y2 - y1 > tol) and (z2 - z1 > tol):
            if c not in result:
                result.append(c)

    # Convert back to axis order if needed
    if bbox_order == "axis":
        result = [(x1, x2, y1, y2, z1, z2) for (x1, y1, z1, x2, y2, z2) in result]

    return result

def cuboids_intersect(c1, c2, tol=1e-10, bbox_order="point"):
    """
    Return True if two 3D cuboids intersect by a positive volume (not just touch).

    Parameters
    ----------
    c1, c2 : tuple
        Cuboids defined as (xmin, ymin, zmin, xmax, ymax, zmax)
    tol : float
        Small tolerance for floating-point comparisons.
    bbox_order : str
        Alignment of the bbox coordinates.
        If 'point' or 'xyzxyz', the coordinates are assumed to be in (x1, y1, z1, x2, y2, z2) format, defining the first point (0:3) and second point (3:6)
        if 'axis' or 'xxyyzz', the coordinates are asummed to be in (x1, x2, y1, y2, z1, z2) format, defining the coords in the x (0:2), y (2:4), and z (4:6) directions.
        Raise ValueError if bbox_order is not 'point' or 'axis'.

    Returns
    -------
    bool
        True if the cuboids intersect by nonzero volume, False otherwise.
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
        raise ValueError(f"Invalid bbox_order: {bbox_order}. Must be 'point' or 'axis'.")

    # Overlap along each axis (strict inequalities for positive volume)
    overlap_x = (x1_min < x2_max - tol) and (x1_max > x2_min + tol)
    overlap_y = (y1_min < y2_max - tol) and (y1_max > y2_min + tol)
    overlap_z = (z1_min < z2_max - tol) and (z1_max > z2_min + tol)

    return overlap_x and overlap_y and overlap_z

#################################################################################
## Tests
#################################################################################

# def test_L():
#     # Create a blocky "L" shape
#     vertices = np.array([
#         [0,0,0], # 0
#         [2,0,0], # 1
#         [2,1,0], # 2
#         [0,2,0], # 3
#         [1,2,0], # 4
#         [1,1,0], # 5
#         [0,0,1], # 6
#         [2,0,1], # 7
#         [2,1,1], # 8
#         [0,2,1], # 9
#         [1,2,1], # 10
#         [1,1,1], # 11
#     ])
#
#     # Define faces (each as list of vertex indices forming one face)
#     faces = [
#         [0,1,2,5,4,3], # front face
#         [0,6,9,3], # left side (tall)
#         [1,7,8,2], # right side (lower)
#         [5,11,10,4], # right side (upper)
#         [0,1,7,6], # bottom
#         [3,4,10,9], # top, upper
#         [2,8,11,5], # top, lower
#         [6,7,8,11,10,9] # back face
#     ]
#
#     cuboids = decompose_axis_aligned_polyhedron_general(vertices, faces)
#     print(f"Decomposed into {len(cuboids)} cuboids:")
#     for c in cuboids:
#         print(c)
#
# def test_cube_w_corner_missing():
#     # Create a blocky "L" shape
#     vertices = np.array([
#         [0,0,0], # 0
#         [2,0,0], # 1
#         [2,1,0], # 2
#         [1,1,0], # 3
#         [1,2,0], # 4
#         [0,2,0], # 5
#         [0,0,2], # 6
#         [2,0,2], # 7
#         [2,2,2], # 8
#         [0,2,2], # 9
#         [2,1,1], # 10
#         [2,2,1], # 11
#         [1,2,1], # 12
#         [1,1,1], # 13
#     ])
#
#     faces = [
#         [0,6,9,5], # left face (square)
#         [6,7,8,9], # back face (square)
#         [0,1,7,6], # bottom side (square)
#         [1,7,8,11,10,2], # right face (L)
#         [5,4,12,11,8,9], # top face (L)
#         [0,1,2,3,4,5], # front face (L)
#         [3,13,12,4], # right-facing side cutout (small square)
#         [3,2,10,13], # up-facing side cutout (small square)
#         [10,11,12,13], # front-facing side cutout (small square)
#     ]
#
#     cuboids = decompose_axis_aligned_polyhedron_general(vertices, faces)
#     print(f"Decomposed into {len(cuboids)} cuboids:")
#     for c in cuboids:
#         print(c)

# def test_hollow_cube():
#     # Outer cube (0,0,0)-(3,3,3)
#     # Inner cube hole (1,1,1)-(2,2,2)
#     verts = np.array([
#         [0, 0, 0], [3, 0, 0], [3, 3, 0], [0, 3, 0],
#         [0, 0, 3], [3, 0, 3], [3, 3, 3], [0, 3, 3],
#         [1, 1, 1], [2, 1, 1], [2, 2, 1], [1, 2, 1],
#         [1, 1, 2], [2, 1, 2], [2, 2, 2], [1, 2, 2]
#     ])
#
#     # Faces — simplified for demonstration: outer + inner boundaries
#     faces = [
#         [0, 1, 2, 3], [4, 5, 6, 7], [0, 4, 7, 3], [1, 5, 6, 2], [3, 2, 6, 7], [0, 1, 5, 4],  # outer cube
#         [8, 9, 10, 11], [12, 13, 14, 15], [8, 12, 15, 11], [9, 13, 14, 10],
#         [11, 10, 14, 15], [8, 9, 13, 12]  # inner hole
#     ]
#
#     cuboids = decompose_axis_aligned_polyhedron_general(verts, faces)
#     print(f"Decomposed into {len(cuboids)} cuboids:")
#     for c in cuboids:
#         print(c)

# def test_2_separate_cubes():
#     verts = np.array([
#         [0,0,0],[1,0,0],[1,1,0],[0,1,0],[0,0,1],[1,0,1],[1,1,1],[0,1,1], # cube 1
#         [2,0,0],[3,0,0],[3,1,0],[2,1,0],[2,0,1],[3,0,1],[3,1,1],[2,1,1], # cube 2
#     ])
#
#     faces = [
#         [0,1,2,3], [4,5,6,7], [0,1,5,4], [3,2,6,7], [0,4,7,3], [1,5,6,2], # faces, cube 1
#         [8,9,10,11], [12, 13, 14, 15], [8, 9, 13, 12], [11, 10, 14, 15], [8, 12, 15, 11], [9, 13, 14, 10],  # faces, cube 2
#     ]
#
#     cuboids = decompose_axis_aligned_polyhedron_general(verts, faces)
#     print(f"Decomposed into {len(cuboids)} cuboids:")
#     for c in cuboids:
#         print(c)

# def test_difference_corner():
#     A = (0,0,0,3,3,3)
#     B = (1,1,1,3,3,3)
#     print("Subtract corner:")
#     print("A", A)
#     print("B", B)
#     print("A-B", subtract_cuboids(A, B))


# def test_difference_self():
#     A = (0,0,0,1,1,1)
#     B = A
#     print("Subtract self:")
#     print("A", A)
#     print("B", B)
#     print("A-B", subtract_cuboids(A, B))

# def test_difference_not_overlapping():
#     A = (0,0,0,1,1,1)
#     B = (3,3,3,4,4,4)
#     print("Subtract non-overlapping:")
#     print("A", A)
#     print("B", B)
#     print("A-B", subtract_cuboids(A, B))

# def test_difference_hole_accross():
#     A = (0,0,0,3,3,1)
#     B = (1,1,0,2,2,1)
#     print("Subtract hole spanning accross:")
#     print("A", A)
#     print("B", B)
#     print("A-B", subtract_cuboids(A, B))

# def test_difference_hole_in_center():
#     A = (0,0,0,3,3,3)
#     B = (1,1,1,2,2,2)
#     print("Subtract hole in center (hollow cube):")
#     print("A", A)
#     print("B", B)
#     print("A-B", subtract_cuboids(A, B))

# def test_difference_l_non_adjoining():
#     A = (0,0,0,2,2,2)
#     B = (-1,1,1,4,2,2)
#     print("Subtract L-corner (with non-adjoining polyhedra):")
#     print("A", A)
#     print("B", B)
#     print("A-B", subtract_cuboids(A, B))



if __name__ == "__main__":
    pass
    # test_difference_corner()
    # print("======")
    # test_difference_self()
    # print("======")
    # test_difference_not_overlapping()
    # print("======")
    # test_difference_hole_accross()
    # print("======")
    # test_difference_hole_in_center()
    # print("======")
    # test_difference_l_non_adjoining()