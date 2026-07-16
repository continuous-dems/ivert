"""Code for managing individual tiles of ICESat-2 data."""

# import geopandas
import numexpr
import numpy
import os
import pandas
import pyproj
import typing

import icesat2_query


class ICESat2_Tile:
    """IS THIS CLASS EVEN NEEDED NOW?"""

    bbox_order = "(xmin, xmax, ymin, ymax, yyyymmdd_min, yyyymmdd_max)"
    default_format = "feather"

    def __init__(
        self,
        filename: typing.Union[str, None] = None,
        directory: typing.Union[str, None] = None,
        crs: typing.Union[str, pyproj.crs.crs, None] = "EPSG:4326",  # WGS84 coordinates
        vdatum: typing.Union[
            None, str, pyproj.crs.crs
        ] = "EPSG:3855",  # EGM2008 geoid vertical datum
        bbox: typing.Union[list[float], tuple[float], None] = None,
    ):
        # If given a filename, list that as the filename, populate the rest from the file attributes once the file is read.
        # If given a directory, append that to the filename listed (if the file is not an absolute path)
        # If not given a filename, create one from the other parameters.
        # Other parameters must include:
        # - crs, vdatum (if not defined in crs)
        # - bbox, OR min-max pairs for all 3 axes (one or the other)
        self.numphotons = None
        self.crs = crs
        self.vdatum = vdatum
        self.atl_version = None
        self.df = None
        self.bbox = bbox

        if (filename is None) and (bbox is None):
            raise ValueError(
                "Either 'filename' or 'bbox' (xmin, ymin, xmax, ymax, yyyymmdd_min, yyyymmdd_max) must be specified upon creation of ICESat2_Tile object."
            )

        if filename is None:
            # Create the filename from the bounding box.
            filename = self.generate_filename(self.bbox)

        # If the directory is specified and is not the same directory as what's in the filename,
        # then append the two for the full file path.
        directory = "" if directory is None else directory
        filename = "" if filename is None else filename

        # Make sure directory and filename use full normalized path, without shortcuts for homedir, etc.
        directory = os.path.normpath(os.path.expanduser(directory))
        filename = os.path.normpath(os.path.expanduser(filename))

        if directory == "" and filename == "":
            raise ValueError(
                "Either filename or directory (or both) must be specified upon creation of ICESat2_Tile object."
            )

        # The filename might have been given with the full directory appended to it. If so, just use the filename.
        elif os.path.normpath(os.path.dirname(filename)) != os.path.normpath(directory):
            self.filepath = os.path.normpath(os.path.join(directory, filename))
        else:
            self.filepath = os.path.normpath(filename)

    @classmethod
    def from_file(cls, filename, directory=None):
        return cls(filename, directory, crs=None, vdatum=None, bbox=None)

    @classmethod
    def from_bbox(
        cls,
        bbox: typing.Tuple,
        directory: str,
        crs: typing.Union[str, pyproj.CRS],
        vdatum: typing.Union[None, str, pyproj.CRS] = None,
    ):
        bbox = cls.validate_bbox(bbox)
        crs, vdatum = icesat2_query.get_dem_horz_and_vert_reference_frame(
            bbox[0:4], crs, vdatum
        )
        crs = crs.srs
        vdatum = vdatum.srs
        filename = cls.generate_filename(bbox)
        return cls(filename, directory, crs, vdatum, bbox)

    @staticmethod
    def validate_bbox(bbox) -> tuple:
        if len(bbox) != 6:
            raise ValueError(
                "ICESat2_Tile bbox must be specified as a 6-tuple in format (xmin, xmax, ymin, ymax, yyyymmdd_min, yyyymmdd_max)"
            )

        try:
            xmin = float(bbox[0])
        except:
            raise ValueError(
                f"Minimum X-value '{bbox[0]}' must be an eligible floating point value."
            )

        try:
            xmax = float(bbox[1])
        except:
            raise ValueError(
                f"Maximum X-value '{bbox[1]}' must be an eligible floating point value."
            )

        try:
            ymin = float(bbox[2])
        except:
            raise ValueError(
                f"Minimum Y-value '{bbox[2]}' must be an eligible floating point value."
            )

        try:
            ymax = float(bbox[3])
        except:
            raise ValueError(
                f"Maximum Y-value '{bbox[3]}' must be an eligible floating point value."
            )

        try:
            tmin = int(bbox[4])
            assert 2018_01_01 <= tmin <= 2100_00_00
        except:
            raise ValueError(
                f"Minimum time-value '{bbox[4]}' must be an eligible YYYYMMDD integer in the ICESat2 period."
            )

        try:
            tmax = int(bbox[5])
            assert 2018_01_01 <= tmax <= 2100_00_00
        except:
            raise ValueError(
                f"Maximum time-value '{bbox[5]}' must be an eligible YYYYMMDD integer in the ICESat2 period."
            )

        if xmin >= xmax:
            raise ValueError(f"X-min {xmin} must be less than X-max {xmax}.")

        if ymin >= ymax:
            raise ValueError(f"Y-min {ymin} must be less than Y-max {ymax}.")

        if tmin > tmax:
            raise ValueError(
                f"Time-min {tmin} must be less or equal to Time-max {tmax}."
            )

        return xmin, xmax, ymin, ymax, tmin, tmax

    @staticmethod
    def get_file_suffix(format: typing.Union[str, None] = None) -> str:
        """From the "format" class field, generate the correct file suffix."""
        # If no format is chosen, use the ICESat-2 Tile default format.
        fmt = (
            (ICESat2_Tile.default_format if format is None else format).strip().lower()
        )

        if fmt == "feather":
            return "feather"
        elif fmt in ("hdf", "hdf5"):
            return "hdf"
        else:
            raise ValueError(f"Un-handled file format '{format}'.")

    @staticmethod
    def generate_filename(bbox: typing.Tuple) -> str:
        """Given a 3D bounding box (x,y,t), generate a filename for this tile that reflects the bounding box."""
        bbox = ICESat2_Tile.validate_bbox(bbox)
        xmin, xmax, ymin, ymax, tmin, tmax = bbox
        digits = 5
        # Make sure if we have very-small tiles, we don't accidentally make the limits not printed long enough.
        # This'll extend the number of digits until the last 2 printed digits are different within each (min, max) pair
        while (xmax - xmin) * (10**digits) < 10:
            digits += 1
        while (ymax - ymin) * (10**digits) < 10:
            digits += 1

        fname = (
            f"tile_{'W' if xmin < 0 else 'E'}{abs(xmin):0.{digits}f}_"
            + f"{'W' if xmax < 0 else 'E'}{abs(xmax):0.{digits}f}_"
            + f"{'S' if ymin < 0 else 'N'}{abs(ymin):0.{digits}f}_"
            + f"{'S' if ymax < 0 else 'N'}{abs(ymax):0.{digits}f}_"
            + f"{tmin}_{tmax}.{ICESat2_Tile.get_file_suffix()}"
        )

        return fname

    def read_tile(
        self, force_reread: bool = False, verbose: bool = True
    ) -> typing.Union[pandas.DataFrame, None]:
        """Read the file, and return the geodataframe.
        If the file doesn't yet exist, return None."""
        assert self.filepath is not None

        if self.df is None or force_reread:
            if os.path.exists(self.filepath):
                if verbose:
                    print(
                        f"Reading {os.path.basename(self.filepath)}...",
                        end=" ",
                        flush=True,
                    )

                ext = os.path.splitext(self.filepath)[1].lower()
                if ext == ".feather":
                    self.df = pandas.read_feather(self.filepath)
                elif ext == ".hdf":
                    self.df = pandas.read_hdf(self.filepath)
                else:
                    # If we can't tell the format type, try HDF. If that breaks, raise an error.
                    try:
                        self.df = pandas.read_hdf(self.filepath)
                    except:
                        print()
                        raise ValueError(
                            f"File {os.path.basename(self.filepath)} has unhandled file type '{ext}'"
                        )

                if verbose:
                    print(len(self.df), "records.", flush=True)

        if self.df is not None:
            # Populate the metadata class variables.
            self.crs = self.df.attrs["crs"]
            self.vdatum = self.df.attrs["vdatum"]
            self.bbox = self.df.attrs["bbox"]

        return self.df

    def create_tile(
        self,
        list_of_granules: list[str],
        format: typing.Union[str, None] = None,
        overwrite: bool = False,
        verbose: bool = True,
    ):
        """Given the search parameters listed, create a tile file from the list of icesat-2 granules given, that
        fully spans the bounding box listed. Save the file and return the gdf."""

        list_of_dfs = []
        for granule_fn in list_of_granules:
            # TODO: Loop over dataframes, read into list of dataframes
            pass

        blank_gdf = self.create_blank_gdf()

        # TODO: Format dataframes to be in exact same format as blank_gdf, or perhaps put them into dicts.
        self.df = pandas.concat([blank_gdf, *list_of_dfs], ignore_index=True)
        self.df.attrs = blank_gdf.attrs

        self.write_tile(format=format)

    def write_tile(
        self, format: str = "feather", overwrite: bool = False, verbose: bool = True
    ):
        if self.df is None:
            raise ValueError(
                "IVERT geodataframe must be populated before the file can be written. Call the "
                "ICESat2_Tile.create_tile() method before calling write_tile()."
            )

        if self.filepath is None:
            raise ValueError(
                "ICESat2_Tile filename must be populated before the file can be written."
            )

        if format is None:
            format = ICESat2_Tile.default_format
        format = format.strip().lower()

        if os.path.exists(self.filepath):
            if overwrite:
                if verbose:
                    print("Deleting previous", os.path.basename(self.filepath))
                os.remove(self.filepath)

            else:
                # not overwrite
                if verbose:
                    print(os.path.basename(self.filepath), "already exists.")
                return

        suffix = self.get_file_suffix(format)
        if format not in ("feather", "hdf", "hdf5"):
            raise ValueError(f"Unhandled file format '{format}' encountered. Aborting")

        if verbose:
            print(
                f"Writing {os.path.basename(self.filepath)} with {len(self.df)} records...",
                end="",
                flush=True,
            )

        if format == "feather":
            self.df.to_feather(self.filepath)
        elif format in ("hdf", "hdf5"):
            self.df.to_hdf(self.filepath)

        if os.path.exists(self.filepath):
            if verbose:
                print(" Done.")
        else:
            raise FileNotFoundError(self.filepath, "not created.")

        return

    def create_blank_gdf(self):
        """Create a blank GDF template with zero records in it. This function defines the fields listed."""
        # Create a single line of data to fill in as the data for the GDF.
        one_line_data = {
            "x": numpy.array(
                [
                    0.0,
                ],
                dtype=float,
            ),
            "y": numpy.array(
                [
                    0.0,
                ],
                dtype=float,
            ),
            "z": numpy.array(
                [
                    0.0,
                ],
                dtype=float,
            ),
            "yyyymmdd": numpy.array(
                [
                    20180101,
                ],
                dtype=numpy.uint32,
            ),
            "delta_time": numpy.array(
                [
                    0.0,
                ],
                dtype=float,
            ),
            "class_code": numpy.array(
                [
                    0,
                ],
                dtype=numpy.uint8,
            ),
            "beam": numpy.array(
                [
                    0,
                ],
                dtype=numpy.uint8,
            ),
            "granule_id1": numpy.array(
                [
                    0,
                ],
                dtype=numpy.uint64,
            ),
            "granule_id2": numpy.array(
                [
                    0,
                ],
                dtype=numpy.uint64,
            ),
            "x_unc": numpy.array(
                [
                    0.0,
                ],
                dtype=float,
            ),
            "y_unc": numpy.array(
                [
                    0.0,
                ],
                dtype=float,
            ),
            "z_unc": numpy.array(
                [
                    0.0,
                ],
                dtype=float,
            ),
            "quality_ph": numpy.array(
                [
                    0.0,
                ],
                dtype=float,
            ),
        }

        # Create a geodatabase from this data
        df = pandas.DataFrame(data=one_line_data)

        # Fill in metadata as table attributes
        df.attrs["crs"] = self.crs
        df.attrs["vdatum"] = self.vdatum
        df.attrs["atl_version"] = self.atl_version
        df.attrs["bbox"] = self.bbox

        # Remove the single line of data to make it an empty dataframe.
        df.drop(index=0, inplace=True)

        return df

    def query(self, bbox: typing.Tuple, verbose: bool = True):
        """Return a dataframe of all photon records within the (xmin, xmax, ymin, ymax, tmin, tmax) bounding box."""
        if len(bbox) == 4:
            # If the bounding box is just a 4-tuple (xmin, xmax, ymin, ymax), then assume it's looking for all date ranges.
            # Choose any dates between year 0 to year 3000.
            b_xmin, b_xmax, b_ymin, b_ymax = bbox
            b_tmin = 0
            b_tmax = int(3e8)
        elif len(bbox) == 6:
            b_xmin, b_xmax, b_ymin, b_ymax, b_tmin, b_tmax = bbox
        else:
            raise ValueError(
                "Bounding box must be a 6-value tuple or list of (xmin, xmax, ymin, ymax, tmin, tmax), or a 4-tuple without the time bounds."
            )

        if self.bbox is None or self.df is None:
            self.read_tile(verbose=verbose)
        assert self.bbox is not None and self.df is not None

        t_xmin, t_xmax, t_ymin, t_ymax, t_tmin, t_tmax = self.bbox

        # If the bounds are entirely outside the bounds of this tile's box, then just return a blank df and move along.
        if (
            (b_xmax <= t_xmin)
            or (b_xmin >= t_xmax)
            or (b_ymax <= t_ymin)
            or (b_ymin >= t_ymax)
            or (b_tmax < t_tmin)
            or (b_tmin > t_tmax)
        ):
            return self.create_blank_gdf()

        # If the bounds entirely enclose this tile's box, then just return the entire dataframe and move on,
        # no need to subset it.
        if (
            (b_xmax >= t_xmax)
            and (b_xmin <= t_xmin)
            and (b_ymax >= t_ymax)
            and (b_ymin <= t_ymin)
            and (b_tmax >= t_tmax)
            and (b_tmin >= t_tmax)
        ):
            return self.df

        # Otherwise, subset the dataset using numexpr (much faster than a multi-step boolean query)
        # Build out the query string using only the conditions we need, for limits that intersect the current tile's
        # bounding box. This will save time.
        query_subset_strings = []
        if t_xmin < b_xmin < t_xmax:
            query_subset_strings.append("(x >= b_xmin)")
        if t_xmin < b_xmax < t_xmax:
            query_subset_strings.append("(x < b_xmax)")
        if t_ymin < b_ymin < t_ymax:
            query_subset_strings.append("(y >= b_ymin)")
        if t_ymin < b_ymax < t_ymax:
            query_subset_strings.append("(y < b_ymax)")
        if t_tmin < b_tmin <= t_tmax:
            query_subset_strings.append("(yyyymmdd >= b_tmin)")
        if t_tmin <= b_tmax < t_tmax:
            query_subset_strings.append("(yyyymmdd <= b_tmax)")

        # If the logic was correct, there should be at least one search string in here.
        # Otherwise it should have returned an empty DF in the previous empty search criteria.
        assert len(query_subset_strings) > 0

        # Join them all together with 'and' statements
        query_string_total = " and ".join(query_subset_strings)
        subset_mask = numexpr.evaluate(
            query_string_total,
            local_dict={
                "x": self.df["x"],
                "y": self.df["y"],
                "yyyymmdd": self.df["yyyymmdd"],
                "b_xmin": b_xmin,
                "b_xmax": b_xmax,
                "b_ymin": b_ymin,
                "b_ymax": b_ymax,
                "b_tmin": b_tmin,
                "b_tmax": b_tmax,
            },
        )

        return self.df[subset_mask]


if __name__ == "__main__":
    tile = ICESat2_Tile.from_bbox(
        directory="~/.ivert/icesat2/",
        crs="EPSG:4326",  # WGS84
        vdatum="EPSG:3855",  # EGM2008
        bbox=(-124, -123.75, 45, 45.25, 2025_09_01, 2026_04_01),
    )

    df = tile.create_blank_gdf()
    tile.df = df
    print(df)
    print(df.attrs)
    print(tile.filepath)

    tile.write_tile()
    tile.df = None
    tile.read_tile(force_reread=True)

    df = tile.df
    print(df)
    print(df.attrs)
    print(tile.filepath)
