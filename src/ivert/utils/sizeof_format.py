"""Code for returning human-readable string of file sizes (in bytes)."""

# Bytes in a kibibyte.
_KIB = 1024.0


def sizeof_fmt(num: float, suffix: str = "B", decimal_digits: int = 1) -> str:
    """Return a number of bytes in human-readable form ('1.5 KB')."""
    for unit in ["", "K", "M", "G", "T", "P", "E", "Z"]:
        # Compare the rounded value, so 1023.96 KB shows as "1.0 MB", not "1024.0 KB".
        if round(abs(num), decimal_digits) < _KIB:
            if unit == "":
                return f"{int(num)} {suffix}"
            return f"{num:.{decimal_digits}f} {unit}{suffix}"
        num /= _KIB
    return f"{num:.1f} Yi{suffix}"
