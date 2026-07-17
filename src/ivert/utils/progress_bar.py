#!/usr/bin/env python3
"""Created on Wed Oct 14 16:32:25 2020

@author: mmacferrin
"""

import os
import sys


def get_terminal_width(default=120):
    if not sys.stdout.isatty():
        return default

    try:
        return os.get_terminal_size().columns
    except OSError:
        return default


# Print iterations progress
def ProgressBar(
    iteration,
    total,
    prefix="",
    suffix="",
    decimals=1,
    width=get_terminal_width(default=120),
    fill="█",
    print_end="\r",
):
    """Call in a loop to create terminal progress bar
    @params:
        iteration   - Required  : current iteration (Int)
        total       - Required  : total iterations (Int)
        prefix      - Optional  : prefix string (Str)
        suffix      - Optional  : suffix string (Str)
        decimals    - Optional  : positive number of decimals in percent complete (Int)
        length      - Optional  : total character length (Int)
        fill        - Optional  : bar fill character (Str)
        printEnd    - Optional  : end character (e.g. "\r", "\r\n") (Str)
    """
    # Only run this if we are running in a terminal.
    if not sys.stdout.isatty():
        return None

    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    bar_length = width - (len(prefix) + 2 + 2 + len(percent) + 1 + len(suffix) + 2)
    filled_length = int(bar_length * iteration // total)
    bar = fill * filled_length + "-" * (bar_length - filled_length)
    outstr = f"{prefix} |{bar}| {percent}% {suffix}"
    print(outstr, end=print_end)
    # Print New Line on Complete
    if iteration == total:
        print()

    return outstr


# Sample Usage
# import time

# # A List of Items
# items = list(range(0, 57))
# l = len(items)

# # Initial call to print 0% progress
# ProgressBar(0, l, prefix = 'Progress:', suffix = 'Complete', length = 50)
# for i, item in enumerate(items):
#     # Do stuff...
#     time.sleep(0.1)
#     # Update Progress Bar
#     ProgressBar(i + 1, l, prefix = 'Progress:', suffix = 'Complete', length = 50)
