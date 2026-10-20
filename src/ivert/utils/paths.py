"""Helpers for handling local filesystem paths."""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def absolute_path(path: str | os.PathLike) -> Path:
    """Return 'path' as an absolute path, the way os.path.abspath does.

    Relative paths are taken from the current directory, and "." and ".."
    components are collapsed lexically. Unlike Path.resolve(), symlinks are not
    followed, so paths keep the names users wrote. pathlib has no lexical
    normalization of its own, hence os.path.normpath.
    """
    return Path(os.path.normpath(Path(path).absolute()))


def make_output_folder(folder: str | os.PathLike) -> None:
    """Create an output folder, with a warning, if it does not exist yet."""
    folder = Path(folder)
    if folder.is_dir():
        return
    logger.warning("Output folder %s does not exist; creating it.", folder)
    folder.mkdir(parents=True, exist_ok=True)
