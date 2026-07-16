# -*- coding: utf-8 -*-
"""Code for reading ICESat-2 ATL08 individual granule files."""

import h5py
import os
import numpy
import dateparser
import warnings

import utils.configfile as configfile


class ATL_granule:
    """Base class for other ATLXX granules, such as ATL03, ATL06, ATL08."""

    # Save the configuration file as a base class variable, shared one instance among all class instances.
    config = configfile.Config()

    beam_name_dict = {"gt1l": 0, "gt1r": 1, "gt2l": 2, "gt2r": 3, "gt3l": 4, "gt3r": 5}
    beam_code_dict = {0: "gt1l", 1: "gt1r", 2: "gt2l", 3: "gt2r", 4: "gt3l", 5: "gt3r"}

    def __init__(self, h5name, dataset_name):
        # The various settings, in /src/ini/Config.ini
        self.config = ATL_granule.config
        self.atl_sdp_epoch = dateparser.parse(self.config.atlas_sdp_epoch)

        # We can retrieve a granule just from two integers. Handy if we want to pull it up from a database.
        if (
            (type(h5name) in (list, tuple)) or (isinstance(h5name, numpy.ndarray))
        ) and len(h5name) == 2:
            h5name = self.intx2_to_granule_id(
                h5name, atl_version=int(dataset_name[3:5])
            )

        # Search for the .h5 exension. If it doesn't exist, append it.
        # This allows us to just feed in a granule_id and it will find the file.
        if os.path.splitext(h5name)[1].lower() != ".h5":
            h5name = h5name + ".h5"

        # First just look for the file as given.
        if os.path.exists(h5name):
            self.h5name = h5name
        else:
            # If it's not found there, look for it in the data directory.
            dataset_name = dataset_name.strip().upper()
            # Right now I just have ATL03,06,08 explicitly implemented. However, the "else" branch
            # should allow us to get the data directory for any of the datsets along
            # as they follow the same naming convention in the Config.ini file.
            if dataset_name == "ATL03":
                data_dir = self.config.atl03_dir_raw
            if dataset_name == "ATL06":
                data_dir = self.config.atl06_dir_raw
            if dataset_name == "ATL08":
                data_dir = self.config.atl08_dir_raw
            else:
                data_dir = getattr(self.config, dataset_name.lower() + "_dir_raw")

            # If not found there, and it appears no path is given, then append the data directory.
            if os.path.exists(os.path.join(data_dir, h5name)):
                self.h5name = os.path.join(data_dir, h5name)
            else:
                raise FileNotFoundError("File {} not found.".format(h5name))

        # Assume the XML file is right next to the h5 file (can verify this later)
        fbase, fext = os.path.splitext(self.h5name)
        self.xml_name = fbase + ".iso.xml"

        # The granule ID is the name of the file, minus the path and the .h5 extension.
        self.granule_id = os.path.splitext(os.path.split(h5name)[1])[0]
        # Save an empty placeholder here.
        self.h5file = None

    def _open_dataset(self, verbose=False):
        """Open the hdf5 dataset, or return the already-opened instance."""
        if not self.h5file:
            if self.config.verbose and verbose:
                print("Opening", self.h5name)

            try:
                self.h5file = h5py.File(self.h5name, "r")
            except OSError as e:
                print(self.h5name)
                raise e

        return self.h5file

    def _list_datasets(self):
        h5 = self._open_dataset()
        print(list(h5.keys()))

    def print_dataset_tree_structure(self):
        """Parse through the dataset, and print out the structure of the HDF5 table tree."""
        self._print_tree_recursive(self._open_dataset())

    def _print_tree_recursive(self, h5_obj):
        """Recursive function to print out the tree structure."""
        print(
            "    " * (len(h5_obj.name.split("/")) - 2)
            + "/"
            + os.path.split(h5_obj.name)[1],
            end="",
        )

        if isinstance(h5_obj, h5py.File) or isinstance(h5_obj, h5py.Group):
            print()
            for key in h5_obj.keys():
                self._print_tree_recursive(h5_obj[key])

        else:
            assert isinstance(h5_obj, h5py.Dataset)
            print(" ", h5_obj.shape)

        return

    def get_data(
        self,
        dataset_str,
        beam=None,
        warn_if_not_present=False,
        max_warnings=10,
        warnings_so_far=[0],
    ):
        """Given an hdf5 path to a leaf of the dataset, e.g.\
            '/quality_assessment/qa_granule_pass_fail'
        accumulate all the values in the granule and pass them along in the
        order they're read. If "beam" is used, just read that beam, else read them
        all in the order (gt1l, gt1r, gt2l, gt2r, gt3l, gt3r).

        To collect data in each beam from all the beams, put the string '[gtx]'
        (with the brackets) in place of the beam name. If the string does not contain
        a '[gtx]' tag, the parameter 'beam' will be ignored and unused.

        If '[gtx] is contained in the dataset_str, beam can be:
            - None: Use all beams
            - str: the name of a single beam. (gt1l, gt1r, gt2l, gt2r, gt3l, gt3r)
            - list or tuple: a set of zero or more beam names.

        warn_if_note_present:  If False, and the requested data branch doesn't
        exist, throw a KeyError (default hdf5 behavior). If True, just toss a warning.
        """
        h5 = self._open_dataset()

        data = numpy.array([])

        # Get a list of the beams to query.
        if dataset_str.find("[gtx]") > -1:
            if beam is None:
                beams = ["gt1l", "gt1r", "gt2l", "gt2r", "gt3l", "gt3r"]
            elif type(beam) == str:
                beams = [beam.lower()]
            elif type(beam) in (list, tuple):
                beams = [b.lower() for b in beam]

            datasets = []

            for beam_name in beams:
                try:
                    query_str = dataset_str.replace("[gtx]", beam_name)
                    data_temp = h5[query_str][:]
                except KeyError as e:
                    if warn_if_not_present:
                        if warnings_so_far[0] < max_warnings:
                            warnings.warn(
                                "{0} is not present in granule '{1}'".format(
                                    query_str, self.granule_id
                                )
                            )
                            warnings_so_far[0] = warnings_so_far[0] + 1
                        continue
                    else:
                        print(self.h5name, dataset_str, beam_name)
                        raise e
                datasets.append(data_temp)

            if len(datasets) > 0:
                data = numpy.concatenate(datasets)
            else:
                data = numpy.array([])

        else:
            try:
                data = h5[dataset_str][:]
            except KeyError as e:
                if warn_if_not_present:
                    if warnings_so_far[0] < max_warnings:
                        warnings.warn(
                            "{0} is not present in granule '{1}'".format(
                                query_str, self.granule_id
                            )
                        )
                        warnings_so_far[0] = warnings_so_far[0] + 1
                        data = numpy.array([])
                else:
                    raise e

        return data

    def bounding_box(self, beam=None, filter_out_bad_points=True):
        """Return the bounding box (xmin, ymin, xmax, ymax) of all the data within the granule.

        Parameters
        ----------
        beam:
            If you only want the bounding box for one of the 6 beams, set 'beam'
            to one of ('gt1l','gt1r','gt2l','gt2r','gt3l','gt3r'). The default
            None gives you the bounding box covered by all the beams.

        filter_out_bad_points:
            If True, omit points that are flagged as not good. See method
            'quality_mask' for details.

        Return value
        ------------
        A 4-tuple bounding box of (xmin, ymin, xmax, ymax) bounds, in lon/lat.
            If the granule contains no good points, the box will consist of
            (None, None, None, None). Check for this return value.
        """
        # Get a list of the beams to query.
        if beam is None:
            beams = ["gt1l", "gt1r", "gt2l", "gt2r", "gt3l", "gt3r"]
        elif type(beam) == str:
            beams = [beam.lower()]
        elif type(beam) in (list, tuple):
            beams = [b.lower() for b in beam]

        # Set the min/max to silly values to begin.
        min_empty = +1e99
        max_empty = -1e99
        lon_min = min_empty
        lon_max = max_empty
        lat_min = min_empty
        lat_max = max_empty

        # h5 = self._open_dataset()

        for beam_name in beams:
            longitudes, latitudes = self.get_coordinates(
                beam=beam_name, include_height=False
            )
            lon_min = min(lon_min, numpy.min(longitudes))
            lon_max = max(lon_max, numpy.max(longitudes))

            # latitudes = h5["/{0}/land_segments/latitude".format(beam_name)][:]
            lat_min = min(lat_min, numpy.min(latitudes))
            lat_max = max(lat_max, numpy.max(latitudes))

        if lon_min == min_empty:
            lon_min = None
        if lon_max == max_empty:
            lon_max = None
        if lat_min == min_empty:
            lat_min = None
        if lat_max == max_empty:
            lat_max = None

        return (lon_min, lat_min, lon_max, lat_max)

    def granule_id_to_intx2(self, granule_id=None):
        """Convert the granule_ID to 2x int64 integers. Far easier to store in a searchable database that way."""
        if granule_id is None:
            granule_id = self.granule_id

        return granule_id_to_intx2(granule_id)

    def intx2_to_granule_id(self, ix2, atl_version=3):
        """Given two long integers (defined above in granule_id_to_intx2), create a granule_id string."""
        return intx2_to_granule_id(ix2, atl_version)

    def beam_name_to_int(self, beam_name):
        return beam_name_to_int(beam_name)

    def beam_int_to_name(self, beam_code):
        return beam_int_to_name(beam_code)

    def close(self):
        """Close the HDF5 file."""
        if self.h5file:
            self.h5file.close()

    def __del__(self):
        self.close()


class ATL03_granule(ATL_granule):
    """Class for retreiving and selecting data from an ATL03 HDF5 granule file."""

    def __init__(self, h5name):
        # Initialize the base ATL_granule class here.
        super().__init__(h5name, "ATL03")

    def region_number(self, start_or_end="start"):
        """Return the ATL03 Region Number. values 1 thru 14.

        See ATL03-V004-UserGuide.pdf, Figure 3 (Page 6), for reference."""
        h5 = self._open_dataset()
        start_or_end = start_or_end.strip().lower()
        return h5["/ancillary_data/{start_or_end}_region"][0]

    def get_coordinates(self, beam=None, include_height=False):
        """Return the longitude & latitude coordinates for the given beam(s).

        This is a required function for each subclass of ATL_granule."""

        longitudes = self.get_data("/[gtx]/heights/lon_ph", beam=beam)
        latitudes = self.get_data("/[gtx]/heights/lat_ph", beam=beam)

        if include_height:
            heights = self.get_data("/[gtx]/heights/h_ph", beam=beam)
            return longitudes, latitudes, heights
        else:
            return longitudes, latitudes


class ATL06_granule(ATL_granule):
    """Class for retreiving and selecting data from an ATL06 HDF5 granule file."""

    def __init__(self, h5name):
        # Initialize the base ATL_granule class here.
        super().__init__(h5name, "ATL06")

    def get_coordinates(self, beam=None, include_height=False):
        """Return the longitude & latitude coordinates for the given beam(s).

        This is a required function for each subclass of ATL_granule."""
        longitudes = self.get_data("/[gtx]/land_ice_segments/latitude", beam=beam)
        latitudes = self.get_data("/[gtx]/land_ice_segments/longitude", beam=beam)

        if include_height:
            pass
        else:
            return longitudes, latitudes

    def get_segment_numbers(self, beam=None):
        """Return the segment numbers in which each photon resides.

        Segment number, counting from the equator. Equal to the segment_id for
        the second of the two 20m ATL03 segments included in the 40m ATL06
        segment
        (Source: section 3.1.2.1)"""

        return self.get_data("[gtx]/land_ice_segments/segment_id")


class ATL08_granule(ATL_granule):
    """Class for retreiving and selecting data from an ATL08 HDF5 granule file."""

    def __init__(self, h5name):
        # Initialize the base ATL_granule class here.
        super().__init__(h5name, "ATL08")

    def region_number(self):
        """Return the ATL08 Region Number. values 1 thru 11.

        See ATL08-V004-UserGuide.pdf, Figure 5 (Page 9), for reference."""
        h5 = self._open_dataset()
        return h5["ancillary_data/land/atl08_region"][0]

    def get_coordinates(
        self, beam=None, include_heights=False, warn_if_not_present=True
    ):
        """Return the longitude & latitude coordinates for the given beam(s).

        This is a required function for each subclass of ATL_granule."""
        longitudes = self.get_data(
            "/[gtx]/land_segments/longitude",
            beam=beam,
            warn_if_not_present=warn_if_not_present,
        )
        latitudes = self.get_data(
            "/[gtx]/land_segments/latitude",
            beam=beam,
            warn_if_not_present=warn_if_not_present,
        )

        if include_heights:
            pass
        else:
            return longitudes, latitudes

    def granule_passes_qa(self):
        """Return the "/quality_assessment/qa_granule_pass_fail" value."""
        h5 = self._open_dataset()
        return h5["/quality_assessment/qa_granule_pass_fail"][0] == 0

    def granule_fail_reason(self):
        """Return the value of the "/quality_assessments_qa_granule_fail_reason" flag."""
        h5 = self._open_dataset()
        return h5["/quality_assessment/qa_granule_fail_reason"][0]

    def quality_mask(self, beam=None, land_only=False):
        """Return a boolean array (T/F) of points that are good quality within this granule.

        TODO: Specify the various criteria and flags used here.

        Parameters
        ----------
        beam:
            The beam to use. The "None" value will create a mask that concatenates
            the points from all the beams in this order:
                (gt1l, gt1r, gt2l, gt2r, gt3l, gt3r)
            The value can be:
                - a string (one of the beams listed above)
                - a list/tuple of beam names
                - None, specifying ALL the beams (default)
        land_only:
            If False, approve all points that contain the land cover type within them,
            even if mixed with other cover types. If True, only approve point that
            contain the land cover type and none else.
            This parameter uses the ATL08 '/gtXD/land_segment/surf_type' flag (see ATBD).

        Flags analyzed
        --------------
        /quality_assessment/qa_granule_pass_fail:
            - The whole-granule QA failure check. If it fails, just return all 0's,
              we don't want to use any of it.
        /gtXD/land_segments/surf_type
            - the 5-tuple surface type in each segment, defined as
              (land, ocean, sea ice, land ice, inland water), with each being given
              a 0/1 value. Land must be set to 1. If the "land_only" flag is True,
              then all the others must be zero. Else, the others are ignored.
        """
        # Get the list of beams.
        if beam is None:
            beams = ["gt1l", "gt1r", "gt2l", "gt2r", "gt3l", "gt3r"]
        elif type(beam) == str:
            beams = [beam.lower()]
        elif type(beam) in (list, tuple):
            beams = [b.lower() for b in beam]

        # Our list of flags to collect:
        surf_types = numpy.empty([0, 5], dtype=numpy.uint8)

        h5 = self._open_dataset()

        for beam_name in beams:
            surf_types = numpy.append(surf_types, h5["{0}/land_segments/surf_type"])

        # Make sure all our arrays are the same length.
        assert surf_types.shape[0]  # == other_stuff.shape[0] == ....
        N = surf_types.shape[0]

        # Get the overall granule QA flag. If it fails, just return all zeros.
        if not self.granule_passes_qa():
            return numpy.zeros((N,), dtype=bool)

        # Create the surface mask, we want land points (not other surface types).
        # NOTE: Land ice points can be had from the ATL06 dataset, not from here.
        # TODO: Think about inland water? Some large water bodies perhaps shouldn't be ignored here.
        surf_mask = surf_types[:, 0] == 1
        if land_only:
            surf_mask = surf_mask & (numpy.sum(surf_types[:, 1:], axis=1) == 0)

        return surf_mask  # & next_msak & other_mask ...


# Standalone functions that don't need the class encapsulation
def granule_id_to_intx2(granule_id):
    """Convert the granule_ID to 2x int64 integers. Far easier to store in a searchable database that way."""
    granule_id = os.path.split(granule_id)[1]
    granule_id = granule_id[granule_id.find("ATL") :]
    assert (granule_id[0:3].upper() == "ATL") and (int(granule_id[3:5]) < 20)
    i1 = int(granule_id[6:20])
    i2 = int(granule_id[21:29] + granule_id[30:33] + granule_id[34:36])

    return (i1, i2)


def intx2_to_granule_id(ix2, atl_version=3):
    """Given two long integers (defined above in granule_id_to_intx2), create a granule_id string."""

    granule_template = "ATL{0:02d}_{1:014d}_{2:s}_{3:s}_{4:s}"

    i1, i2 = ix2
    i2s = "{0:013d}".format(i2)

    return granule_template.format(atl_version, i1, i2s[0:8], i2s[8:11], i2s[11:13])


def beam_name_to_int(beam_name):
    """Return the integer from the beam name.

    In databases, we use an integer identifier for the beam name rather than the name."""
    beam_name == beam_name.strip().lower()

    return ATL_granule.beam_name_dict[beam_name]


def beam_int_to_name(beam_code):
    """Return the beam name from the integer.

    In databases, we use an integer identifier for the beam name rather than the name."""

    return ATL_granule.beam_code_dict[beam_code]


if __name__ == "__main__":
    # h5 = ATL03_granule("ATL03_20200102112805_01030606_004_01.h5")
    atl3 = ATL03_granule("ATL03_20200102112805_01030606_004_01.h5")

    # h5.print_dataset_tree_structure()

    # beam = 'gt3r'

    atl8 = ATL08_granule("ATL08_20200102112805_01030606_004_01.h5")
    # beam = 'gt3r'
    # data = h5.get_data("/[gtx]/land_segments/terrain/h_te_mean", beam=beam)
    # data2 = h5.get_data("/[gtx]/land_segments/terrain/h_te_mean", beam=beam)
    # print(len(data), data.dtype)
    # print(beam, h5.bounding_box(beam=beam))
