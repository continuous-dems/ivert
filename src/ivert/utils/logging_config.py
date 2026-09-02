"""Shared logging configuration for IVERT.

IVERT emits its progress and diagnostic messages through the standard :mod:`logging`
module. This module is the single place where verbosity names are mapped to logging
levels and where handlers get installed, so that the CLI and the worker sub-processes
stay in agreement about what gets shown.

Two entry points matter:

``configure_logging``
    Called by the CLI (:mod:`ivert.cli`) to set up the root logger for the run.

``configure_worker_logging``
    Called at the top of a function that is the target of a spawned sub-process.
    ``validate_dem`` and ``validate_dem_collection`` set the multiprocessing start
    method to "spawn", and a spawned child starts with a fresh, unconfigured logging
    module: it does *not* inherit the parent's handlers or level. Without this call the
    child's messages would fall through to logging's last-resort handler, which drops
    everything below WARNING -- silently losing nearly all of the validation output.
"""

import logging

# Verbosity names accepted on the command line and in ivert_defaults.ini, mapped to the
# logging level each one selects.
VERBOSITY_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

DEFAULT_VERBOSITY = "info"


class LevelPrefixFormatter(logging.Formatter):
    """Prefix a message with its level name only when the level warrants it.

    IVERT's normal progress output is plain prose that used to be written with print(),
    and prefixing every line of it with "INFO: " would be noise. Warnings and errors,
    though, were written with an explicit "WARNING: " marker in the text, so the prefix
    has to come from somewhere once that marker moves into the logging level. This
    formatter therefore labels WARNING and above, and leaves INFO bare.

    At debug verbosity every record is labelled, since knowing which messages are debug
    output is the point of asking for it.
    """

    def __init__(self, *, always_prefix: bool = False) -> None:
        super().__init__("%(message)s")
        self.always_prefix = always_prefix

    def format(self, record: logging.LogRecord) -> str:
        """Render the record, prefixing the level name where appropriate."""
        message = super().format(record)
        if self.always_prefix or record.levelno >= logging.WARNING:
            return f"{record.levelname}: {message}"
        return message


def level_for(verbosity: str) -> int:
    """Return the logging level for a verbosity name.

    Raises:
        KeyError: if the verbosity name is not one of VERBOSITY_LEVELS.

    """
    return VERBOSITY_LEVELS[str(verbosity).strip().lower()]


def configure_logging(verbosity: str) -> int:
    """Configure the root logger from a verbosity name, and return the level chosen.

    Raises:
        KeyError: if the verbosity name is not one of VERBOSITY_LEVELS.

    """
    level = level_for(verbosity)
    _install_handler(level)
    return level


def configure_worker_logging(level: int) -> None:
    """Configure logging inside a spawned sub-process.

    Call this as the first statement of any function used as a multiprocessing target,
    passing the level the parent process was running at. ``level`` is a numeric logging
    level (e.g. ``logging.INFO``) rather than a verbosity name because it is read from
    the parent's live logger, and numbers survive pickling to the child unambiguously.
    """
    _install_handler(level)


def _install_handler(level: int) -> None:
    """Install a stderr handler at the given level, replacing any existing handlers.

    Rebuilding the handler matters in a sub-process launched through
    :class:`ivert.utils.loggerproc.LoggerProc`: that class reassigns ``sys.stdout`` and
    ``sys.stderr`` to a file-backed logger *before* calling its target. A StreamHandler
    binds whatever ``sys.stderr`` refers to at the moment it is constructed, so building
    the handler here -- after the redirect, and discarding any handler built before it --
    is what keeps job output flowing into the logfile rather than to the real terminal.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(LevelPrefixFormatter(always_prefix=level <= logging.DEBUG))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger().setLevel(level)
