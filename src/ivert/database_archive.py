"""Pack an IVERT database into a zip archive, and restore one into a database.

'ivert database dump' writes the photon granule files and the landmask store of a
database, or of a region and date range of it, to a single compressed zip, with a
manifest and a summary of what is inside. 'ivert database restore' unpacks such a
zip into the local database and rebuilds its index.

Granule files are cut to the region with IS2Database.clip_granule_file(), which
writes the photons of a file that fall in an (x, y, t) cuboid to a file of their
own, whose query box is that cuboid. The database records what it covers by those
query boxes, so a clipped file claims only the ground it really holds.

Where a restored archive overlaps data already in the database, the caller picks
one of OVERLAP_MODES:

    all      import everything, even where it duplicates photons already stored
    keep     keep the existing data, and import only what lies outside it
    replace  import everything, and cut the imported area out of the existing data
    cancel   import nothing
"""

import contextlib
import dataclasses
import datetime
import json
import logging
import os
import pathlib
import shutil
import tempfile
import zipfile

import numpy as np
import tqdm
import tqdm.contrib.logging

import ivert
import ivert.icesat2_database_v2
import ivert.landmask
import ivert.utils.cuboid_funcs

logger = logging.getLogger(__name__)

FORMAT = "ivert-database-dump"
FORMAT_VERSION = 1

MANIFEST_NAME = "manifest.json"
SUMMARY_NAME = "summary.txt"
GRANULES_ARCDIR = "granules"
LANDMASKS_ARCDIR = "landmasks"

OVERLAP_MODES = ("all", "keep", "replace", "cancel")

# Per-class photon counts in a granule record, in the order the summary lists them.
_CLASS_COUNTS = (
    ("numphotons_ground", "Ground (1)"),
    ("numphotons_canopy", "Canopy (2)"),
    ("numphotons_canopy_top", "Canopy top (3)"),
    ("numphotons_ice_surface", "Land ice (6)"),
    ("numphotons_buildings", "Buildings (7)"),
    ("numphotons_bathy_floor", "Bathymetry floor (40)"),
    ("numphotons_bathy_surface", "Bathymetry surface (41)"),
    ("numphotons_inland_water_surface", "Inland water surface (42)"),
    ("numphotons_noise", "Noise (0)"),
    ("numphotons_unclassified", "Unclassified (-1)"),
)

_QUERY_COLS = ivert.icesat2_database_v2.IS2Database._bbox_cols("query_bbox")
_DATA_COLS = ivert.icesat2_database_v2.IS2Database._bbox_cols("data_bbox")


class ArchiveError(Exception):
    """A dump could not be written, or an archive cannot be restored."""


###############################################################
# Box arithmetic
###############################################################


def _move(src, dst) -> None:
    """Move a file, replacing any file already at dst."""
    pathlib.Path(src).replace(dst)


def _intersection(a, b):
    """The overlap of two same-length axis-order boxes, or None if it has no volume."""
    out = []
    for i in range(0, len(a), 2):
        lo, hi = max(a[i], b[i]), min(a[i + 1], b[i + 1])
        if hi <= lo:
            return None
        out.extend((lo, hi))
    return tuple(out)


def _contains(outer, inner) -> bool:
    """Whether an axis-order box lies entirely inside another."""
    return all(
        outer[i] <= inner[i] and inner[i + 1] <= outer[i + 1]
        for i in range(0, len(outer), 2)
    )


def _subtract_all(box, others) -> list:
    """What is left of an axis-order (x, y, t) cuboid after removing all of others."""
    pieces = [tuple(box)]
    for other in others:
        remaining = []
        for piece in pieces:
            remaining.extend(
                ivert.utils.cuboid_funcs.subtract_cuboids(
                    piece,
                    tuple(other),
                    bbox_order="axis",
                ),
            )
        pieces = remaining
    return pieces


def _subtract_all_2d(rect, others) -> list:
    """What is left of an (xmin, xmax, ymin, ymax) rectangle after removing others."""
    pieces = _subtract_all((*rect, 0, 1), [(*o, 0, 1) for o in others])
    return [p[:4] for p in pieces]


def _query_cuboid(record) -> tuple:
    """The (xmin, xmax, ymin, ymax, tmin, tmax) query box of a granule record."""
    return tuple(float(record[c]) for c in _QUERY_COLS[:4]) + tuple(
        int(record[c]) for c in _QUERY_COLS[4:]
    )


def _data_cuboid(data_bbox) -> tuple:
    """A granule's data box as a half-open (x, y, t) cuboid that holds all its photons.

    The stored data box runs from the first photon to the last, both included, so
    it is widened by a hair in x and y and by a day in t.
    """
    xmin, xmax, ymin, ymax, tmin, tmax = data_bbox
    eps = 1e-9
    return (
        float(xmin) - eps,
        float(xmax) + eps,
        float(ymin) - eps,
        float(ymax) + eps,
        int(tmin),
        ivert.icesat2_database_v2.IS2Database.increment_yyyymmdd_by_n(tmax, 1),
    )


def _touches_any(cuboid, others) -> bool:
    return any(
        ivert.utils.cuboid_funcs.cuboids_intersect(cuboid, o, bbox_order="axis")
        for o in others
    )


def _merged(cuboids) -> list:
    """Unique cuboids, merged where they combine exactly into larger ones."""
    unique = sorted({tuple(c) for c in cuboids})
    if not unique:
        return []
    return ivert.utils.cuboid_funcs.merge_cuboids(unique, bbox_order="axis")


###############################################################
# Progress bars and archive members
###############################################################

# Bytes copied at a time into or out of an archive, between progress-bar updates.
_COPY_CHUNK = 4 * 1024 * 1024


@contextlib.contextmanager
def _progress_bar(**kwargs: object):
    """A tqdm progress bar, drawn only at 'info' verbosity or above."""
    # 'disable=None' tells tqdm to draw the bar only when attached to a terminal,
    # and stay silent when output is redirected to a file or a pipe. Log records
    # are routed through tqdm.write() meanwhile, so a warning doesn't break the bar.
    with (
        tqdm.contrib.logging.logging_redirect_tqdm(),
        tqdm.tqdm(
            disable=None if logger.isEnabledFor(logging.INFO) else True,
            **kwargs,
        ) as bar,
    ):
        yield bar


def _progress(iterable, **kwargs: object):
    """Iterate over iterable, advancing a progress bar as each item is finished."""
    if hasattr(iterable, "__len__"):
        kwargs.setdefault("total", len(iterable))
    kwargs.setdefault("unit", "file")
    with _progress_bar(**kwargs) as bar:
        for item in iterable:
            yield item
            bar.update()


def _byte_bar(total, desc):
    """A progress bar counting bytes."""
    return _progress_bar(
        total=total,
        desc=desc,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
    )


def _write_member(zf, src, arcname, bar) -> None:
    """Add a file to an open zip archive, advancing bar by the bytes it reads."""
    info = zipfile.ZipInfo.from_file(src, arcname)
    info.compress_type = zf.compression
    with open(src, "rb") as fin, zf.open(info, "w") as fout:
        while chunk := fin.read(_COPY_CHUNK):
            fout.write(chunk)
            bar.update(len(chunk))


def _extract_all(zf, dest) -> None:
    """Unpack every file of an open zip archive into dest, with a progress bar.

    Raises:
        ArchiveError: if a member's name would put it outside dest.

    """
    root = os.path.realpath(dest)
    infos = [info for info in zf.infolist() if not info.is_dir()]
    with _byte_bar(sum(i.file_size for i in infos), "Unpacking archive") as bar:
        for info in infos:
            target = os.path.realpath(os.path.join(root, info.filename))
            if os.path.commonpath([root, target]) != root:
                msg = f"{zf.filename} holds a file outside its own folders: {info.filename}"
                raise ArchiveError(msg)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as fin, open(target, "wb") as fout:
                while chunk := fin.read(_COPY_CHUNK):
                    fout.write(chunk)
                    bar.update(len(chunk))


###############################################################
# Manifest and summary
###############################################################


def _jsonable(value):
    """A record value as a plain JSON type."""
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    return value


def _fmt_date(yyyymmdd) -> str:
    s = str(int(yyyymmdd))
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def _fmt_rect(rect) -> str:
    xmin, xmax, ymin, ymax = rect
    return f"W {xmin:g} to E {xmax:g}, S {ymin:g} to N {ymax:g}"


def _extent(rects):
    rects = list(rects)
    return (
        min(r[0] for r in rects),
        max(r[1] for r in rects),
        min(r[2] for r in rects),
        max(r[3] for r in rects),
    )


def summarize(manifest: dict) -> str:
    """A human-readable summary of what an archive holds."""
    granules = manifest["granules"]
    landmasks = manifest["landmasks"]
    region = manifest["region"]

    lines = [
        "IVERT database dump",
        f"  Created: {manifest['created_utc']} by IVERT {manifest['ivert_version']}",
    ]
    if region["rectangles"] is None:
        lines.append("  Requested region: the whole database")
    else:
        lines.append(f"  Requested region: {len(region['rectangles'])} rectangle(s)")
        lines.extend(f"    {_fmt_rect(r)}" for r in region["rectangles"])
    if region["date_range"] is None:
        lines.append("  Requested dates: all")
    else:
        t0, t1 = region["date_range"]
        lines.append(f"  Requested dates: {_fmt_date(t0)} up to {_fmt_date(t1)}")

    if not granules:
        lines.append("  Granule files: none")
    else:
        queries = sorted({tuple(g["query_bbox"]) for g in granules})
        lines.append(
            f"  Granule files: {len(granules):,} "
            f"(from {len({g['source_granule'] for g in granules}):,} ICESat-2 granules)",
        )
        lines.append(
            f"  Query boxes: {len(queries):,}, covering "
            f"{_fmt_rect(_extent(q[:4] for q in queries))}, "
            f"dates {_fmt_date(min(q[4] for q in queries))} to "
            f"{_fmt_date(max(q[5] for q in queries))}",
        )
        lines.append(
            "  Photon dates: "
            f"{_fmt_date(min(g['data_bbox'][4] for g in granules))} to "
            f"{_fmt_date(max(g['data_bbox'][5] for g in granules))}",
        )
        total = sum(g["numphotons"] for g in granules)
        lines.append(f"  Photons: {total:,}")
        width = max(len(label) for _, label in _CLASS_COUNTS)
        for col, label in _CLASS_COUNTS:
            n = sum(g.get(col, 0) for g in granules)
            if n:
                lines.append(f"    {label:<{width}}  {n:>14,}")
    lines.append(
        f"  Datums: horizontal {', '.join(manifest['horizontal_datums']) or 'n/a'}, "
        f"vertical {', '.join(manifest['vertical_datums']) or 'n/a'}",
    )
    if landmasks:
        lines.append(
            f"  Landmask tiles: {len(landmasks):,}, covering "
            f"{_fmt_rect(_extent(lm['bbox'] for lm in landmasks))}",
        )
    else:
        lines.append("  Landmask tiles: none")
    return "\n".join(lines)


def read_manifest(archive: str) -> dict:
    """The manifest of an archive, checked for a format this IVERT can restore."""
    try:
        with zipfile.ZipFile(archive) as zf:
            manifest = json.loads(zf.read(MANIFEST_NAME))
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        msg = f"{archive} is not an IVERT database dump: {exc}"
        raise ArchiveError(msg) from exc
    if manifest.get("format") != FORMAT:
        msg = f"{archive} is not an IVERT database dump."
        raise ArchiveError(msg)
    if int(manifest.get("format_version", 0)) > FORMAT_VERSION:
        msg = (
            f"{archive} was written by a newer IVERT (dump format "
            f"{manifest['format_version']}; this IVERT reads up to {FORMAT_VERSION}). "
            "Upgrade IVERT to restore it."
        )
        raise ArchiveError(msg)
    # Restore joins these names onto the database's own directories, so each must
    # be a plain file name that cannot reach outside them.
    for section in ("granules", "landmasks"):
        for entry in manifest.get(section, []):
            name = entry.get("filename") if isinstance(entry, dict) else None
            if (
                not isinstance(name, str)
                or name in ("", ".", "..")
                or name != os.path.basename(name)
            ):
                msg = f"{archive} lists an invalid {section} file name: {name!r}"
                raise ArchiveError(msg)
    return manifest


###############################################################
# Dump
###############################################################


@dataclasses.dataclass
class DumpResult:
    """What dump() wrote."""

    path: str
    manifest: dict
    summary: str


def dump(
    output_zip: str,
    region_rects=None,
    date_range=None,
    db=None,
) -> DumpResult:
    """Write a database, or part of it, to a zip archive.

    Args:
        output_zip: the archive to write. It is written beside itself under a
            temporary name and renamed when complete.
        region_rects: (xmin, xmax, ymin, ymax) rectangles to limit the dump to, in
            WGS84. None dumps everything.
        date_range: (tmin, tmax) YYYYMMDD dates to limit the dump to, tmax not
            included. None dumps every date.
        db: the IS2Database to dump. Defaults to the configured one.

    Returns:
        A DumpResult.

    Raises:
        ArchiveError: if the database is empty or nothing falls in the region.

    """
    db = db or ivert.icesat2_database_v2.IS2Database()
    gdf = db.open_gdf()
    if gdf is None or len(gdf) == 0:
        msg = f"The IVERT database at {db.db_fname} is empty; there is nothing to dump."
        raise ArchiveError(msg)

    t_lo, t_hi = date_range if date_range is not None else (0, 99991231)
    rects = (
        [tuple(float(v) for v in r) for r in region_rects]
        if region_rects is not None
        else None
    )
    region = (
        [(*r, int(t_lo), int(t_hi)) for r in rects]
        if rects is not None
        else [(-180.0, 180.0, -90.0, 90.0, int(t_lo), int(t_hi))]
    )

    staging = tempfile.mkdtemp(
        prefix=".ivert_dump_",
        dir=os.path.dirname(os.path.abspath(output_zip)),
    )
    try:
        members = []  # (source path, name in the archive)
        granule_records = []
        for _, row in _progress(
            gdf.iterrows(),
            total=len(gdf),
            desc="Staging granules",
        ):
            src = os.path.join(db.granules_dir, row["filename"])
            if not os.path.exists(src):
                logger.warning("Skipping missing granule file %s.", row["filename"])
                continue
            cuboid = _query_cuboid(row)
            if any(_contains(r, cuboid) for r in region):
                members.append((src, f"{GRANULES_ARCDIR}/{row['filename']}"))
                granule_records.append({k: _jsonable(v) for k, v in row.items()})
                continue
            pieces = [p for p in (_intersection(cuboid, r) for r in region) if p]
            if not pieces:
                continue
            for record in db.clip_granule_file(src, pieces, staging):
                members.append(
                    (
                        os.path.join(staging, record["filename"]),
                        f"{GRANULES_ARCDIR}/{record['filename']}",
                    ),
                )
                granule_records.append({k: _jsonable(v) for k, v in record.items()})

        if not granule_records:
            msg = "No photons in the database fall in the requested region and dates."
            raise ArchiveError(msg)

        landmask_staging = os.path.join(staging, "landmasks")
        landmasks = []
        stored = ivert.landmask.stored_landmasks(db.landmask_dir)
        for path, bbox in _progress(stored.items(), desc="Staging landmasks"):
            if rects is None or any(_contains(r, bbox) for r in rects):
                pieces = [(path, bbox)]
            else:
                pieces = []
                for r in rects:
                    part = _intersection(bbox, r)
                    if part:
                        pieces.append(
                            (
                                ivert.landmask.clip_stored_landmask(
                                    path,
                                    part,
                                    landmask_staging,
                                ),
                                part,
                            ),
                        )
            for src, part in pieces:
                name = os.path.basename(src)
                members.append((src, f"{LANDMASKS_ARCDIR}/{name}"))
                landmasks.append({"filename": name, "bbox": list(part)})

        manifest = _build_manifest(granule_records, landmasks, rects, date_range)
        summary = summarize(manifest)

        tmp_zip = output_zip + ".tmp"
        with zipfile.ZipFile(
            tmp_zip,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as zf:
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=1))
            zf.writestr(SUMMARY_NAME, summary + "\n")
            total = sum(os.path.getsize(src) for src, _ in members)
            with _byte_bar(total, "Compressing") as bar:
                for src, arcname in members:
                    _write_member(zf, src, arcname, bar)
        _move(tmp_zip, output_zip)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        with contextlib.suppress(OSError):
            os.remove(output_zip + ".tmp")

    return DumpResult(path=output_zip, manifest=manifest, summary=summary)


def _build_manifest(granule_records, landmasks, rects, date_range) -> dict:
    granules = [
        {
            "filename": r["filename"],
            "source_granule": r["source_granule"],
            "query_bbox": list(_query_cuboid(r)),
            "data_bbox": [float(r[c]) for c in _DATA_COLS[:4]]
            + [int(r[c]) for c in _DATA_COLS[4:]],
            "horizontal_datum": r.get("horizontal_datum", ""),
            "vertical_datum": r.get("vertical_datum", ""),
            **{
                col: int(r.get(col, 0))
                for col in ("numphotons", *(c for c, _ in _CLASS_COUNTS))
            },
        }
        for r in granule_records
    ]
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "ivert_version": ivert.__version__,
        "created_utc": datetime.datetime.now(datetime.UTC).strftime(
            "%Y-%m-%d %H:%M UTC",
        ),
        "region": {
            "rectangles": [list(r) for r in rects] if rects is not None else None,
            "date_range": list(date_range) if date_range is not None else None,
        },
        "horizontal_datums": sorted({g["horizontal_datum"] for g in granules} - {""}),
        "vertical_datums": sorted({g["vertical_datum"] for g in granules} - {""}),
        "granules": granules,
        "landmasks": landmasks,
    }


###############################################################
# Restore
###############################################################


@dataclasses.dataclass
class RestorePlan:
    """What restoring an archive into a database would involve."""

    archive: str
    manifest: dict
    summary: str
    # Imported query cuboids that overlap query cuboids already in the database.
    overlaps: list


@dataclasses.dataclass
class RestoreResult:
    """What restore() did."""

    granules_added: int = 0
    granules_clipped: int = 0
    granules_skipped: int = 0
    existing_clipped: int = 0
    existing_removed: int = 0
    landmasks_added: int = 0
    n_index_records: int = 0


def plan_restore(archive: str, db=None) -> RestorePlan:
    """Read an archive and check it against the database, without changing anything.

    Raises:
        ArchiveError: if the archive is not a dump this IVERT can read, or its
            vertical datum differs from the database's.

    """
    db = db or ivert.icesat2_database_v2.IS2Database()
    manifest = read_manifest(archive)

    gdf = db.open_gdf()
    existing = []
    if gdf is not None and len(gdf) > 0:
        ours = sorted(set(gdf["vertical_datum"].astype(str)) - {""})
        theirs = manifest["vertical_datums"]
        if ours and theirs and set(ours) != set(theirs):
            msg = (
                f"The archive's photon heights are in {', '.join(theirs)}, but this "
                f"database's are in {', '.join(ours)}. Mixing them would give wrong "
                "validations, so the archive was not restored. Restore it into a "
                "database set up for the same vertical datum "
                "(see 'ivert options icesat2_vertical_datum')."
            )
            raise ArchiveError(msg)
        existing = _merged(db.unique_bboxes(data_or_query="query") or [])

    imported = sorted({tuple(g["query_bbox"]) for g in manifest["granules"]})
    overlaps = [
        c
        for c in imported
        if any(
            ivert.utils.cuboid_funcs.cuboids_intersect(c, e, bbox_order="axis")
            for e in existing
        )
    ]
    return RestorePlan(
        archive=archive,
        manifest=manifest,
        summary=summarize(manifest),
        overlaps=overlaps,
    )


def restore(plan: RestorePlan, mode: str, db=None) -> RestoreResult:
    """Unpack an archive into the database and rebuild its index.

    Args:
        plan: from plan_restore().
        mode: one of OVERLAP_MODES. It only matters where the archive overlaps the
            database; 'cancel' does nothing at all.
        db: the IS2Database to restore into. Defaults to the configured one.

    Returns:
        A RestoreResult.

    """
    if mode not in OVERLAP_MODES:
        msg = f"Unknown overlap mode {mode!r}; choose from {', '.join(OVERLAP_MODES)}."
        raise ValueError(msg)
    result = RestoreResult()
    if mode == "cancel":
        return result

    db = db or ivert.icesat2_database_v2.IS2Database()
    os.makedirs(db.granules_dir, exist_ok=True)
    os.makedirs(db.landmask_dir, exist_ok=True)

    # Unpack beside the database, so moving files in is a rename, not a copy.
    staging = tempfile.mkdtemp(
        prefix=".ivert_restore_",
        dir=os.path.dirname(os.path.abspath(db.granules_dir)),
    )
    try:
        with zipfile.ZipFile(plan.archive) as zf:
            _extract_all(zf, staging)
        _restore_granules(plan, mode, db, staging, result)
        _restore_landmasks(plan, mode, db, staging, result)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    gdf = db.create_new_database(populate=True, overwrite=True)
    result.n_index_records = 0 if gdf is None else len(gdf)
    return result


def _restore_granules(plan, mode, db, staging, result) -> None:
    src_dir = os.path.join(staging, GRANULES_ARCDIR)
    imported = plan.manifest["granules"]
    present = set(os.listdir(db.granules_dir))

    if mode == "keep":
        existing = _merged(db.unique_bboxes(data_or_query="query") or [])
        for g in _progress(imported, desc="Importing granules"):
            src = os.path.join(src_dir, g["filename"])
            if g["filename"] in present:
                result.granules_skipped += 1
                continue
            cuboid = tuple(g["query_bbox"])
            # A file whose photons all lie outside the existing area duplicates
            # nothing, so it goes in whole rather than cut into pieces.
            if not _touches_any(_data_cuboid(g["data_bbox"]), existing):
                pieces = [cuboid]
            else:
                pieces = _subtract_all(cuboid, existing)
            if pieces == [cuboid]:
                _move(src, os.path.join(db.granules_dir, g["filename"]))
                result.granules_added += 1
            elif pieces:
                db.clip_granule_file(src, pieces, db.granules_dir)
                result.granules_clipped += 1
            else:
                result.granules_skipped += 1
        return

    # 'replace' first cuts the imported area out of the existing files, into
    # staging, so that nothing existing is removed before its remainder is written.
    to_remove = []
    remainder_dir = os.path.join(staging, ".existing_remainders")
    if mode == "replace":
        imported_names = {g["filename"] for g in imported}
        imported_cuboids = _merged(tuple(g["query_bbox"]) for g in imported)
        gdf = db.open_gdf()
        for _, row in _progress(
            gdf.iterrows() if gdf is not None else (),
            total=0 if gdf is None else len(gdf),
            desc="Cutting existing granules",
        ):
            if row["filename"] in imported_names:
                continue  # replaced outright by the imported file of the same name
            cuboid = _query_cuboid(row)
            # Only files with photons in the imported area need cutting.
            data_bbox = [row[c] for c in _DATA_COLS]
            if not _touches_any(_data_cuboid(data_bbox), imported_cuboids):
                continue
            src = os.path.join(db.granules_dir, row["filename"])
            pieces = _subtract_all(cuboid, imported_cuboids)
            if pieces:
                db.clip_granule_file(src, pieces, remainder_dir)
                result.existing_clipped += 1
            to_remove.append(src)

    # 'all' and 'replace' take every imported file as it is.
    for g in imported:
        _move(
            os.path.join(src_dir, g["filename"]),
            os.path.join(db.granules_dir, g["filename"]),
        )
        result.granules_added += 1

    if os.path.isdir(remainder_dir):
        for name in os.listdir(remainder_dir):
            _move(
                os.path.join(remainder_dir, name),
                os.path.join(db.granules_dir, name),
            )
    for path in to_remove:
        os.remove(path)
        result.existing_removed += 1


def _restore_landmasks(plan, mode, db, staging, result) -> None:
    src_dir = os.path.join(staging, LANDMASKS_ARCDIR)
    imported = plan.manifest["landmasks"]
    existing = ivert.landmask.stored_landmasks(db.landmask_dir)
    existing_names = {os.path.basename(p) for p in existing}

    if mode == "keep":
        rects = list(existing.values())
        for lm in _progress(imported, desc="Importing landmasks"):
            src = os.path.join(src_dir, lm["filename"])
            if lm["filename"] in existing_names:
                continue
            rect = tuple(lm["bbox"])
            pieces = _subtract_all_2d(rect, rects)
            if pieces == [rect]:
                _move(src, os.path.join(db.landmask_dir, lm["filename"]))
            else:
                for piece in pieces:
                    ivert.landmask.clip_stored_landmask(src, piece, db.landmask_dir)
            if pieces:
                result.landmasks_added += 1
        return

    to_remove = []
    remainder_dir = os.path.join(staging, ".landmask_remainders")
    if mode == "replace":
        imported_names = {lm["filename"] for lm in imported}
        imported_rects = [tuple(lm["bbox"]) for lm in imported]
        for path, rect in _progress(
            existing.items(),
            desc="Cutting existing landmasks",
        ):
            if os.path.basename(path) in imported_names:
                continue
            if not any(_intersection(rect, r) for r in imported_rects):
                continue
            for piece in _subtract_all_2d(rect, imported_rects):
                ivert.landmask.clip_stored_landmask(path, piece, remainder_dir)
            to_remove.append(path)

    for lm in imported:
        _move(
            os.path.join(src_dir, lm["filename"]),
            os.path.join(db.landmask_dir, lm["filename"]),
        )
        result.landmasks_added += 1
    if os.path.isdir(remainder_dir):
        for name in os.listdir(remainder_dir):
            _move(
                os.path.join(remainder_dir, name),
                os.path.join(db.landmask_dir, name),
            )
    for path in to_remove:
        os.remove(path)
