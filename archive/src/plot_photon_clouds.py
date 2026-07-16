#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Jun 10 20:04:24 2021

plot_photon_clouds.py -- Simple tool for making photon point clouds with matplotlib

@author: mmacferrin
"""

import utils.pickle_blosc

import geopandas
import matplotlib.pyplot as plt
import os
import numpy
import numexpr
import utm
import typing


def plot_photon_data(
    photon_gpkg: typing.Union[str, geopandas.GeoDataFrame],
    outfile: str,
    unique_laser_id: int = 0,
    figsize: typing.Union[tuple, list] = (6.5, 3.2),
    dpi=300,
    bbox=None,
    x="x",
    y="y",
    z="z",
    ylim=None,
    plot_north=True,
):
    """Read an IVERT-derived ICESat-2 photon geodataframe and plot one of the laser paths.

    Parameters
    ----------
    photon_gpkg : str
        Path to the geopackage containing the photon data, or a geodataframe object.
    outfile : str
        Path to the output image file to write.
    unique_laser_id : int
        The unique laser ID of the photon data to plot, in column "unique_laser_id".
        Only used if more than one granule_id is present.
    bbox : list
        Bounding box subset to plot, in (xmin, xmax, ymin, ymax) format, to subset and "zoom in" on the data.
    figsize : tuple, list
        Width and height of the image in inches
    dpi: int
        DPI of the image
    x: str
        Column name of the x coordinate
    y: str
        Column name of the y coordinate
    z: str
        Column name of the z coordinate
    ylim: tuple
        Plot the limits of the y axis.
    plot_north: bool
        Plot from south to north. Reverse if False.

    """

    if isinstance(photon_gpkg, geopandas.GeoDataFrame):
        gdf = photon_gpkg
    elif os.path.splitext(photon_gpkg)[1] == ".gpkg":
        print("Reading", os.path.basename(photon_gpkg), end="...", flush=True)
        gdf = geopandas.read_file(photon_gpkg, driver="GPKG")
        print(" done.", flush=True)
    elif os.path.splitext(photon_gpkg)[1] in (".blosc", ".blosc2"):
        print("Reading", os.path.basename(photon_gpkg), end="...", flush=True)
        gdf = utils.pickle_blosc.read(photon_gpkg)
        print(" done.", flush=True)
    else:
        raise ValueError(
            "Unrecognized file extension: " + os.path.splitext(photon_gpkg)[1]
        )

    # Type codes:
    # -1 : uncoded
    #  0 : noise
    #  1 : ground
    #  2 : canopy
    #  3 : top of canopy
    #  4 : bathy floor
    #  5 : bathy surface
    #  6 : ice surface
    #  7 : built structure

    # If the gdf has -1 values, classify those same as 0 values (noise)
    # For right now, also get rid of forest canopies and just classify those as noise too.
    # Just keep ground, water floor, water surface, and building tops.
    gdf["class_code"] = gdf["class_code"].apply(lambda x: x if (x >= 0) else 0)

    # Anything with a confidence less than 3, just set the class_code to zero. Low confidence is classified as noise.
    # Turn this off, was just testing before.
    # gdf["class_code"] = gdf.apply(lambda x: 0 if x["confidence"] < 3 else x["class_code"], axis=1)

    # Subset the GDF to the unique_granule_id if more than one granule_id exists.
    if len(gdf["unique_laser_id"].unique()) > 1:
        gdf = gdf[gdf["unique_laser_id"] == unique_laser_id].copy()

    # Subset the GDF to the bounding box
    if bbox is not None:
        min_x, max_x, min_y, max_y = bbox

        gdf = gdf[
            numexpr.evaluate(
                "(x >= min_x) & (x <= max_x) & (y >= min_y) & (y <= max_y)",
                local_dict={
                    "x": gdf[x],
                    "y": gdf[y],
                    "min_x": min_x,
                    "max_x": max_x,
                    "min_y": min_y,
                    "max_y": max_y,
                },
            )
        ]

    # Sort the points going either north or south.
    gdf.sort_values(y, axis=0, ascending=plot_north, inplace=True, ignore_index=True)

    # Assign the class colors and labels to be used.
    colors = {}
    labels = {}
    zorders = {}
    markers = {}
    alphas = {}
    sizes = {}
    # Set the settings for the plotting colors and point-sizes.
    for class_code in sorted(gdf["class_code"].unique()):
        # If there are no photons of that class code, don't bother including it in the index.
        if numpy.count_nonzero(gdf["class_code"] == class_code) == 0:
            continue

        if class_code == 0:
            color = "lightgrey"
            label = "Noise"
            zorder = 0
            marker = "o"
            alpha = 0.4
            size = 1

        elif class_code == 1:
            color = "brown"
            label = "Land"
            zorder = 1
            marker = "o"
            alpha = 1.0
            size = 2.5

        elif class_code == 2:
            color = "mediumseagreen"
            label = "Canopy"
            zorder = 0
            marker = "o"
            alpha = 0.4
            size = 1

        elif class_code == 3:
            color = "green"
            label = "Canopy Top"
            zorder = 0
            marker = "o"
            alpha = 0.4
            size = 1

        elif class_code == 4:
            color = "saddlebrown"
            label = "Sea Floor"
            zorder = 1
            marker = "o"
            alpha = 1.0
            size = 2.5

        elif class_code == 5:
            color = "dodgerblue"
            label = "Sea Surface"
            zorder = 0
            marker = "."
            alpha = 1.0
            size = 1

        elif class_code == 6:
            color = "lightblue"
            label = "Ice Surface"
            zorder = 1
            marker = "o"
            alpha = 1.0
            size = 2.5

        elif class_code == 7:
            color = "red"
            label = "Built Structure"
            zorder = 1
            marker = "o"
            alpha = 0.4
            size = 1

        else:
            raise ValueError("Unknown class code {}".format(class_code))

        colors[class_code] = color
        labels[class_code] = label
        zorders[class_code] = zorder
        markers[class_code] = marker
        alphas[class_code] = alpha
        sizes[class_code] = size

    gdf["distance_km"] = calculate_distances(gdf, unit="km", x=x, y=y)

    # Create the figure
    fig, ax = plt.subplots(nrows=1, ncols=1, figsize=figsize, dpi=dpi)

    for class_code in sorted(gdf["class_code"].unique()):
        # If there are no photons of that class code, don't bother including it in the index.
        gdf_class = gdf[gdf["class_code"] == class_code].copy()
        print(f"class {class_code}, {len(gdf_class)} points", flush=True)
        ax.scatter(
            gdf_class["distance_km"],
            gdf_class[z],
            color=colors[class_code],
            zorder=zorders[class_code],
            marker=markers[class_code],
            label=labels[class_code],
            s=sizes[class_code],
            linewidth=0,
            alpha=alphas[class_code],
        )

    lgnd = ax.legend(loc="upper right", fontsize="small", labelspacing=0.5)

    # Change the marker size manually for both lines
    legend_markersize = 20
    for color_id in range(len(lgnd.legendHandles)):
        lgnd.legendHandles[color_id]._sizes = [legend_markersize]

    ax.set_xlabel("Distance (km)")
    ax.set_ylabel("Elevation (m)")

    ax.text(
        0.02,
        0.98,
        "Eureka, CA",
        ha="left",
        va="top",
        fontsize="large",
        transform=ax.transAxes,
    )

    if ylim is None:
        # If we haven't specified y-limits, just use whatever matplot lib automatically selects but pad the top by
        # an extra 10%.
        ax.set_ylim(ax.get_ylim()[0], ax.get_ylim()[1] * 1.1)
    else:
        # Otherwise, use whatever we picked.
        ax.set_ylim(ylim[0], ylim[1])

    fig.tight_layout()

    fig.savefig(outfile)
    print(os.path.basename(outfile), "written.")

    plt.close(fig)
    return


def calculate_distances(gdf, unit="km", x="x", y="y"):
    """Given a geopandas GeoDataFrame, calculate the distance from the first point to each point in the GeoDataFrame."""

    utm_e, utm_n, zone, zone_letter = utm.from_latlon(
        gdf[y].to_numpy(), gdf[x].to_numpy()
    )
    print("UTM", zone, zone_letter)

    e1 = utm_e[0]
    n1 = utm_n[0]
    # Gives UTM coordinates in meters.
    distance_m = numpy.sqrt(numpy.power(utm_e - e1, 2) + numpy.power(utm_n - n1, 2))
    distance_m = distance_m - distance_m.min()
    # print(utm_e - e1, utm_n - n1, distance_m)

    # assert numpy.all(distance_m >= 0) and distance_m.min() == 0
    # assert numpy.all(sorted(distance_m) == distance_m)

    unit = unit.strip().lower()

    if unit == "km":
        return distance_m / 1000
    elif unit == "m":
        return distance_m
    elif unit == "mi":
        return distance_m / 1609.344
    else:
        raise ValueError(f"Unknown unit {unit}.")


if __name__ == "__main__":
    plot_photon_data(
        "/home/mmacferrin/.ivert/jobs/elliot.lim_202409260002/ncei1_westCoast_CRM_2024v1_photons_14.blosc2",
        "/home/mmacferrin/.ivert/jobs/elliot.lim_202409260002/ncei1_westCoast_CRM_2024v1_photons_14_sub1.png",
        unique_laser_id=0,
        bbox=[-124.5, -124, 40.73, 40.92],
        # bbox = [-124.5, -124, 40.8013, 40.815],
        ylim=(-40, 160),
    )
