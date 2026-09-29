"""Tests for the autouse isolation fixture in conftest.py.

No other test mentions the autouse fixture, so if it stopped applying, the suite
would still pass while using a real ~/.ivert. These tests catch that. Where a
check would pass anyway on a normal machine, the test flips the setting and
checks that IVERT sees the patched value.
"""

from pathlib import Path

import pytest

from ivert import photon_classes
from ivert.utils import configfile


def test_user_config_is_redirected_into_a_temporary_directory(tmp_path):
    """Config.user_config_path must keep honoring IVERT_USER_CONFIG."""
    config = configfile.Config()

    assert config.user_config_path == str(tmp_path / "ivert_user_config.ini")


def test_the_real_user_config_is_never_the_target():
    config = configfile.Config()

    assert not Path(config.user_config_path).is_relative_to(Path.home() / ".ivert")


def test_the_config_singleton_starts_unset():
    """Config.__init__ assigns the module global; each test must start clean."""
    assert configfile.ivert_config is None


def test_config_reads_aws_detection_where_conftest_patches_it(monkeypatch):
    """Conftest's is_aws() patch has to reach Config.

    configfile calls is_aws.is_aws(), so patching the module attribute reaches
    Config. Patching it True proves that; checking False wouldn't, since it's
    False everywhere but EC2.
    """
    monkeypatch.setattr("ivert.utils.is_aws.is_aws", lambda: True)

    assert configfile.Config().is_aws is True


@pytest.fixture(scope="module")
def populated_photon_class_cache():
    """Fill the photon_classes() cache before the per-test isolation fixture runs.

    Pytest always sets up a module-scoped fixture before function-scoped ones like
    the isolation fixture, so this runs first whatever order the tests run in.
    """
    photon_classes.photon_classes()


@pytest.mark.usefixtures("populated_photon_class_cache")
def test_the_photon_class_cache_is_cleared_before_each_test():
    """The cache was filled before this test's setup, so it must be empty now."""
    assert photon_classes.photon_classes.cache_info().currsize == 0
