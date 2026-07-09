import os
import sys

try:
    from ivert._version import __version__
except ImportError:
    # Fallback when using the package from source without installing
    # in editable mode with pip (nobody should do this):
    # <https://pip.pypa.io/en/stable/topics/local-project-installs/#editable-installs>
    import warnings

    warnings.warn(
        "Importing 'ivert' outside a proper installation."
        " It's highly recommended to install the package from a stable release or"
        " in editable mode.",
        stacklevel=2,
    )
    __version__ = "dev"

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
