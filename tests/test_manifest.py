"""Tests for settling a manifest's options against the current 'ivert validate' options."""

import click
import pytest

from ivert import manifest

_TRACKED = ("band_num", "projection", "variable")


def _reconcile(
    manifest_options,
    interactive=False,
    current_values=None,
    command_line=(),
):
    return manifest.reconcile_options(
        "old.ini",
        "0.6.10",
        manifest_options,
        _TRACKED,
        "0.6.11",
        interactive=interactive,
        current_values=current_values,
        command_line=command_line,
    )


def test_missing_options_warn_only_when_they_fall_back_to_defaults(caplog):
    """A manifest from before an option existed still replays.

    The option takes its command-line value quietly, or its default with a
    warning. Adding -p and --variable once made every older manifest
    incompatible.
    """
    options = _reconcile(
        {"band_num": "2"},
        current_values={"band_num": 1, "projection": "EPSG:4326", "variable": None},
        command_line={"projection"},
    )

    assert options == {"band_num": "2"}
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == [
        (
            "old.ini: the manifest (IVERT 0.6.10) has no 'variable' option; "
            "using its default (not set)."
        ),
    ]


def test_unrecognized_options_stop_without_a_terminal():
    """Dropping an option IVERT doesn't recognize could change results, so a run that can't ask stops."""
    with pytest.raises(click.ClickException, match="doesn't recognize"):
        _reconcile({"band_num": "1", "projection": "", "variable": "", "old": "x"})


def test_unrecognized_options_are_dropped_if_the_user_agrees(monkeypatch):
    """At a terminal the user is asked, and agreeing replays the manifest without them."""
    monkeypatch.setattr(click, "confirm", lambda *_args, **_kwargs: True)

    options = _reconcile(
        {"band_num": "1", "projection": "", "variable": "", "old": "x"},
        interactive=True,
    )

    assert options == {"band_num": "1", "projection": "", "variable": ""}
