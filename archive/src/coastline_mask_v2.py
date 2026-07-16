import numpy
import os
import typing

from cudem import regions
from cudem.fetches import osm, bingbfp

# Use both OpenStreetMap as well as Bing Building Footprints (BFP) to create polygon vector masks.


class VectorMask:
    """Base class for generating vector masks of various surface types."""

    def __init__(
        self,
        download_directory: str,
        bbox: typing.Union[list, tuple, numpy.ndarray],
        cache_directory=None,
    ):
        if not hasattr(self, "default_file_basename"):
            raise NotImplementedError(
                f"Class {self.__class__.__name__} must define 'default_file_basename' before calling superclass constructor."
            )

        self.download_directory = os.path.normpath(
            os.path.expanduser(download_directory)
        )
        if cache_directory is None:
            self.cache_directory = self.download_directory
        else:
            self.cache_directory = os.path.normpath(os.path.expanduser(cache_directory))

        self.bbox = list(bbox)[0:4]
        # These fields should be filled in by the sub-class constructor
        self.filename = None
        self.default_filename = self.create_default_filename()

    def download(
        self,
        overwrite: bool = False,
        create_download_dir_if_not_exists: bool = True,
        max_attempts=10,
        verbose: bool = True,
    ):
        if self.filename is None:
            self.create_default_filename()

        if create_download_dir_if_not_exists and not os.path.exists(
            self.download_directory
        ):
            os.makedirs(self.download_directory)

        if (not overwrite) and os.path.exists(self.filename):
            return self.filename

        fetches_obj = self.call_fetches_obj(max_attempts=max_attempts, verbose=verbose)
        # Save the geometries to a file.
        fetches_obj(out_fn=self.filename, return_geom=False, overwrite=overwrite)

        return self.filename

    def call_fetches_obj(self, max_attempts=None, verbose=None):
        raise NotImplementedError(
            "Base class 'call_fetches_obj' method must be superceded in sub-class."
        )

    def open(self, filename_if_not_default=None):
        if filename_if_not_default is not None:
            self.filename = filename_if_not_default

        if self.filename is None:
            self.filename = self.default_filename

        assert self.filename is not None

        # TODO: Finish opening the geopackage file.

    def create_default_filename(self):
        xmin, xmax, ymin, ymax = self.bbox
        fname = os.path.join(
            self.download_directory,
            f"{self.default_file_basename}"
            f"_{'W' if xmin < 0 else 'E'}{abs(xmin):0.4f}"
            f"_{'W' if xmax < 0 else 'E'}{abs(xmax):0.4f}"
            f"_{'S' if ymin < 0 else 'N'}{abs(ymin):0.4f}"
            f"_{'S' if ymax < 0 else 'N'}{abs(ymax):0.4f}.gpkg",
        )

        self.default_filename = fname
        self.filename = fname


class BingBFPMask(VectorMask):
    """Generates a mask of building footprints (BFPs) from Bing BFP global maps."""

    def __init__(
        self, download_directory: str, bbox: typing.Union[list, tuple, numpy.ndarray]
    ):
        # Initialized the base class
        self.default_file_basename = "bingbfp"
        super().__init__(download_directory, bbox)

    def call_fetches_obj(self, max_attempts=10, verbose: bool = True):
        # Download the Bing datasets.
        bing_geometries = bingbfp.bingBuildings(
            region=regions.Region().from_list(self.bbox),
            verbose=verbose,
            attempts=max_attempts,
            cache_dir=self.cache_directory,
        )

        return bing_geometries


class OSMOceanMask(VectorMask):
    """Generates a mask of offshore areas over the ocean or large inland seas, from OpenStreetMap."""

    def __init__(
        self, download_directory: str, bbox: typing.Union[list, tuple, numpy.ndarray]
    ):
        self.default_file_basename = "osm_coast"
        super().__init__(download_directory, bbox)

    def call_fetches_obj(self, max_attempts=10, verbose: bool = True):
        # Download the Bing datasets.
        osm_coastline = osm.osmCoastline(
            region=regions.Region().from_list(self.bbox),
            chunks=True,
            verbose=verbose,
            attempts=max_attempts,
            cache_dir=self.cache_directory,
        )

        return osm_coastline


class OSMLakesMask(VectorMask):
    """Generates a mask of inland lake outlines, from OpenStreetMap."""

    def __init__(
        self, download_directory: str, bbox: typing.Union[list, tuple, numpy.ndarray]
    ):
        self.default_file_basename = "osm_lakes"

        super().__init__(download_directory, bbox)

    def call_fetches_obj(self, max_attempts=10, verbose: bool = True):
        # Download the Bing datasets.
        osm_lakes = osm.osmCoastline(
            region=regions.Region().from_list(self.bbox),
            chunks=True,
            verbose=verbose,
            attempts=max_attempts,
            cache_dir=self.cache_directory,
            q="lakes",
        )

        return osm_lakes


class OSMInlandWaterMask(VectorMask):
    """Generates a mask of inland water bodies (rivers and lakes combined), from OpenStreetMap.

    This will be a superset of OSMLakesMask. Typically either one or the other is called, exclusively, depending
    whether you want major rivers or not."""

    def __init__(
        self, download_directory: str, bbox: typing.Union[list, tuple, numpy.ndarray]
    ):
        self.default_file_basename = "osm_inland_water"

        super().__init__(download_directory, bbox)

    def call_fetches_obj(self, max_attempts=10, verbose: bool = True):
        # Download the Bing datasets.
        osm_water = osm.osmCoastline(
            region=regions.Region().from_list(self.bbox),
            chunks=True,
            verbose=verbose,
            attempts=max_attempts,
            cache_dir=self.cache_directory,
            q="water",
        )

        return osm_water


if __name__ == "__main__":
    bbox = [-123.01, -122.15, 48.50, 49.01]

    bing_test_dir = "~/.ivert/cache/test_osm"
    bing = BingBFPMask(bing_test_dir, bbox)
    bing.download(overwrite=False, verbose=True)

    osm_coast_dir = "~/.ivert/cache/test_osm_coast"
    osm_coast = OSMOceanMask(osm_coast_dir, bbox)
    osm_coast.download()

    osm_lakes_dir = "~/.ivert/cache/test_osm_lakes"
    osm_lakes = OSMLakesMask(osm_lakes_dir, bbox)
    osm_lakes.download()

    osm_inland_water_dir = "~/.ivert/cache/test_osm_inland_water"
    osm_water = OSMInlandWaterMask(osm_inland_water_dir, bbox)
    osm_water.download()
