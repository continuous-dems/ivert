import os
import sys

# Compatibility shim: add this package's own directory (src/) to sys.path so
# that scripts using direct-run style imports work whether invoked via the
# installed 'ivert' CLI command or run directly as 'python src/script.py'.
#
# Without this, bare imports like 'import utils.configfile' or
# 'import icesat2_database_v2' fail when called through the CLI, because
# Python only knows about the installed 'ivert' and 'ivert_utils' packages,
# not the raw src/ directory tree.
#
# This shim runs before any ivert submodule code executes (ivert/__init__.py
# is always imported first), so the path is available for all downstream imports.
_src_dir = os.path.dirname(os.path.abspath(__file__))
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

# VERSION file lives one level above src/ in the repo.
_vfile = os.path.abspath(os.path.join(_src_dir, "..", "VERSION"))
if not os.path.exists(_vfile):
    raise FileNotFoundError(f"Could not find 'VERSION' file at {_vfile}")

__version__ = None
with open(_vfile, 'r') as _f:
    __version__ = _f.read().strip()
