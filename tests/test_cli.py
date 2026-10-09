"""Smoke tests for the command-line interface.

Each runs one command end to end and checks that it succeeds.
"""

import pytest
from click.testing import CliRunner

from ivert.cli import ivert_cli


@pytest.fixture
def runner():
    """Return a Click runner that invokes commands in this process."""
    return CliRunner()


def test_version_reports_a_version(runner):
    """The cheapest end-to-end check: the package imports and the entry point runs."""
    result = runner.invoke(ivert_cli, ["--version"])

    assert result.exit_code == 0, result.output
    assert "ivert" in result.output


def test_classes_lists_the_photon_classification_codes(runner):
    """'ivert classes' reads the codes out of globato, so it also checks that."""
    result = runner.invoke(ivert_cli, ["classes"])

    assert result.exit_code == 0, result.output
    assert "Ground" in result.output


def test_options_list_runs_against_an_isolated_config(runner):
    """'options list' reads every config layer, even conftest's redirected user file."""
    result = runner.invoke(ivert_cli, ["options", "list"])

    assert result.exit_code == 0, result.output
    assert "Setting" in result.output
