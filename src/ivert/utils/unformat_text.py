
import re

from pandas.core.array_algos.replace import compare_or_regex_search

# 7-bit C1 ANSI sequences
ansi_escape = re.compile(r'''
    \x1B  # ESC
    (?:   # 7-bit C1 Fe (except CSI)
        [@-Z\\-_]
    |     # or [ for CSI, followed by a control sequence
        \[
        [0-?]*  # Parameter bytes
        [ -/]*  # Intermediate bytes
        [@-~]   # Final byte
    )
''', re.VERBOSE)

# Matches any lines that end in a carriage return.
# Unless they're immediately follwed by a newline character, at which point that'll be displayed to the screen.
cr_line_regex = re.compile(r'(((?<=[\n\r]).*?\r)|(^.*?\r))(?!\n)')


def unformat(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return ansi_escape.sub("", text)


def delete_cr_lines(text: str) -> str:
    """Remove carriage returns and the lines preceding them if they were overwritten by new lines.

    And then get rid of any remaining carriage returns."""
    return cr_line_regex.sub("", text).replace("\r", "")


def unformat_and_delete_cr_lines (text: str) -> str:
    """Remove ANSI escape codes and carriage returns and the lines preceding them from text."""
    return delete_cr_lines(unformat(text))

