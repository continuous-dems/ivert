"""Cross-platform utility for adding colors to command-line output.

The color codes below are standard ANSI escape sequences. They render natively
on Linux and macOS terminals. On Windows they require virtual-terminal (VT)
processing to be enabled on the console, which is off by default; colorama's
``just_fix_windows_console()`` turns it on for modern Windows terminals
(Windows 10+ / Windows Terminal) and is a harmless no-op on other platforms.

That call only sets the console-mode flag -- it does not wrap or replace
``sys.stdout`` -- so it does not interfere with the stdout redirection in
``loggerproc.Logger`` (which strips these codes when writing to log files).

colorama is a hard dependency on Windows (pulled in via click) and is not needed
on Linux/macOS, so a missing import there is fine and simply left as a no-op.

Original ANSI codes from:
https://svn.blender.org/svnroot/bf-blender/trunk/blender/build_files/scons/tools/bcolors.py
"""

try:
    import colorama

    # Enable ANSI/VT processing on Windows consoles. No-op on Linux/macOS.
    colorama.just_fix_windows_console()
except (ImportError, AttributeError):
    # colorama absent (Linux/macOS) or too old to expose just_fix_windows_console.
    # ANSI codes render natively on POSIX terminals, so nothing else is needed.
    pass


class Bcolors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"

    def disable(self):
        """Disable color output."""
        self.HEADER = ""
        self.OKBLUE = ""
        self.OKGREEN = ""
        self.WARNING = ""
        self.FAIL = ""
        self.ENDC = ""
        self.BOLD = ""
        self.ITALIC = ""
        self.UNDERLINE = ""
