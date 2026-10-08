"""The database index always lives in the database directory, wherever that is set."""

import logging

from ivert.icesat2_database_v2 import IS2Database
from ivert.utils.configfile import Config


def _user_config(tmp_path, text):
    (tmp_path / "ivert_user_config.ini").write_text("[DEFAULT]\n" + text)


def test_moving_the_database_directory_moves_the_index(tmp_path):
    """The index used to stay at its default path, in the old directory.

    Every database command then read or rebuilt an index that listed none of
    the granules beside it, while the granules' own index went unused.
    """
    _user_config(tmp_path, f"ivert_database_directory = {tmp_path / 'db'}\n")

    db = IS2Database(ivert_config=Config())

    assert db.db_fname == tmp_path / "db" / "_ivert_database_index.nc"
    assert db.db_fname.parent == db.granules_dir


def test_an_old_index_setting_is_ignored_and_commented_out(tmp_path, caplog):
    """A user config from before the change keeps working; the setting is retired."""
    elsewhere = tmp_path / "elsewhere" / "index.nc"
    _user_config(
        tmp_path,
        f"ivert_database_directory = {tmp_path / 'db'}\n"
        f"ivert_database_index = {elsewhere}\n",
    )

    with caplog.at_level(logging.WARNING):
        db = IS2Database(ivert_config=Config())

    assert db.db_fname == tmp_path / "db" / "_ivert_database_index.nc"
    assert "ivert_database_index" in caplog.text
    lines = (tmp_path / "ivert_user_config.ini").read_text().splitlines()
    assert not any(line.startswith("ivert_database_index") for line in lines)
