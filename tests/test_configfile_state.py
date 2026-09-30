"""IVERT keeps no shared config state, and a Config can't be changed once built.

Code that needs settings builds a Config when it runs, or is handed one, so a
change one caller makes can't reach another, and nothing is read at import.

Self-contained on purpose: no conftest or pytest configuration is needed, so
this runs today and folds under the shared scaffold when that lands. Nothing
here touches the developer's real ~/.ivert.
"""

import importlib
import pickle

import pytest

from ivert.utils import configfile


@pytest.fixture(autouse=True)
def _isolated_user_config(tmp_path, monkeypatch):
    monkeypatch.setenv("IVERT_USER_CONFIG", str(tmp_path / "ivert_user_config.ini"))
    monkeypatch.setattr("ivert.utils.is_aws.is_aws", lambda: False)


def test_there_is_no_module_level_config():
    configfile.Config()

    assert not hasattr(configfile, "ivert_config")


def test_a_built_config_is_read_only():
    config = configfile.Config()

    with pytest.raises(AttributeError, match="read-only"):
        config.dem_default_ndv = 0.0
    with pytest.raises(AttributeError, match="read-only"):
        config.a_new_setting = 1


def test_a_config_survives_pickling():
    """Spawned validation workers receive their Config by pickle."""
    config = configfile.Config()

    copy = pickle.loads(pickle.dumps(config))

    # Compared by repr: dem_default_ndv is NaN, which never equals itself.
    assert {k: repr(v) for k, v in vars(copy).items() if k != "_config"} == {
        k: repr(v) for k, v in vars(config).items() if k != "_config"
    }
    with pytest.raises(AttributeError, match="read-only"):
        copy.dem_default_ndv = 0.0


@pytest.mark.parametrize(
    "module_name",
    ["ivert.validate_dem", "ivert.plot_validation_results"],
)
def test_importing_a_module_reads_no_config(module_name, monkeypatch):
    module = importlib.import_module(module_name)
    built = []
    original_init = configfile.Config.__init__

    def counting_init(self, *args: object, **kwargs: object) -> None:
        built.append(args)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(configfile.Config, "__init__", counting_init)

    importlib.reload(module)

    assert built == []
