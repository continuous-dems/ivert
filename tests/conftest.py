"""Shared fixtures for the IVERT test suite.

The autouse fixture below keeps every test off the developer's real config, and
out of state earlier tests leave behind.
"""

import pytest

from ivert import photon_classes


@pytest.fixture(autouse=True)
def isolate_ivert_state(tmp_path, monkeypatch):
    """Keep every test off the real ~/.ivert and out of other tests' state."""
    # Config.user_config_path honors IVERT_USER_CONFIG ahead of the packaged
    # default, so setting it redirects reads *and* writes into a per-test
    # temporary directory.
    monkeypatch.setenv(
        "IVERT_USER_CONFIG",
        str(tmp_path / "ivert_user_config.ini"),
    )

    # Config.__init__ switches to the [AWS] section based on this, so pin it
    # rather than letting the answer differ between a laptop and a CI runner.
    monkeypatch.setattr("ivert.utils.is_aws.is_aws", lambda: False)

    # photon_classes() caches its result; clear it so no test sees another's.
    photon_classes.photon_classes.cache_clear()
    yield
    photon_classes.photon_classes.cache_clear()
