"""icesat2_requests.py — track and re-use NASA Harmony job submissions.

Persists a record of every Harmony job to ~/.ivert/icesat2/requests.csv so
that identical bbox/time requests can be satisfied from a cached job rather
than re-submitting to Harmony.
"""

import ast
import datetime
import os
import time

import dateparser
import numpy
import pandas

import utils.configfile


class ICESat2RequestsCSV:
    """Read/write the Harmony request cache at ~/.ivert/icesat2/requests.csv.

    CSV columns:
        atl_dataset     — e.g. "ATL03"
        bbox            — 6-tuple (xmin, xmax, ymin, ymax, tmin, tmax)
        creation_date   — ISO-8601 string from Harmony
        expiration_date — ISO-8601 string from Harmony
        job_id          — Harmony job UUID
        json            — full Harmony status dict, str-repr of a Python dict
    """

    def __init__(self, config=None):
        if config is None:
            self.config = utils.configfile.Config()
        else:
            self.config = config
        self.csv_file = self.config.icesat2_requests_csv
        self.df = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_matching_request(self,
                              atl_dataset: str,
                              bbox,
                              auto_clean_csv: bool = False,
                              only_unexpired: bool = True,
                              tolerance: float = 1e-9,
                              return_rows: bool = False
                              ) -> dict | pandas.DataFrame | None:
        """Return the cached Harmony JSON for a matching request, or None.

        Parameters
        ----------
        atl_dataset : str
            Short name, e.g. "ATL03".
        bbox : tuple
            6-tuple (xmin, xmax, ymin, ymax, tmin, tmax).
        only_unexpired : bool
            When True (default), ignore records whose dataExpiration has passed.
        return_rows : bool
            When True, return the matching DataFrame rows instead of the JSON dict.
        """
        if self.df is None:
            self.open()

        if auto_clean_csv:
            self.clean_csv()

        matching_mask = (
            self.df["bbox"].apply(lambda b: self._bbox_match(b, bbox, tolerance))
            & (self.df["atl_dataset"] == atl_dataset.upper().strip())
        )

        if only_unexpired and not auto_clean_csv:
            matching_mask = matching_mask & ~self.df["expiration_date"].apply(self._is_expired)

        if numpy.any(matching_mask):
            if return_rows:
                return self.df[matching_mask]
            return self._read_json(self.df[matching_mask].iloc[0]["json"])
        return None

    def add_record(self, atl_dataset: str, query_bbox, json_dict, write_file: bool = True):
        """Append a new Harmony job record."""
        if self.df is None:
            self.open()

        if isinstance(query_bbox, str):
            query_bbox = ast.literal_eval(query_bbox)
        query_bbox = tuple(query_bbox)
        assert len(query_bbox) == 6

        if isinstance(json_dict, str):
            json_dict = self._read_json(json_dict)

        new_row = pandas.DataFrame([{
            "atl_dataset":   atl_dataset,
            "bbox":          query_bbox,
            "creation_date": json_dict.get("createdAt", ""),
            "expiration_date": json_dict.get("dataExpiration", ""),
            "job_id":        json_dict.get("jobID", ""),
            "json":          str(json_dict),
        }])

        self.df = pandas.concat([self.df, new_row], ignore_index=True)

        if write_file:
            self.export()

    def update_record(self,
                      atl_dataset: str,
                      query_bbox,
                      json_dict,
                      write_file: bool = True,
                      fail_quietly: bool = False):
        """Replace the JSON for an existing record (matched by dataset + bbox + job_id)."""
        if self.df is None:
            self.open()

        matching = self.find_matching_request(atl_dataset, query_bbox,
                                              only_unexpired=False, return_rows=True)
        if matching is None:
            if fail_quietly:
                return None
            raise ValueError(f"No matching record for '{atl_dataset}' bbox={query_bbox}")

        if isinstance(json_dict, str):
            json_dict = ast.literal_eval(json_dict)

        job_id = json_dict.get("jobID", "")
        matching = matching[matching["job_id"] == job_id]
        if len(matching) == 0:
            if fail_quietly:
                return None
            raise ValueError(f"No record with jobID '{job_id}'")

        self.df.loc[matching.index, "json"] = str(json_dict)

        if write_file:
            self.export()
        return self.df

    def open(self, read_again: bool = False, create_if_nonexistent: bool = True):
        """Load the CSV into self.df, creating it if needed."""
        if self.df is not None and not read_again:
            return self.df

        if os.path.exists(self.csv_file):
            num_tries = 0
            while num_tries < 20:
                try:
                    self.df = pandas.read_csv(self.csv_file, index_col=False)
                    break
                except (TypeError, pandas.errors.ParserError) as e:
                    num_tries += 1
                    if num_tries >= 20:
                        raise e
                    time.sleep(0.001)
            self.df["bbox"] = self.df["bbox"].apply(ast.literal_eval)
        elif create_if_nonexistent:
            self._create_empty()
        else:
            raise FileNotFoundError(f"{self.csv_file} not found.")

        return self.df

    def export(self):
        """Write self.df back to disk."""
        os.makedirs(os.path.dirname(self.csv_file), exist_ok=True)
        self.df.to_csv(self.csv_file, index=False, header=True)

    def clean_csv(self, verbose: bool = True):
        """Remove expired records from the CSV."""
        if self.df is None:
            self.open()

        expired = self.df["expiration_date"].apply(self._is_expired)
        if numpy.any(expired):
            if verbose:
                print(f"Removing {expired.sum()} expired Harmony request record(s).")
            self.df = self.df[~expired]
            self.export()
        return self.df

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_empty(self):
        """Create an empty CSV with the correct columns."""
        self.df = pandas.DataFrame(columns=[
            "atl_dataset", "bbox", "creation_date",
            "expiration_date", "job_id", "json",
        ])
        self.export()

    @staticmethod
    def _bbox_match(b0, b1, tolerance: float = 1e-9) -> bool:
        """True if two 6-tuple bboxes are equal within tolerance."""
        return (abs(b0[0] - b1[0]) <= tolerance and
                abs(b0[1] - b1[1]) <= tolerance and
                abs(b0[2] - b1[2]) <= tolerance and
                abs(b0[3] - b1[3]) <= tolerance and
                b0[4] == b1[4] and
                b0[5] == b1[5])

    @staticmethod
    def _is_expired(dt_string: str) -> bool:
        """True if the expiration date has passed."""
        try:
            ex = dateparser.parse(dt_string)
            return datetime.datetime.now(datetime.timezone.utc) >= ex
        except Exception:
            return False

    @staticmethod
    def _read_json(json_str) -> dict:
        return ast.literal_eval(json_str)
