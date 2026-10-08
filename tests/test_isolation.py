"""Tests for the autouse isolation fixture in conftest.py.

No other test mentions the autouse fixture, so if it stopped applying, the suite
would still pass while using a real ~/.ivert. These tests catch that. Where a
check would pass anyway on a normal machine, the test flips the setting and
checks that IVERT sees the patched value.
"""

from pathlib import Path

from ivert.utils import configfile


def test_user_config_is_redirected_into_a_temporary_directory(tmp_path):
    """Config.user_config_path must keep honoring IVERT_USER_CONFIG."""
    config = configfile.Config()

    assert config.user_config_path == tmp_path / "ivert_user_config.ini"


def test_the_real_user_config_is_never_the_target():
    """The guarantee the fixture exists for, checked directly: no test writes to ~/.ivert."""
    config = configfile.Config()

    assert not config.user_config_path.is_relative_to(Path.home() / ".ivert")


def test_config_reads_aws_detection_where_conftest_patches_it(monkeypatch):
    """Conftest's is_aws() patch has to reach Config.

    configfile calls is_aws.is_aws(), so patching the module attribute reaches
    Config. Patching it True proves that; checking False wouldn't, since it's
    False everywhere but EC2.
    """
    monkeypatch.setattr("ivert.utils.is_aws.is_aws", lambda: True)

    assert configfile.Config().is_aws is True
