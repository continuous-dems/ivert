# Quick utility to creatwe an empty .tif file to use for the IVERT test utility.


import logging
import os

import rasterio

from ivert.utils import configfile

logger = logging.getLogger(__name__)


def create_empty_tiff():
    """Create an empty one-cell TIFF file for IVERT to use for testing."""
    ivert_config = configfile.Config()
    tiff_location = ivert_config.empty_tiff

    if not os.path.exists(tiff_location):
        with rasterio.open(
            tiff_location,
            "w",
            driver="GTiff",
            width=1,
            height=1,
            count=1,
            dtype="float32",
        ):
            pass

        logger.info(
            "Created %s, %s bytes",
            tiff_location,
            os.path.getsize(tiff_location),
        )

    else:
        logger.info("%s already exists.", tiff_location)


if __name__ == "__main__":
    create_empty_tiff()
