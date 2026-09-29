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
    """Yield the argument list for every command in the tree, groups included.

    Asks each group for its commands the way 'ivert --help' does, so the walk
    finds the commands a user can reach however the CLI loads them.
    """
    yield list(path)
    if isinstance(command, click.Group):
        ctx = click.Context(command)
        for name in command.list_commands(ctx):
            yield from _command_paths(command.get_command(ctx, name), (*path, name))


ALL_COMMAND_PATHS = list(_command_paths(ivert_cli))


@pytest.fixture
def runner():
    return CliRunner()


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
