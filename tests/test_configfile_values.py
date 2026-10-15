"""How a config file's values are read: as literals, booleans, paths or text."""

import math
import os
from pathlib import Path

import pytest

from ivert.utils.configfile import Config


@pytest.fixture
def read(tmp_path):
    """Return a function reading one option's value from a config file of its own."""

    def read_value(value, key="setting"):
        configfile = tmp_path / "configs" / "test_config.ini"
        configfile.parent.mkdir(exist_ok=True)
        configfile.write_text(f"[DEFAULT]\n{key} = {value}\n", encoding="utf-8")
        config = Config(configfile)
        return getattr(config, key), config.path_options

    return read_value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3", 3),
        ("2.5", 2.5),
        ("[1, 6, 40]", [1, 6, 40]),
        ("{'a': 1}", {"a": 1}),
        ("None", None),
        ("True", True),
        ("'007'", "007"),
    ],
)
def test_python_literals_are_read_as_their_values(read, value, expected):
    """Anything ast.literal_eval() reads is taken as that value."""
    assert read(value) == (expected, [])


@pytest.mark.parametrize(("value", "sign"), [("NaN", None), ("inf", 1), ("-inf", -1)])
def test_non_finite_numbers_are_floats(read, value, sign):
    """NaN and inf aren't Python literals, but are numbers all the same."""
    got, _ = read(value)

    assert isinstance(got, float)
    if sign is None:
        assert math.isnan(got)
    else:
        assert got == sign * math.inf


def test_a_zero_padded_number_stays_text(read):
    """'007' unquoted is an ATL version, not the number 7."""
    assert read("007") == ("007", [])


@pytest.mark.parametrize(
    ("value", "expected"),
    [("yes", True), ("On", True), ("1", 1), ("no", False), ("off", False)],
)
def test_boolean_words_are_booleans(read, value, expected):
    """The yes/no and on/off words configparser knows are read as booleans."""
    assert read(value) == (expected, [])


def test_a_home_path_is_expanded(read, monkeypatch, tmp_path):
    """'~' is the user's home directory, on any platform."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert read("~/data") == (tmp_path / "home" / "data", ["setting"])


def test_an_absolute_path_is_kept(read, tmp_path):
    """An absolute path names itself."""
    assert read(f"{tmp_path}/data") == (tmp_path / "data", ["setting"])


def test_a_relative_path_is_relative_to_the_config_file(read, tmp_path):
    """A path with a separator is taken from the config file's own folder."""
    got, paths = read("sub/data")

    assert Path(got) == tmp_path / "configs" / "sub" / "data"
    assert paths == ["setting"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ivert_results_subdir", "results/ivert"),  # resolved against each DEM later
        ("s3_bucket_key", "ivert/inputs"),  # an S3 key, not a local path
        ("setting", "https://example.com/a/b"),  # a URL
    ],
)
def test_paths_that_are_not_local_paths_stay_text(read, key, value):
    """These contain '/' but name nothing on this machine's disk."""
    assert read(value, key=key) == (value, [])


def test_plain_words_are_text(read):
    """Anything else is a string, as written."""
    assert read("ellipsoid") == ("ellipsoid", [])


def test_a_user_setting_that_is_not_a_path_drops_the_path_verdict():
    """A path option overridden with a non-path value is no longer a path option."""
    Path(os.environ["IVERT_USER_CONFIG"]).write_text(
        "[DEFAULT]\nivert_database_directory = 12\n",
        encoding="utf-8",
    )

    config = Config()

    assert config.ivert_database_directory == 12
    assert "ivert_database_directory" not in config.path_options
    assert "user_data_directory" in config.path_options
