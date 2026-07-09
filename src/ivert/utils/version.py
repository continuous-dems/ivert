import os
import sys

__version__ = None


def current_version():
    # If the version has already been read from the file, just return it.
    global __version__
    if __version__ is not None:
        return __version__

    # in the codes files.
    VFILE1 = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "VERSION"))
    # In the package files, after installing.
    VFILE2 = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "ivert_data", "VERSION"))

    if vars(sys.modules[__name__])['__package__'] == 'ivert_utils' and os.path.exists(VFILE2):
        file_to_use = VFILE2
    elif os.path.exists(VFILE1):
        file_to_use = VFILE1
    else:
        raise FileNotFoundError("Could not find 'VERSION' file.")

    with open(file_to_use, 'r') as fobj:
        v = fobj.read().strip()
        return v


# Define the __version__ variable in this module.
__version__ = current_version()

if __name__ == "__main__":
    print(__version__)
