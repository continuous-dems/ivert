# Quick utility to creatwe an empty .tif file to use for the IVERT test utility.

import sys

if vars(sys.modules[__name__])['__package__'] == 'ivert_utils':
    # When this is built a setup.py package, it names the modules 'ivert' and 'ivert_utils'. This reflects that.
    import ivert_utils.configfile as configfile
else:
    # If running as a script, import this way.
    # Depends if we're importing from this directory or from the parent directory.
    try:
        import configfile
    except ModuleNotFoundError:
        import utils.configfile as configfile

import os
from osgeo import gdal


def create_empty_tiff():
    """Create an empty one-cell TIFF file for IVERT to use for testing."""
    ivert_config = configfile.Config()
    tiff_location = ivert_config.empty_tiff

    if not os.path.exists(tiff_location):
        ds = gdal.GetDriverByName('GTiff').Create(tiff_location, 1, 1, 1, gdal.GDT_Float32)

        ds = None

        print(f"Created {tiff_location}, {os.path.getsize(tiff_location)} bytes")

    else:
        print(f"{tiff_location} already exists.")


if __name__ == "__main__":
    create_empty_tiff()
