"""Tests for splitting a DEM into pieces, as validate_dem does after a memory error."""

import logging

import numpy as np
import pytest
import rasterio
import rasterio.transform

from ivert.utils import split_dem


def _small_dem(path, width=5, height=4):
    """Write a small GeoTIFF that doesn't split evenly in either direction."""
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=rasterio.transform.from_origin(-105.0, 40.0, 0.1, 0.1),
    ) as dst:
        dst.write(np.arange(width * height, dtype="float32").reshape(1, height, width))
    return str(path)


@pytest.mark.parametrize(("n", "factor"), [(10, 4), (11, 4), (7, 2), (100, 3), (5, 5)])
def test_pieces_cover_every_index_once(n, factor):
    """The old batching left more than one remainder batch for some sizes, e.g. 11 into 4."""
    batches = split_dem.evenly_split(n, factor)

    assert len(batches) == factor
    indices = [i for start, end in batches for i in range(start, end + 1)]
    assert indices == list(range(n))
    sizes = [end - start + 1 for start, end in batches]
    assert max(sizes) - min(sizes) <= 1


def test_more_pieces_than_indices_is_an_error():
    """Some pieces would be empty, and an empty raster window can't be written."""
    with pytest.raises(ValueError, match="Cannot split 3 indices into 4 pieces"):
        split_dem.evenly_split(3, 4)


def test_a_rerun_returns_the_pieces_already_written(tmp_path):
    """validate_dem needs all four pieces back, even those an earlier run left on disk."""
    dem = _small_dem(tmp_path / "dem.tif")
    first = split_dem.split([dem], factor=2, output_dir=str(tmp_path))

    second = split_dem.split([dem], factor=2, output_dir=str(tmp_path))

    assert len(first) == 4
    assert second == first


def test_written_pieces_log_no_error(tmp_path, caplog):
    """Every piece used to log "failed." right after "written."."""
    dem = _small_dem(tmp_path / "dem.tif")

    with caplog.at_level(logging.INFO, logger=split_dem.logger.name):
        split_dem.split([dem], factor=2, output_dir=str(tmp_path))

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
