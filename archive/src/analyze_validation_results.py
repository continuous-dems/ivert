# -*- coding: utf-8 -*-

"""analyze_validation_results.py -- A couple functions for taking a deeper look at our ICESat-2 validation results."""

import numpy
import pandas
import os
import argparse


def plot_analysis(photon_h5_name):
    """Plot the analysis of the photon_level_results.h5 file."""
    if os.path.basename(photon_h5_name).find("photon_level_results.h5") == -1:
        print("Need a '_photon_level_results.h5' file to analyze.")
        return

    outname = photon_h5_name.replace("_photon_level_results.h5", "_analysis.png")
    print("Reading", os.path.basename(photon_h5_name), end="...")
    df = pandas.read_hdf(photon_h5_name)
    print(" done.")
    print(df)
    print(df.columns)

    print("class_code values:", numpy.unique(df.class_code))
    print("lat range:", numpy.min(df.latitude), numpy.max(df.latitude))
    print("lon range:", numpy.min(df.longitude), numpy.max(df.longitude))
    print("quality_ph values:", numpy.unique(df.quality_ph))
    print("conf_land values:", numpy.unique(df.conf_land))
    print("conf_land_ice values:", numpy.unique(df.conf_land_ice))
    print("conf_inland_water values:", numpy.unique(df.conf_inland_water))
    print("conf_ocean values:", numpy.unique(df.conf_ocean))
    print("conf_sea_ice values:", numpy.unique(df.conf_sea_ice))

    print()
    print("=== DEM_MINUS_IS2_STATS ===")
    errors = df.dem_minus_is2_m
    print("All:", len(errors), "photons,", numpy.mean(errors), "+/-", numpy.std(errors))
    print("By quality_ph:")
    ph0_errors = errors[df.quality_ph == 0]
    print(
        "\t0:",
        len(ph0_errors),
        "photons,",
        numpy.mean(ph0_errors),
        "+/-",
        numpy.std(ph0_errors),
    )
    ph1_errors = errors[df.quality_ph == 1]
    print(
        "\t1:",
        len(ph1_errors),
        "photons,",
        numpy.mean(ph1_errors),
        "+/-",
        numpy.std(ph1_errors),
    )
    ph2_errors = errors[df.quality_ph == 2]
    print(
        "\t2:",
        len(ph2_errors),
        "photons,",
        numpy.mean(ph2_errors),
        "+/-",
        numpy.std(ph2_errors),
    )

    print("By conf_land:")
    for val in numpy.unique(df.conf_land):
        c0_errors = errors[df.conf_land == val]
        print(
            "\t{0}:".format(val),
            len(c0_errors),
            "photons,",
            numpy.mean(c0_errors),
            "+/-",
            numpy.std(c0_errors),
        )


def define_and_parse_args():
    parser = argparse.ArgumentParser(
        description="Break down validation results into different photon categories. Just to see."
    )
    parser.add_argument(
        "photon_file", help="The photon_level_results.h5 file we want to analyze."
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = define_and_parse_args()
    plot_analysis(args.photon_file)
