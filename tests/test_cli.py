"""Smoke tests for the command-line interface.

These assert almost nothing about behavior. Their job is to import and invoke
every command in the tree, which is enough to catch a broken import, a malformed
decorator, or two options sharing a flag -- the failures that otherwise reach a
user as a traceback on the first thing they type.
"""

import click
import pytest
from click.testing import CliRunner

from ivert.cli import ivert_cli


def _command_paths(command, path=()):
    """Yield the argument list for every command in the tree, groups included."""
    yield list(path)
    if isinstance(command, click.Group):
        for name, subcommand in sorted(command.commands.items()):
            yield from _command_paths(subcommand, (*path, name))


# Walked rather than hard-coded, so a command added later is covered without
# anyone remembering to list it here.
ALL_COMMAND_PATHS = list(_command_paths(ivert_cli))


@pytest.fixture
def runner():
    return CliRunner()


def test_the_command_tree_was_discovered():
    """Guard the parametrization itself: an empty walk would vacuously pass."""
    assert len(ALL_COMMAND_PATHS) > 10
    assert ["validate"] in ALL_COMMAND_PATHS
    assert ["database", "download"] in ALL_COMMAND_PATHS


@pytest.mark.parametrize(
    "path",
    ALL_COMMAND_PATHS,
    ids=lambda path: " ".join(path) if path else "ivert",
)
def test_help_is_available_for_every_command(runner, path):
    result = runner.invoke(ivert_cli, [*path, "--help"])

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_version_reports_a_version(runner):
    result = runner.invoke(ivert_cli, ["--version"])

    assert result.exit_code == 0, result.output
    assert "ivert" in result.output


def test_classes_lists_the_photon_classification_codes(runner):
    """'ivert classes' reads the codes out of globato, so it also checks that."""
    result = runner.invoke(ivert_cli, ["classes"])

    assert result.exit_code == 0, result.output
    # Ground is code 1 upstream and is the class validation actually uses.
    assert "Ground" in result.output


def test_options_list_runs_against_an_isolated_config(runner):
    """The isolate_ivert_state fixture points this at a tmp file, not ~/.ivert."""
    result = runner.invoke(ivert_cli, ["options", "list"])

    assert result.exit_code == 0, result.output
    assert "Setting" in result.output
