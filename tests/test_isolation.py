"""Tests for the autouse isolation fixture in conftest.py.

The fixture is what keeps the rest of the suite off a developer's real
configuration, and it is invisible: no test mentions it. If it silently stopped
applying, every other test here would still pass -- while reading and writing a
real ~/.ivert. These make that failure loud.

Where a check would be true anyway on a laptop or a CI runner, it tests the
*seam* instead of the state: it patches the thing to its non-default value and
confirms IVERT actually reads it there. A test that passes whether or not the
fixture works is worse than no test, because it looks like coverage.
"""

import os

from ivert import photon_classes
from ivert.utils import configfile


def test_user_config_is_redirected_into_a_temporary_directory(tmp_path):
    """Config.user_config_path must keep honoring IVERT_USER_CONFIG.

    That environment variable is the seam the whole suite hangs off. If its
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


def test_config_reads_aws_detection_through_the_patchable_seam(monkeypatch):
    """The AWS switch has to stay reachable where conftest patches it.

    configfile does "from ivert.utils import is_aws" and calls
    "is_aws.is_aws()", so patching the module attribute reaches Config.
    Rebinding that import to the function itself would strand the fixture's
    patch, and Config would start reading the [AWS] section on a laptop.

    Asserting is_aws is False would not catch that: it is False anyway
    everywhere except an EC2 instance. Patching it True is what proves the
    seam.
    """
    monkeypatch.setattr("ivert.utils.is_aws.is_aws", lambda: True)

    assert configfile.Config().is_aws is True


# The next two run in this order, which is pytest's default within a file, and
# the ordering is the point: the first populates the lru_cache and the second
# checks the fixture's teardown emptied it again. Keep them adjacent, and keep
# the populating one first.


def test_the_photon_class_cache_is_populated_by_a_lookup():
    photon_classes.photon_classes()

    assert photon_classes.photon_classes.cache_info().currsize == 1


def test_the_photon_class_cache_was_cleared_after_the_previous_test():
    assert photon_classes.photon_classes.cache_info().currsize == 0
