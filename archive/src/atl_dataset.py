# -*- coding: utf-8 -*-
"""Code for managing, mapping, and searching ATL08 points within the dataset."""

import os
import numpy

import utils.progress_bar as progress_bar
import atl_granules


class ATL_dataset:
    def __init__(self, dirname):
        """Initialize."""
        self.config = ATL_dataset.config
        self.dataframe_dict = {}
        self.granules_dir = dirname

        self._granule_dict = {}

    def _cache_granule(self, granule_obj):
        """Save the granule object into a dictionary if it's not already there."""
        if granule_obj.granule_id not in self._granule_dict:
            self._granule_dict[granule_obj.granule_id] = granule_obj

    def get_granule(self, granule_name, cache=True):
        gid = os.path.splitext(os.path.split(granule_name)[1])[0]

        try:
            return self._granule_dict[gid]
        except KeyError:
            # Check if this is an ATL08 or another datasets granule (ATL03, 06, etc)
            gid_upper = gid.upper()

            if gid_upper.find("ATL03") > -1:
                granule = atl_granules.ATL03_granule(gid)
            elif gid_upper.find("ATL06") > -1:
                granule = atl_granules.ATL08_granule(gid)
            elif gid_upper.find("ATL08") > -1:
                granule = atl_granules.ATL08_granule(gid)
            else:
                raise ValueError(
                    "Uknown granule type '{0}'. Does not contain 'ATL03' or 'ATL08'. Other data types have not yet been implemented."
                )

            if cache:
                self._granule_dict[gid] = granule
            return granule

    def print_missing_granule_files(self, dirname=None):
        """Check to ensure all granules have all files needed.

        Each granule consists of a .h5 and .iso.xml file, with identical filenames.
        Sometimes the NSIDC download scripts can miss a file if a connection times-out.
        This flips through and find any .h5 files that are missing the corresponding .iso.xml file, and vice-versa.
        It prints out the names of any files that are missing.

        This will miss any files where both the .h5 and .iso.xml files are simultaneously missing. No matter.
        """
        if dirname is None:
            dirname = self.granules_dir

        walk_tuple = tuple(os.walk(dirname))
        fnames = walk_tuple[0][2]

        h5_names = [f for f in fnames if os.path.splitext(f)[1] == ".h5"]
        h5_names.sort()
        h5_names_left = h5_names.copy()
        xml_names = [f for f in fnames if f[-8:] == ".iso.xml"]
        xml_names.sort()

        for h5f in h5_names:
            base, ext = os.path.splitext(h5f)
            xml_name = base + ".iso.xml"

            try:
                # Find the xml file.
                _ = xml_names.index(xml_name)
                # Remove it from the list to speed this process.
                xml_names.remove(xml_name)
                h5_names_left.remove(h5f)
            except ValueError:
                # If it doesn't exist, print out the filename
                print(xml_name)

        for xmlf in xml_names:
            base, ext = os.path.splitext(xmlf)
            h5_name = base + ".h5"

            try:
                # Find the hdf5 file.
                _ = h5_names_left.index(h5_name)
                # Remove it from the lsit to speed this process
                h5_names_left.remove(h5_name)
            except ValueError:
                print(h5_name)

        return

    def list_of_granules_ids(self, dirname=None, sort=True):
        """Get a list of all the .h5 filename granules in the data directory.

        If bounding_box is a 4-tuple of (xmin, ymin, xmax, ymax):
            filter out only granules whose data overlaps that bounding box.

        Technically this just finds all the
        """
        if dirname is None:
            dirname = self.granules_dir

        walk_tuple = tuple(os.walk(dirname))
        fnames = walk_tuple[0][2]
        h5_names = [f for f in fnames if os.path.splitext(f)[1].lower() == ".h5"]

        if sort:
            h5_names.sort()

        return h5_names

    def accumulate_data(
        self,
        dataset_str,
        beam=None,
        max_granules=None,
        use_progress_bar=False,
        cache_granules=True,
        warn_if_not_present=True,
    ):
        """Go through all the granules, and accumluate the dataset requested.

        Parameters
        ----------
        dataset_str: The string to the hdf5 leaf dataset to acquire. Example:
            '/quality_assessment/qa_granule_pass_fail' or
            '/[gtx]/land_segments/latitude'

            Use the [gtx] flag to collect specifically from one or more beams.

        beam: An indication of which beam(s) from which to collect data.
            If no [gtx] is in the dataset_str, this parameter is ignored.
            beam can be:
            - None (default): Collect all the beams
            - str: The name of the beam. Values (gt1l, gt1r, gt2l, gt2r, gt3l, gt3r)
            - list or tuple: A list of beam names to collect.

        max_granules: The maximum number of dataset granules to read. Useful for
            debugging or exploratory purposes. Default None: collect them all.

        use_progress_bar: If True, use a progress bar to show progress reading all the samples.

        cache_granules: If we plan to reuse these granules again, save the data and leave them open.
            If not, the granule files will all be closed. Caching saves time but uses memory.
            Default: cache the granules and use them again if we call this function later.

        warn_if_not_present: If the files aren't present, raise a Warning. Else, just skip and read the ones that
            are present.

        Return value
        ------------
        A numpy array containing the data. If no data was collected, return an empty list [].
        """
        granule_ids = self.list_of_granules_ids()

        # Accumulate the datsets into a list.
        N = (
            len(granule_ids)
            if (max_granules is None)
            else min(max_granules, len(granule_ids))
        )
        datasets = [None] * N

        warnings_so_far = [0]

        for i, gid in enumerate(granule_ids[:N]):
            granule = self.get_granule(gid, cache=cache_granules)
            datasets[i] = granule.get_data(
                dataset_str,
                beam=beam,
                warn_if_not_present=warn_if_not_present,
                max_warnings=1,
                warnings_so_far=warnings_so_far,
            )

            if use_progress_bar:
                progress_bar.ProgressBar(i + 1, N, suffix="{0}/{1}".format(i + 1, N))

        # Concatenate all the data in the list.
        return numpy.concatenate(datasets)

    def summarize_flags(self, max_granules=None):
        """For diagnostic and data-exploration purposes, summaries some of the stats."""
        print("\n************\nqa_granule_pass_fail\n************")
        qa_granule_pass_fails = self.accumulate_data(
            "/quality_assessment/qa_granule_pass_fail",
            max_granules=max_granules,
            use_progress_bar=True,
        )
        pass_N = numpy.count_nonzero(qa_granule_pass_fails == 0)
        fail_N = numpy.count_nonzero(qa_granule_pass_fails)
        N = len(qa_granule_pass_fails)
        print(
            "{0} pass ({1}%), {2} fail ({3}%)".format(
                pass_N, (pass_N * 100.0 / N), fail_N, (fail_N * 100 / N)
            )
        )

        print("\n************\nsurf_type\n************")
        surf_types = self.accumulate_data(
            "/[gtx]/land_segments/surf_type",
            max_granules=max_granules,
            warn_if_not_present=True,
            use_progress_bar=True,
        )

        N = surf_types.shape[0]
        land_N = numpy.sum(surf_types[:, 0])
        ocean_N = numpy.sum(surf_types[:, 1])
        seaice_N = numpy.sum(surf_types[:, 2])
        landice_N = numpy.sum(surf_types[:, 3])
        iwater_N = numpy.sum(surf_types[:, 4])

        land_only_N = numpy.sum(
            (surf_types[:, 0] == 1)
            & (surf_types[:, 1] == 0)
            & (surf_types[:, 2] == 0)
            & (surf_types[:, 3] == 0)
            & (surf_types[:, 4] == 0)
        )
        print("Land        : {0} ({1}%)".format(land_N, land_N * 100.0 / N))
        print("Land ONLY   : {0} ({1}%)".format(land_only_N, land_only_N * 100.0 / N))
        print("Ocean       : {0} ({1}%)".format(ocean_N, ocean_N * 100.0 / N))
        print("Sea Ice     : {0} ({1}%)".format(seaice_N, seaice_N * 100.0 / N))
        print("Land Ice    : {0} ({1}%)".format(landice_N, landice_N * 100.0 / N))
        print("Inland Water: {0} ({1}%)".format(iwater_N, iwater_N * 100.0 / N))

        print("\n************\nbrightness_flag\n************")
        bflags = self.accumulate_data(
            "/[gtx]/land_segments/brightness_flag",
            max_granules=max_granules,
            use_progress_bar=True,
        )
        print(bflags.shape)
        N = len(bflags)
        print(
            "Not bright: {0}/{1} ({2} %)".format(
                numpy.count_nonzero(bflags == 0),
                N,
                numpy.count_nonzero(bflags == 0) * 100.0 / N,
            )
        )
        print(
            "Bright: {0}/{1} ({2} %)".format(
                numpy.count_nonzero(bflags == 1),
                N,
                numpy.count_nonzero(bflags == 1) * 100.0 / N,
            )
        )

        print("\n************\nph_removal_flag\n************")
        phrflags = self.accumulate_data(
            "/[gtx]/land_segments/ph_removal_flag",
            max_granules=max_granules,
            use_progress_bar=True,
        )
        print(phrflags.shape)
        N = len(phrflags)
        print(
            "Not exceeded # photons removed: {0}/{1} ({2} %)".format(
                numpy.count_nonzero(phrflags == 0),
                N,
                numpy.count_nonzero(phrflags == 0) * 100.0 / N,
            )
        )
        print(
            "Exceeded # photons removed: {0}/{1} ({2} %)".format(
                numpy.count_nonzero(phrflags == 1),
                N,
                numpy.count_nonzero(phrflags == 1) * 100.0 / N,
            )
        )

        print("\n************\nterrain_flg\n************")
        flags = self.accumulate_data(
            "/[gtx]/land_segments/terrain_flg",
            max_granules=max_granules,
            use_progress_bar=True,
        )
        print(flags.shape)
        N = len(flags)
        print(
            "Not exceeded terrain threshold: {0}/{1} ({2} %)".format(
                numpy.count_nonzero(flags == 0),
                N,
                numpy.count_nonzero(flags == 0) * 100.0 / N,
            )
        )
        print(
            "Exceeded terrain threshold: {0}/{1} ({2} %)".format(
                numpy.count_nonzero(flags == 1),
                N,
                numpy.count_nonzero(flags == 1) * 100.0 / N,
            )
        )

        print("\n************\nurban_flag\n************")
        flags = self.accumulate_data(
            "/[gtx]/land_segments/urban_flag",
            max_granules=max_granules,
            use_progress_bar=True,
        )
        print(flags.shape)
        N = len(flags)
        print(
            "Not urban: {0}/{1} ({2} %)".format(
                numpy.count_nonzero(flags == 0),
                N,
                numpy.count_nonzero(flags == 0) * 100.0 / N,
            )
        )
        print(
            "Urban: {0}/{1} ({2} %)".format(
                numpy.count_nonzero(flags == 1),
                N,
                numpy.count_nonzero(flags == 1) * 100.0 / N,
            )
        )

        print("\n************\nsubset_te_flag\n************")
        flags = self.accumulate_data(
            "/[gtx]/land_segments/terrain/subset_te_flag",
            max_granules=max_granules,
            use_progress_bar=True,
        )
        print(flags.shape)

        for i in range(5):
            print("***", i, ":")
            print(
                -1,
                "{0} ({1}%)\n".format(
                    numpy.count_nonzero(flags[:, i] == -1),
                    (numpy.count_nonzero(flags[:, i] == -1)) * 100.0 / flags.shape[0],
                ),
                0,
                " {0} ({1}%)\n".format(
                    numpy.count_nonzero(flags[:, i] == 0),
                    (numpy.count_nonzero(flags[:, i] == 0)) * 100.0 / flags.shape[0],
                ),
                1,
                " {0} ({1}%)\n".format(
                    numpy.count_nonzero(flags[:, i] == 1),
                    (numpy.count_nonzero(flags[:, i] == 1)) * 100.0 / flags.shape[0],
                ),
            )

        print("\n************\nsc_orient\n************")
        flags = self.accumulate_data(
            "/orbit_info/sc_orient", max_granules=max_granules, use_progress_bar=True
        )

        print(flags.shape)
        print(
            "0 (backward): {0}  ({1} %)".format(
                numpy.count_nonzero(flags == 0),
                numpy.count_nonzero(flags == 0) * 100.0 / len(flags),
            )
        )
        print(
            "1 (forward): {0}  ({1} %)".format(
                numpy.count_nonzero(flags == 1),
                numpy.count_nonzero(flags == 1) * 100.0 / len(flags),
            )
        )
        print(
            "2 (transition): {0}  ({1} %)".format(
                numpy.count_nonzero(flags == 2),
                numpy.count_nonzero(flags == 2) * 100.0 / len(flags),
            )
        )


if __name__ == "__main__":
    ds = ATL_dataset()
    # ds.summarize_flags(max_granules=1000)
    ds.print_missing_granule_files()
