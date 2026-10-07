"""Helpers for handling local filesystem paths."""

import os
from pathlib import Path


def absolute_path(path: str | os.PathLike) -> Path:
    """Return 'path' as an absolute path, the way os.path.abspath does.

    Relative paths are taken from the current directory, and "." and ".."
    components are collapsed lexically. Unlike Path.resolve(), symlinks are not
    followed, so paths keep the names users wrote. pathlib has no lexical
    normalization of its own, hence os.path.normpath.
    """
    return Path(os.path.normpath(Path(path).absolute()))
