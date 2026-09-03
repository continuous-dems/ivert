"""Quick utility for splitting a large DEM into sub-segments to ease processing constraints."""

import glob
import itertools
import logging
import os

import click
import rasterio
import rasterio.windows

logger = logging.getLogger(__name__)


def contains_glob_flags(fname: str) -> bool:
    """Return True if a string contains any glob-style wildcard flags."""
    return ("*" in fname) or ("?" in fname) or ("[" in fname and "]" in fname)


def split(
    dem_name: str | list[str],
    factor: int = 2,
    output_dir: str | None = None,
) -> list[str]:
    """Split a DEM into sub-segments, each side split by a factor. 2 will create 4 sub-segments.

    Args:
        dem_name: The name of the DEM, with path, or a list of DEM names.
        factor: The factor by which to split the DEM.
        output_dir: The directory to which the sub-segments will be written. Defaults to the same directory as the DEM.

    Returns:
        list[str]: The names of the new DEM files.

    """
    if isinstance(dem_name, str):
        dem_name = [
            dem_name,
        ]

    if output_dir is None:
        output_dir = os.path.dirname(dem_name[0])

    outfiles = []
    infiles = []

    # Expand the number of files if there are glob flags.
    for dname in dem_name:
        if contains_glob_flags(dname):
            infiles.extend(glob.glob(dname))
        else:
            infiles.append(dname)

    # some bug here?
    for fname in infiles:
        with rasterio.open(fname) as src:
            y, x = src.height, src.width
            # How to cover all the DEM when it can't be split evenly?
            x_steps = evenly_split(x, factor)
            y_steps = evenly_split(y, factor)

            for xi, xb in zip(range(factor), x_steps, strict=True):
                for yj, yb in zip(range(factor), y_steps, strict=True):
                    assert len(xb) == 2
                    assert len(yb) == 2
                    fn_out = os.path.join(
                        output_dir,
                        f"{os.path.splitext(os.path.basename(fname))[0]}_{yj}.{xi}.tif",
                    )

                    if os.path.exists(fn_out):
                        logger.info("%s already exists.", fn_out)
                        continue

                    window = rasterio.windows.Window(
                        xb[0],
                        yb[0],
                        xb[1] - xb[0] + 1,
                        yb[1] - yb[0] + 1,
                    )
                    profile = src.profile.copy()
                    # Let GDAL pick its default tile size; the source's block layout
                    # (e.g. strips) is usually invalid for a tiled output.
                    profile.pop("blockxsize", None)
                    profile.pop("blockysize", None)
                    profile.update(
                        driver="GTiff",
                        width=window.width,
                        height=window.height,
                        transform=src.window_transform(window),
                        compress="deflate",
                        predictor=2,
                        tiled=True,
                    )
                    with rasterio.open(fn_out, "w", **profile) as dst:
                        dst.write(src.read(window=window))

                    if os.path.exists(fn_out):
                        logger.info("%s written.", fn_out)
                        outfiles.append(fn_out)
                    logger.error("%s failed.", fn_out)

    return outfiles


def evenly_split(n: int, factor: int) -> list:
    """Split n evenly into factor pieces by index.

    If it doesn't split evenly, add an extra to the last (remainder) pieces to make it as even as possible.

    Returns the starting and ending index of each sub-segment.
    """
    batches_all = list(itertools.batched(range(n), n // factor))
    if len(batches_all) == factor:
        batches = [(b[0], b[-1]) for b in batches_all]
    else:
        assert len(batches_all) == (factor + 1)
        batches = [(b[0], b[-1]) for b in batches_all[:-1]]
        extras = batches_all[-1]
        assert len(extras) < len(batches)

        m = len(extras)
        for i in range(m):
            j = -(i + 1)
            batches[j] = (batches[j][0] + (m + j), batches[j][1] + (m + j + 1))

        assert len(batches) == factor

    return batches


@click.command(help="Split a DEM into sub-segments, each side split by a factor.")
@click.argument("dem_name", nargs=-1, required=True)
@click.option(
    "-f",
    "--factor",
    type=int,
    default=2,
    help="The factor by which to split each side of the DEM. This will create f^2 files.",
)
@click.option(
    "-o",
    "--output_dir",
    type=str,
    default=None,
    help="The directory to which the sub-segments will be written. Default: will use the same directory as the input DEM.",
)
def main(dem_name, factor, output_dir):
    """Split a DEM into sub-segments, each side split by a factor.

    DEM_NAME is the name of the DEM file. May use bash-style glob flags (*.tif) to select multiple files.
    """
    split(list(dem_name), factor, output_dir)


if __name__ == "__main__":
    main()
