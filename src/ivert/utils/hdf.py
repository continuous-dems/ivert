"""Reading IVERT's own dataframes back from HDF5 files."""

from pathlib import Path

import pandas as pd


def read_hdf_dataframe(path: str | Path) -> pd.DataFrame:
    """Read a DataFrame that IVERT wrote to an HDF5 file.

    pd.read_hdf() returns whatever object the file holds, which may be a Series.
    IVERT only ever writes DataFrames, so anything else means the wrong file.

    Raises:
        TypeError: if the file holds something other than a DataFrame.

    """
    data = pd.read_hdf(path)
    if not isinstance(data, pd.DataFrame):
        msg = f"{path} holds a {type(data).__name__}, not a DataFrame."
        raise TypeError(msg)
    return data
