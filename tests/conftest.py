"""Shared fixtures for the IVERT test suite.

The autouse fixture below keeps every test off the developer's real config.
"""

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def isolate_ivert_state(tmp_path, monkeypatch):
    """Keep every test off the real ~/.ivert."""
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


class FakeGlobatoStream:
    """Stands in for the stream globato.read() returns.

    Yields ``chunks`` and records the hooks chained onto it as (name, kwargs)
    pairs in ``hooks``.
    """

    def __init__(self) -> None:
        """Start with no photons and nothing recorded."""
        self.chunks: list = []
        self.hooks: list[tuple[str, dict]] = []

    def read(self, *_args: object, **_kwargs: object) -> "FakeGlobatoStream":
        """Take globato.read()'s place."""
        return self

    def pipe(self, name: str, **kwargs: object) -> "FakeGlobatoStream":
        """Record a hook chained onto the stream."""
        self.hooks.append((name, kwargs))
        return self

    def __iter__(self) -> Iterator:
        """Yield the photon chunks set in ``chunks``."""
        return iter(self.chunks)


@pytest.fixture
def fake_globato_read(monkeypatch):
    """Replace globato.read() with a FakeGlobatoStream; its ``chunks`` feed photons."""
    from ivert import icesat2_database_v2 as is2db  # noqa: PLC0415 - slow import

    stream = FakeGlobatoStream()
    monkeypatch.setattr(is2db.globato, "read", stream.read)
    return stream
