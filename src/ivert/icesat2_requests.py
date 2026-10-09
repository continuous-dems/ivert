"""icesat2_requests.py — track and re-use NASA Harmony job submissions.

Persists a record of every Harmony job to ~/.ivert/icesat2/requests.csv so
that identical bbox/time requests can be satisfied from a cached job rather
than re-submitting to Harmony.
"""

import ast
import datetime
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

import ivert.utils.configfile
from ivert.utils.cuboid_funcs import BBOX_WITH_DATES_LEN

logger = logging.getLogger(__name__)

# An NSIDC ATL version is written as a zero-padded number of this many digits, "007".
_ATL_VERSION_DIGITS = 3
# Times to try reading the requests CSV, which another process may be part-way
# through writing, before giving up.
_CSV_READ_TRIES = 20


def normalize_atl_version(value: int | str) -> str:
    """Return an NSIDC ATL version as its zero-padded string, e.g. 7 or "7" -> "007".

    The config file stores it quoted ("007"), but a value set by hand may be parsed
    as an int, and either has to compare equal to the release field of a granule
    filename and to the versions recorded in the requests CSV.
    """
    text = str(value).strip()
    if not text.isdigit() or len(text) > _ATL_VERSION_DIGITS:
        msg = (
            "nsidc_atl_version must be a number of up to 3 digits such as 007, not "
            f"{value!r}."
        )
        raise ValueError(msg)
    return text.zfill(_ATL_VERSION_DIGITS)


def _atl_version_from_csv(value: str) -> str:
    """Return a version from the requests CSV, normalized, or "" if none is recorded.

    A file saved by a spreadsheet program can hold the version as a number, "7" or
    "7.0", and one written before the column existed holds nothing. A value that is not
    a version at all is kept as it is, so it matches no lookup.
    """
    text = value.strip().removesuffix(".0")
    if not text:
        return ""
    try:
        return normalize_atl_version(text)
    except ValueError:
        return value


class ICESat2RequestsCSV:
    """Read/write the Harmony request cache at ~/.ivert/icesat2/requests.csv.

    CSV columns:
        atl_dataset     — e.g. "ATL03"
        atl_version     — NSIDC version the job asked for, e.g. "007". Empty in
                          records written before this column existed.
        bbox            — 6-tuple (xmin, xmax, ymin, ymax, tmin, tmax)
        creation_date   — ISO-8601 string from Harmony
        expiration_date — ISO-8601 string from Harmony
        job_id          — Harmony job UUID
        json            — full Harmony status dict, str-repr of a Python dict
    """

    def __init__(self, config=None) -> None:
        """Point at the request CSV named in the configuration; it is read on first use.

        Args:
            config: The IVERT configuration. If None, read the default one.
        """
        if config is None:
            self.config = ivert.utils.configfile.Config()
        else:
            self.config = config
        self.csv_file = Path(self.config.icesat2_requests_csv)
        self.df = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_matching_request(
        self,
        atl_dataset: str,
        bbox,
        *,
        auto_clean_csv: bool = False,
        only_unexpired: bool = True,
        tolerance: float = 1e-9,
        return_rows: bool = False,
        atl_version: int | str | None = None,
    ) -> dict | pd.DataFrame | None:
        """Return the cached Harmony JSON for a matching request, or None.

        Args:
            atl_dataset: Short name, e.g. "ATL03".
            bbox (tuple): 6-tuple (xmin, xmax, ymin, ymax, tmin, tmax).
            auto_clean_csv: When True, drop expired records from the CSV before
                searching, which also makes `only_unexpired` redundant. Defaults to
                False.
            only_unexpired: When True (default), ignore records whose dataExpiration has
                passed.
            tolerance: Absolute tolerance for matching each bbox coordinate. Defaults to
                1e-9.
            return_rows: When True, return the matching DataFrame rows instead of the
                JSON dict.
            atl_version: When given (e.g. "007" or 7), only match records of jobs that
                asked for that version. Records with no version recorded never match, so
                a job submitted before the version was tracked is not re-used. Defaults
                to None (any version).

        """
        if self.df is None:
            self.open()

        if auto_clean_csv:
            self.clean_csv()

        matching_mask = self.df["bbox"].apply(
            lambda b: self._bbox_match(b, bbox, tolerance),
        ) & (self.df["atl_dataset"] == atl_dataset.upper().strip())

        if atl_version is not None:
            matching_mask = matching_mask & (
                self.df["atl_version"] == normalize_atl_version(atl_version)
            )

        if only_unexpired and not auto_clean_csv:
            matching_mask = matching_mask & ~self.df["expiration_date"].apply(
                self._is_expired,
            )

        if np.any(matching_mask):
            if return_rows:
                return self.df[matching_mask]
            return self._read_json(self.df[matching_mask].iloc[0]["json"])
        return None

    def add_record(
        self,
        atl_dataset: str,
        query_bbox,
        json_dict,
        *,
        write_file: bool = True,
        atl_version: int | str = "",
    ):
        """Append a new Harmony job record."""
        if self.df is None:
            self.open()

        if atl_version != "":
            atl_version = normalize_atl_version(atl_version)

        if isinstance(query_bbox, str):
            query_bbox = ast.literal_eval(query_bbox)
        query_bbox = tuple(query_bbox)
        if len(query_bbox) != BBOX_WITH_DATES_LEN:
            msg = (
                "query_bbox must have 6 values (xmin, xmax, ymin, ymax, tmin, tmax), "
                f"not {len(query_bbox)}."
            )
            raise ValueError(msg)

        if isinstance(json_dict, str):
            json_dict = self._read_json(json_dict)

        new_row = pd.DataFrame(
            [
                {
                    "atl_dataset": atl_dataset,
                    "atl_version": atl_version,
                    "bbox": query_bbox,
                    "creation_date": json_dict.get("createdAt", ""),
                    "expiration_date": json_dict.get("dataExpiration", ""),
                    "job_id": json_dict.get("jobID", ""),
                    "json": str(json_dict),
                },
            ],
        )

        self.df = pd.concat([self.df, new_row], ignore_index=True)

        if write_file:
            self.export()

    def update_record(
        self,
        atl_dataset: str,
        query_bbox,
        json_dict,
        *,
        write_file: bool = True,
        fail_quietly: bool = False,
    ):
        """Replace an existing record's JSON (matched by dataset + bbox + job_id)."""
        if self.df is None:
            self.open()

        matching = self.find_matching_request(
            atl_dataset,
            query_bbox,
            only_unexpired=False,
            return_rows=True,
        )
        if matching is None:
            if fail_quietly:
                return None
            msg = f"No matching record for '{atl_dataset}' bbox={query_bbox}"
            raise ValueError(msg)

        if isinstance(json_dict, str):
            json_dict = ast.literal_eval(json_dict)

        job_id = json_dict.get("jobID", "")
        matching = matching[matching["job_id"] == job_id]
        if len(matching) == 0:
            if fail_quietly:
                return None
            msg = f"No record with jobID '{job_id}'"
            raise ValueError(msg)

        self.df.loc[matching.index, "json"] = str(json_dict)

        if write_file:
            self.export()
        return self.df

    def open(self, *, read_again: bool = False, create_if_nonexistent: bool = True):
        """Load the CSV into self.df, creating it if needed."""
        if self.df is not None and not read_again:
            return self.df

        if self.csv_file.exists():
            num_tries = 0
            while num_tries < _CSV_READ_TRIES:
                try:
                    # Read the version as text, or "007" comes back as the number 7.
                    self.df = pd.read_csv(
                        self.csv_file,
                        index_col=False,
                        dtype={"atl_version": str},
                    )
                    break
                except (TypeError, pd.errors.ParserError):
                    num_tries += 1
                    if num_tries >= _CSV_READ_TRIES:
                        raise
                    time.sleep(0.001)
            self.df["bbox"] = self.df["bbox"].apply(ast.literal_eval)
            # Files written before the version was tracked have no such column.
            if "atl_version" not in self.df.columns:
                self.df.insert(1, "atl_version", "")
            self.df["atl_version"] = (
                self.df["atl_version"].fillna("").apply(_atl_version_from_csv)
            )
        elif create_if_nonexistent:
            self._create_empty()
        else:
            msg = f"{self.csv_file} not found."
            raise FileNotFoundError(msg)

        return self.df

    def export(self):
        """Write self.df back to disk."""
        self.csv_file.parent.mkdir(parents=True, exist_ok=True)
        self.df.to_csv(self.csv_file, index=False, header=True)

    def clean_csv(self):
        """Remove expired records from the CSV."""
        if self.df is None:
            self.open()

        expired = self.df["expiration_date"].apply(self._is_expired)
        if np.any(expired):
            logger.info("Removing %s expired Harmony request record(s).", expired.sum())
            self.df = self.df[~expired]
            self.export()
        return self.df

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_empty(self):
        """Create an empty CSV with the correct columns."""
        self.df = pd.DataFrame(
            columns=[
                "atl_dataset",
                "atl_version",
                "bbox",
                "creation_date",
                "expiration_date",
                "job_id",
                "json",
            ],
        )
        self.export()

    @staticmethod
    def _bbox_match(b0, b1, tolerance: float = 1e-9) -> bool:
        """Return True if two 6-tuple bboxes are equal within tolerance."""
        return (
            abs(b0[0] - b1[0]) <= tolerance
            and abs(b0[1] - b1[1]) <= tolerance
            and abs(b0[2] - b1[2]) <= tolerance
            and abs(b0[3] - b1[3]) <= tolerance
            and b0[4] == b1[4]
            and b0[5] == b1[5]
        )

    @staticmethod
    def _is_expired(dt_string: str) -> bool:
        """Return True if the expiration date has passed."""
        # Imported here: it is slow to import, and only this needs it.
        import dateparser  # noqa: PLC0415 - slow import

        try:
            ex = dateparser.parse(dt_string)
            return datetime.datetime.now(datetime.UTC) >= ex
        except TypeError:
            return False

    @staticmethod
    def _read_json(json_str) -> dict:
        return ast.literal_eval(json_str)
