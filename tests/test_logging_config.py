"""Tests for how IVERT sets up logging: levels, message prefixes, and sub-processes."""

import logging
import multiprocessing

import pytest

from ivert.utils import logging_config

log = logging.getLogger("ivert.test_logging_config")


@pytest.fixture(autouse=True)
def restore_logging():
    """Put back the root and pyogrio loggers as they were before each test."""
    root = logging.getLogger()
    pyogrio = logging.getLogger("pyogrio")
    handlers, level, pyogrio_level = root.handlers[:], root.level, pyogrio.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    pyogrio.setLevel(pyogrio_level)


def _log_one_of_each(logger: logging.Logger = log) -> None:
    logger.debug("a debug line")
    logger.info("an info line")
    logger.warning("a warning line")
    logger.error("an error line")


# --- Verbosity names -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("verbosity", "level"),
    [
        ("debug", logging.DEBUG),
        ("info", logging.INFO),
        ("warning", logging.WARNING),
        ("error", logging.ERROR),
        (" Warning ", logging.WARNING),
        ("DEBUG", logging.DEBUG),
    ],
)
def test_verbosity_names_map_to_levels(verbosity, level):
    """Names are read case-insensitively, ignoring surrounding spaces."""
    assert logging_config.level_for(verbosity) == level


def test_an_unknown_verbosity_is_a_key_error():
    """A name that isn't one of the four is rejected, not treated as a default."""
    with pytest.raises(KeyError):
        logging_config.level_for("verbose")
    with pytest.raises(KeyError):
        logging_config.configure_logging("verbose")


# --- What gets shown -----------------------------------------------------------------


def test_info_verbosity_shows_progress_bare_and_labels_warnings(capsys):
    """Progress reads as plain text; warnings and errors carry their level."""
    assert logging_config.configure_logging("info") == logging.INFO
    _log_one_of_each()
    assert capsys.readouterr().err.splitlines() == [
        "an info line",
        "WARNING: a warning line",
        "ERROR: an error line",
    ]


def test_debug_verbosity_labels_every_line(capsys):
    """At debug verbosity every message says which level it is."""
    logging_config.configure_logging("debug")
    _log_one_of_each()
    assert capsys.readouterr().err.splitlines() == [
        "DEBUG: a debug line",
        "INFO: an info line",
        "WARNING: a warning line",
        "ERROR: an error line",
    ]


def test_warning_verbosity_hides_progress(capsys):
    """Only warnings and errors are shown."""
    logging_config.configure_logging("warning")
    _log_one_of_each()
    assert capsys.readouterr().err.splitlines() == [
        "WARNING: a warning line",
        "ERROR: an error line",
    ]


def test_configuring_again_replaces_the_handler(capsys):
    """A second call changes the level and doesn't print each message twice."""
    logging_config.configure_logging("info")
    logging_config.configure_logging("warning")
    _log_one_of_each()
    assert capsys.readouterr().err.splitlines() == [
        "WARNING: a warning line",
        "ERROR: an error line",
    ]


@pytest.mark.parametrize(
    ("verbosity", "shown"),
    [
        ("info", ["WARNING: a warning line", "ERROR: an error line"]),
        (
            "debug",
            [
                "DEBUG: a debug line",
                "INFO: an info line",
                "WARNING: a warning line",
                "ERROR: an error line",
            ],
        ),
    ],
)
def test_pyogrio_chatter_shows_only_at_debug(capsys, verbosity, shown):
    """Pyogrio's per-file INFO messages are hidden unless debugging; warnings stay."""
    logging_config.configure_logging(verbosity)
    _log_one_of_each(logging.getLogger("pyogrio"))
    assert capsys.readouterr().err.splitlines() == shown


# --- Spawned workers -----------------------------------------------------------------


def _worker(level: int) -> None:
    """Stand-in for a validation worker: set up logging, then log."""
    logging_config.configure_worker_logging(level)
    _log_one_of_each()


@pytest.mark.parametrize(
    ("level", "shown"),
    [
        (
            logging.INFO,
            ["an info line", "WARNING: a warning line", "ERROR: an error line"],
        ),
        (logging.ERROR, ["ERROR: an error line"]),
    ],
)
def test_a_spawned_worker_logs_at_the_level_it_is_given(capfd, level, shown):
    """A spawned child starts with no logging; the call gives it the parent's."""
    process = multiprocessing.get_context("spawn").Process(
        target=_worker,
        args=(level,),
    )
    process.start()
    process.join(timeout=60)
    assert process.exitcode == 0
    assert capfd.readouterr().err.splitlines() == shown


# --- Keeping IVERT's logging through fetchez ------------------------------------


def _take_over_logging() -> None:
    """Do what fetchez.get() does: replace the root handlers and raise the level."""
    logging.basicConfig(
        level=logging.WARNING,
        handlers=[logging.NullHandler()],
        force=True,
    )


def test_keep_root_logging_undoes_a_takeover(capsys):
    """After the block, IVERT's handler and level are back, and progress shows again."""
    logging_config.configure_logging("info")
    with logging_config.keep_root_logging():
        _take_over_logging()
        log.info("hidden while taken over")
    log.info("shown again")
    assert capsys.readouterr().err.splitlines() == ["shown again"]
    assert logging.getLogger().level == logging.INFO


def test_keep_root_logging_restores_after_an_error(capsys):
    """The handlers and level come back even if the block raises."""
    logging_config.configure_logging("info")

    def take_over_and_fail():
        with logging_config.keep_root_logging():
            _take_over_logging()
            raise RuntimeError

    with pytest.raises(RuntimeError):
        take_over_and_fail()
    log.info("shown again")
    assert capsys.readouterr().err.splitlines() == ["shown again"]
