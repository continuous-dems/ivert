# Quick utility to creatwe an empty .tif file to use for the IVERT test utility.


import ivert.utils.configfile as configfile

import os
from osgeo import gdal


def create_empty_tiff():
    """Create an empty one-cell TIFF file for IVERT to use for testing."""
    ivert_config = configfile.Config()
    tiff_location = ivert_config.empty_tiff

    if not os.path.exists(tiff_location):
        gdal.GetDriverByName("GTiff").Create(tiff_location, 1, 1, 1, gdal.GDT_Float32)

        print(f"Created {tiff_location}, {os.path.getsize(tiff_location)} bytes")

    else:
        print(f"{tiff_location} already exists.")


if __name__ == "__main__":
    create_empty_tiff()
