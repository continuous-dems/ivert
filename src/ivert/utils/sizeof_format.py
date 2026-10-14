"""Code for returning human-readable string of file sizes (in bytes)."""

# Bytes in a kibibyte.
_KIB = 1024.0


def sizeof_fmt(num: float, suffix: str = "B", decimal_digits: int = 1) -> str:
    """Return a number of bytes in human-readable form ('1.5 KB')."""
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
