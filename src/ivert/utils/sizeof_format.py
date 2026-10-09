"""Code for returning human-readable string of file sizes (in bytes)."""

from pathlib import Path

# Bytes in a kibibyte.
_KIB = 1024.0


def sizeof_fmt(num, suffix="B", decimal_digits=1):
    """Resturn a filesize in human readable format.

    Can be a number of bytes, or a filename (str or Path)
    """
    if isinstance(num, (str, Path)) and Path(num).exists():
        num = Path(num).stat().st_size

    for unit in ["", "K", "M", "G", "T", "P", "E", "Z"]:
        if abs(num) < _KIB:
            return (
                f"{int(num)}"
                + (
                    ""
                    if (unit == "")
                    else ("{0:0." + f"{decimal_digits}" + "f}")
                    .format(num % 1)
                    .lstrip("0")
                )
                + f" {unit}{suffix}"
            )
        num /= _KIB
    return f"{num:.1f} Yi{suffix}"
