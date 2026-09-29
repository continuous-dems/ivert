"""Tests for the autouse isolation fixture in conftest.py.

The fixture is what keeps the rest of the suite off a developer's real
configuration, and it is invisible: no test mentions it. If it silently stopped
applying, every other test here would still pass -- while reading and writing a
real ~/.ivert. These make that failure loud.

Where a check would be true anyway on a laptop or a CI runner, it instead
patches the setting to its non-default value and confirms IVERT reads it from
the place the fixture patches. A test that passes whether or not the
fixture works is worse than no test, because it looks like coverage.
"""

import os

import pytest

from ivert import photon_classes
from ivert.utils import configfile


def test_user_config_is_redirected_into_a_temporary_directory(tmp_path):
    """Config.user_config_path must keep honoring IVERT_USER_CONFIG.

    Every test's isolation depends on that environment variable. If its
    precedence in user_config_path ever changes, every test starts reading the
    developer's real settings and this is what catches it.
    """
    config = configfile.Config()

    assert config.user_config_path == str(tmp_path / "ivert_user_config.ini")
    assert os.environ["IVERT_USER_CONFIG"].startswith(str(tmp_path))


def test_the_real_user_config_is_never_the_target():
    """The safety property itself, stated plainly enough to read at a glance."""
    config = configfile.Config()

    assert not config.user_config_path.startswith(os.path.expanduser("~/.ivert"))


def test_the_config_singleton_starts_unset():
    """Config.__init__ assigns the module global; each test must start clean."""
    assert configfile.ivert_config is None


def test_config_reads_aws_detection_where_conftest_patches_it(monkeypatch):
    """The AWS switch has to stay reachable where conftest patches it.

    configfile does "from ivert.utils import is_aws" and calls
    "is_aws.is_aws()", so patching the module attribute reaches Config.
    Rebinding that import to the function itself would strand the fixture's
    patch, and Config would start reading the [AWS] section on a laptop.

    Asserting is_aws is False would not catch that: it is False anyway
    everywhere except an EC2 instance. Patching it True is what proves the
    fixture's patch reaches Config.
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
