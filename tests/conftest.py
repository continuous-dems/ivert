"""Shared fixtures for the IVERT test suite.

IVERT carries process-global state -- a user config file it reads and writes, a
module-level Config singleton, and cached lookups of the platform and of
globato's photon classes. Left alone, tests would read and write the developer's
real configuration and pass or fail depending on whose machine they ran on. The
autouse fixture below closes all of that off.
"""

import pytest

from ivert import photon_classes
from ivert.utils import configfile


@pytest.fixture(autouse=True)
def isolate_ivert_state(tmp_path, monkeypatch):
    """Keep every test off the developer's real config, cache and platform.

    Applied automatically to every test, because forgetting it in one place is
    enough to write into a real ~/.ivert config file.
    """
    # Config.user_config_path honors IVERT_USER_CONFIG ahead of the packaged
    # default, so this is the seam that redirects reads *and* writes into a
    # per-test temporary directory.
    monkeypatch.setenv(
        "IVERT_USER_CONFIG",
        str(tmp_path / "ivert_user_config.ini"),
    )

    # Config.__init__ switches to the [AWS] section based on this, so pin it
    # rather than letting the answer differ between a laptop and a CI runner.
    monkeypatch.setattr("ivert.utils.is_aws.is_aws", lambda: False)

    # Config.__init__ assigns to this module global when it loads the defaults
    # file, so the first test to build one would otherwise leak it into the rest.
    monkeypatch.setattr(configfile, "ivert_config", None)

    # photon_classes() is lru_cached over a parse of globato's docstring; clear
    # it on both sides so a test that patches globato neither sees nor leaves a
    # stale result.
    photon_classes.photon_classes.cache_clear()
    yield
    photon_classes.photon_classes.cache_clear()
