"""Count physical CPU cores."""

import multiprocessing as mp

import psutil


def physical_cpu_count():
    """On this machine, get the number of physical cores.

    Not logical cores (when hyperthreading is available), but actual physical cores.
    Things such as multiprocessing.cpu_count often give us the logical cores, which
    means we'll spin off twice as many processes as really helps us when we're
    multiprocessing for performance. We want the physical cores.
    """
    # psutil.cpu_count(logical=False) is cross-platform and returns the number of
    # physical cores. It can return None on some platforms if it can't determine the
    # count, in which case fall back to the logical core count (better than nothing).
    num_physical = psutil.cpu_count(logical=False)
    if num_physical is None:
        return mp.cpu_count()
    return num_physical
