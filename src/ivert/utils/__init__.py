import sys

# Register this package under both 'utils' (direct-run style) and
# 'ivert_utils' (installed-package style) in sys.modules.
#
# When running 'python src/script.py', Python names this package 'utils'
# because src/ is on sys.path and utils/ is a subdirectory.
# When running via the installed 'ivert' CLI, it is named 'ivert_utils'.
#
# By aliasing both names to the same module object we prevent two separate
# copies of the same file from being loaded into the same process, which
# would cause subtle bugs (duplicate singletons, failed isinstance checks).
_self = sys.modules[__name__]
_alt = 'utils' if __name__ == 'ivert_utils' else 'ivert_utils'
sys.modules.setdefault(_alt, _self)
