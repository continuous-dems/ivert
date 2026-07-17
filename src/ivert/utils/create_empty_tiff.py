# Quick utility to creatwe an empty .tif file to use for the IVERT test utility.


import ivert.utils.configfile as configfile

import os
import rasterio


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

        print(f"Created {tiff_location}, {os.path.getsize(tiff_location)} bytes")

    else:
        print(f"{tiff_location} already exists.")


if __name__ == "__main__":
    create_empty_tiff()
