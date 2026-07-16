#!/usr/bin/env python
# ----------------------------------------------------------------------------
# NSIDC Data Download Script
#
# Copyright (c) 2022 Regents of the University of Colorado
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# Tested in Python 2.7 and Python 3.4, 3.6, 3.7, 3.8, 3.9
#
# To run the script at a Linux, macOS, or Cygwin command-line terminal:
#   $ python nsidc-data-download.py
#
# On Windows, open Start menu -> Run and type cmd. Then type:
#     python nsidc-data-download.py
#
# The script will first search Earthdata for all matching files.
# You will then be prompted for your Earthdata username/password
# and the script will download the matching files.
#
# If you wish, you may store your Earthdata username/password in a .netrc
# file in your $HOME directory and the script will automatically attempt to
# read this file. The .netrc file should have the following format:
#    machine urs.earthdata.nasa.gov login MYUSERNAME password MYPASSWORD
# where 'MYUSERNAME' and 'MYPASSWORD' are your Earthdata credentials.
#
# Instead of a username/password, you may use an Earthdata bearer token.
# To construct a bearer token, log into Earthdata and choose "Generate Token".
# To use the token, when the script prompts for your username,
# just press Return (Enter). You will then be prompted for your token.
# You can store your bearer token in the .netrc file in the following format:
#    machine urs.earthdata.nasa.gov login token password MYBEARERTOKEN
# where 'MYBEARERTOKEN' is your Earthdata bearer token.
#
from __future__ import print_function

import base64

# import getopt
import itertools
import json
import math

# import netrc
import os
import ssl
import sys
import time
from getpass import getpass
import argparse
import ast
import numpy
import dateutil.parser
import datetime
import re
import shapely.geometry

#
# ####################################3
# # Include the base /src/ directory of thie project, to add all the other modules.
# import import_parent_dir; import_parent_dir.import_src_dir_via_pythonpath()
####################################3
# import utils.progress_bar as progress_bar
# Use Config file to get the encrypted credentials.
import utils.configfile as configfile

my_config = configfile.Config()


try:
    from urllib.parse import urlparse
    from urllib.request import urlopen, Request, build_opener, HTTPCookieProcessor
    from urllib.error import HTTPError, URLError
except ImportError:
    from urlparse import urlparse
    from urllib2 import (
        urlopen,
        Request,
        HTTPError,
        URLError,
        build_opener,
        HTTPCookieProcessor,
    )

# short_name = 'ATL03'
# version = '005'
# time_start = '2021-01-01T00:00:00Z'
# time_end = '2021-12-31T23:59:59Z'
# bounding_box = '-120,45,-119,46'
# polygon = ''
# filename_filter = ''
# url_list = []

CMR_URL = "https://cmr.earthdata.nasa.gov"
# URS_URL = 'https://urs.earthdata.nasa.gov'
CMR_PAGE_SIZE = 2000
CMR_FILE_URL = (
    "{0}/search/granules.json?provider=NSIDC_ECS"
    "&sort_key[]=start_date&sort_key[]=producer_granule_id"
    "&scroll=true&page_size={1}".format(CMR_URL, CMR_PAGE_SIZE)
)


def put_polygon_string_in_correct_rotation(polygon_str, direction="clockwise"):
    """NSIDC only likes clockwise polygons. So, reverse it if it's counter-clockwise."""
    polygon_str = polygon_str.strip()
    polygon_list = ast.literal_eval(
        ("[" if (polygon_str[0] not in ("[", "(")) else "")
        + polygon_str
        + ("]" if (polygon_str[-1] not in ("]", ")")) else "")
    )
    xs = polygon_list[::2]
    ys = polygon_list[1::2]
    sg_a = signed_area(xs, ys)

    if sg_a == 0:
        # If the polygon is entirely on a line or a point, (has no area, or a zero-sum area) it will return zero.
        raise ValueError("Invalid zero-area polygon:", polygon_str)

    is_clockwise = sg_a < 0

    direction_lower = direction.replace(" ", "").replace("-", "").lower()
    assert direction_lower in ("clockwise", "counterclockwise")

    if (is_clockwise and direction_lower == "clockwise") or (
        (not is_clockwise) and direction_lower == "counterclockwise"
    ):
        return polygon_str
    else:
        reverse_polygon = zip(reversed(xs), reversed(ys))
        # Flatten the zipped polygon
        reverse_polygon_flat = [item for sublist in reverse_polygon for item in sublist]
        return ",".join([str(n) for n in reverse_polygon_flat])


def signed_area(xs, ys):
    """Return the signed area enclosed by a ring using the linear time
    algorithm at http://www.cgafaq.info/wiki/Polygon_Area. A value >= 0
    indicates a counter-clockwise oriented ring."""
    return (
        sum([xs[i] * (ys[(i + 1) % len(xs)] - ys[i - 1]) for i in range(1, len(xs))])
        / 2.0
    )


def get_username():
    username = ""

    # For Python 2/3 compatibility:
    try:
        do_input = raw_input  # noqa
    except NameError:
        do_input = input

    username = do_input("NASA Earthdata username: ")
    return username


def get_password():
    password = ""
    while not password:
        password = getpass("password: ")
    return password


def get_username_and_pwd_from_creds():
    uname, pwd = my_config._read_credentials()
    if (uname, pwd) == (None, None):
        # We don't have the file stored, must read the credentials and save to the file.
        uname = get_username()
        pwd = get_password()
        my_config._save_credentials(uname, pwd)

    return uname, pwd


# def get_token():
#     token = ''
#     while not token:
#         token = getpass('bearer token: ')
#     return token


def get_login_credentials():
    """Get user credentials."""
    credentials = None
    token = None

    #    username, password = get_username_and_pwd_from_creds()
    username, password = get_username(), get_password()
    credentials = "{0}:{1}".format(username, password)
    credentials = base64.b64encode(credentials.encode("ascii")).decode("ascii")

    return credentials, token


def build_version_query_params(version):
    desired_pad_length = 3
    if len(version) > desired_pad_length:
        print('Version string too long: "{0}"'.format(version))
        quit()

    version = str(int(version))  # Strip off any leading zeros
    query_params = ""

    while len(version) <= desired_pad_length:
        padded_version = version.zfill(desired_pad_length)
        query_params += "&version={0}".format(padded_version)
        desired_pad_length -= 1
    return query_params


def filter_add_wildcards(filter):
    if not filter.startswith("*"):
        filter = "*" + filter
    if not filter.endswith("*"):
        filter = filter + "*"
    return filter


def build_filename_filter(filename_filter):
    filters = filename_filter.split(",")
    result = "&options[producer_granule_id][pattern]=true"
    for filter in filters:
        result += "&producer_granule_id[]=" + filter_add_wildcards(filter)
    return result


def build_cmr_query_url(
    short_name,
    version,
    time_start,
    time_end,
    bounding_box=None,
    polygon=None,
    filename_filter=None,
):
    params = "&short_name={0}".format(short_name)
    params += build_version_query_params(version)
    params += "&temporal[]={0},{1}".format(time_start, time_end)
    if polygon:
        params += "&polygon={0}".format(polygon)
    elif bounding_box:
        params += "&bounding_box={0}".format(bounding_box)
    if filename_filter:
        params += build_filename_filter(filename_filter)
    return CMR_FILE_URL + params


def get_speed(time_elapsed, chunk_size):
    if time_elapsed <= 0:
        return ""
    speed = chunk_size / time_elapsed
    if speed <= 0:
        speed = 1
    size_name = ("", "k", "M", "G", "T", "P", "E", "Z", "Y")
    i = int(math.floor(math.log(speed, 1000)))
    p = math.pow(1000, i)
    return "{0:.1f}{1}B/s".format(speed / p, size_name[i])


def output_progress(count, total, status="", bar_len=60):
    if total <= 0:
        return
    fraction = min(max(count / float(total), 0), 1)
    filled_len = int(round(bar_len * fraction))
    percents = int(round(100.0 * fraction))
    bar = "=" * filled_len + " " * (bar_len - filled_len)
    fmt = "  [{0}] {1:3d}%  {2}   ".format(bar, percents, status)
    print("\b" * (len(fmt) + 4), end="")  # clears the line
    sys.stdout.write(fmt)
    sys.stdout.flush()


def cmr_read_in_chunks(file_object, chunk_size=1024 * 1024):
    """Read a file in chunks using a generator. Default chunk size: 1Mb."""
    while True:
        data = file_object.read(chunk_size)
        if not data:
            break
        yield data


def get_login_response(url, credentials, token):
    opener = build_opener(HTTPCookieProcessor())

    req = Request(url)
    if token:
        req.add_header("Authorization", "Bearer {0}".format(token))
    elif credentials:
        try:
            response = opener.open(req)
            # We have a redirect URL - try again with authorization.
            url = response.url
        except HTTPError:
            # No redirect - just try again with authorization.
            pass
        except Exception as e:
            print("Error{0}: {1}".format(type(e), str(e)))
            raise e

        req = Request(url)
        req.add_header("Authorization", "Basic {0}".format(credentials))

    try:
        response = opener.open(req)
    except HTTPError as e:
        err = "HTTP error {0}, {1}".format(e.code, e.reason)
        if "Unauthorized" in e.reason:
            if token:
                err += ": Check your bearer token"
            else:
                err += ": Check your username and password"
        print(err)
        raise e
    except Exception as e:
        print("Error{0}: {1}".format(type(e), str(e)))
        raise e

    return response


def cmr_download(urls, download_dir=None, force=False, quiet=False):
    """Download files from list of urls."""
    if not urls:
        return

    url_count = len(urls)
    if not quiet:
        print("Downloading {0} files...".format(url_count))
    credentials = None
    token = None

    # Get the location of the local files to download.
    if download_dir is None:
        local_filenames = [url.split("/")[-1] for url in urls]
    else:
        local_filenames = [
            os.path.join(download_dir, url.split("/")[-1]) for url in urls
        ]

    for index, (url, filename) in enumerate(zip(urls, local_filenames), start=1):
        if not credentials and not token:
            p = urlparse(url)
            if p.scheme == "https":
                credentials, token = get_login_credentials()

        # filename = url.split('/')[-1]
        # if download_dir is not None:
        #     filename = os.path.join(download_dir, filename)

        if not quiet:
            print(
                "{0}/{1}: {2}".format(
                    str(index).zfill(len(str(url_count))), url_count, url
                )
            )

        try:
            response = get_login_response(url, credentials, token)
            length = int(response.headers["content-length"])
            try:
                if not force and length == os.path.getsize(filename):
                    if not quiet:
                        print("  File exists, skipping")
                    continue
            except OSError:
                pass
            count = 0
            chunk_size = min(max(length, 1), 1024 * 1024)
            max_chunks = int(math.ceil(length / chunk_size))
            time_initial = time.time()
            with open(filename, "wb") as out_file:
                for data in cmr_read_in_chunks(response, chunk_size=chunk_size):
                    out_file.write(data)
                    if not quiet:
                        count = count + 1
                        time_elapsed = time.time() - time_initial
                        download_speed = get_speed(time_elapsed, count * chunk_size)
                        output_progress(count, max_chunks, status=download_speed)
            if not quiet:
                print()
        except HTTPError as e:
            print("local file:", filename)
            print("url:", url)
            print("HTTP error {0}, {1}".format(e.code, e.reason))
            raise e
        except URLError as e:
            print("URL error: {0}".format(e.reason))
            raise e
        except IOError as e:
            print("local file:", filename)
            print("url:", url)
            raise e
        except KeyboardInterrupt as e:
            # if the last file was left partially downloaded, delete it.
            if os.path.exists(filename) and os.path.getsize(filename) != length:
                os.remove(filename)
                if not quiet:
                    print("\nPartial file", filename, "removed.")
            # Then re-raise the keyboard interrupt to exit.
            raise e
        except Exception as e:
            print("local file:", filename)
            print("url:", url)
            raise e


def cmr_filter_urls(search_results):
    """Select only the desired data files from CMR response."""
    if "feed" not in search_results or "entry" not in search_results["feed"]:
        return []

    entries = [e["links"] for e in search_results["feed"]["entry"] if "links" in e]
    # Flatten "entries" to a simple list of links
    links = list(itertools.chain(*entries))

    urls = []
    unique_filenames = set()
    for link in links:
        if "href" not in link:
            # Exclude links with nothing to download
            continue
        if "inherited" in link and link["inherited"] is True:
            # Why are we excluding these links?
            continue
        if "rel" in link and "data#" not in link["rel"]:
            # Exclude links which are not classified by CMR as "data" or "metadata"
            continue

        if "title" in link and "opendap" in link["title"].lower():
            # Exclude OPeNDAP links--they are responsible for many duplicates
            # This is a hack; when the metadata is updated to properly identify
            # non-datapool links, we should be able to do this in a non-hack way
            continue

        filename = link["href"].split("/")[-1]
        if filename in unique_filenames:
            # Exclude links with duplicate filenames (they would overwrite)
            continue
        unique_filenames.add(filename)

        urls.append(link["href"])

    return urls


def cmr_search(
    short_name,
    version,
    time_start,
    time_end,
    bounding_box="",
    polygon="",
    filename_filter="",
    quiet=False,
):
    """Perform a scrolling CMR query for files matching input criteria."""
    cmr_query_url = build_cmr_query_url(
        short_name=short_name,
        version=version,
        time_start=time_start,
        time_end=time_end,
        bounding_box=bounding_box,
        polygon=polygon,
        filename_filter=filename_filter,
    )
    if not quiet:
        print("Querying for data:\n\t{0}\n".format(cmr_query_url))

    cmr_scroll_id = None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    urls = []
    hits = 0
    # if not quiet:
    #     print(cmr_query_url)
    while True:
        req = Request(cmr_query_url)
        if cmr_scroll_id:
            req.add_header("cmr-scroll-id", cmr_scroll_id)
        try:
            response = urlopen(req, context=ctx)
        except Exception as e:
            # This is kind of a weird way to code this up, but if we get an except
            # return it rather than raising it. _main() uses this to handle
            # the exception. I may change this later, but it works for now.
            print("Error: " + str(e))
            return e
        if not cmr_scroll_id:
            # Python 2 and 3 have different case for the http headers
            headers = {k.lower(): v for k, v in dict(response.info()).items()}
            cmr_scroll_id = headers["cmr-scroll-id"]
            hits = int(headers["cmr-hits"])
            if not quiet:
                if hits > 0:
                    print("Found {0} matches.".format(hits))
                else:
                    print("Found no matches.")
        search_page = response.read()
        search_page = json.loads(search_page.decode("utf-8"))
        url_scroll_results = cmr_filter_urls(search_page)
        if not url_scroll_results:
            break
        if not quiet and hits > CMR_PAGE_SIZE:
            print(".", end="")
            sys.stdout.flush()
        urls += url_scroll_results

    if not quiet and hits > CMR_PAGE_SIZE:
        print()
    return urls


def get_region_as_bbox_or_polygon(region_text):
    """The region can be either a 4-value bounding box, or a polygon.

    Figure out which one it is, and return either the bounding_box or the polygon,
    with None for the other.
    """
    if type(region_text) == str:
        region_obj = ast.literal_eval(region_text)
    else:
        region_obj = region_text

    if (
        (type(region_obj) in (list, tuple))
        and (len(region_obj) == 4)
        and numpy.all([type(i) in (int, float) for i in region_obj])
    ):
        bounding_box = region_obj
        polygon = None
    else:
        # Flatten the polygon object
        if type(region_obj) == shapely.geometry.Polygon:
            polygon = list(numpy.array(list(region_obj.exterior.coords)).flatten())
        else:
            polygon = list(numpy.concatenate(region_obj).flatten())
        bounding_box = None

    if bounding_box != None:
        bounding_box = ",".join([str(x) for x in bounding_box])
    if polygon != None:
        polygon = ",".join([str(x) for x in polygon])

    return bounding_box, polygon


def define_and_parse_args():
    parser = argparse.ArgumentParser(description="Tool for downloading NSIDC granules.")
    parser.add_argument(
        "-dataset_name",
        "-d",
        metavar="ATLXX",
        type=str,
        default="ATL03",
        help="Dataset short name. Currently accepts ATL03, ATL06, or ATL08. Default ATL03.",
    )
    parser.add_argument(
        "-region",
        "-r",
        type=str,
        default="[-180,-90,180,90]",
        help="Search area. Can be [xmin,ymin,xmax,ymax], a list of (x,y) points in a polygon [[x1,y1],[x2,y2],...,[x1,y1]] in WGS84 (EPSG:4326) lat/lon coordinates.",
    )
    parser.add_argument(
        "-local_dir",
        default=os.getcwd(),
        help="Local directory to download the data. Defaults to the process current-working-directory.",
    )
    parser.add_argument(
        "-dates",
        default="2021-01-01,2021-12-31",
        type=str,
        help="A pair of dates (separated by a comma) in which to search: YYYY-MM-DD,YYYY-MM-DD",
    )
    parser.add_argument(
        "-version", default="005", type=str, help="Version of the data."
    )
    parser.add_argument(
        "-fname_filter",
        default="",
        type=str,
        help="Filename filter by which to search for explicit file names. Can use open flags, such as 'ATL03_20200102*'.",
    )
    parser.add_argument(
        "--force",
        "-f",
        default=False,
        action="store_true",
        help="Force the download even if files already exist.",
    )
    parser.add_argument(
        "--query_only",
        default=False,
        action="store_true",
        help="Only return (or print) a list of URLS. Do not download. Overrides -force.",
    )
    parser.add_argument(
        "--quiet", "-q", default=False, action="store_true", help="Run in quiet mode."
    )

    return parser.parse_args()


def download_granules(
    short_name="ATL03",
    region=[-180, -90, 180, 90],
    local_dir=os.getcwd(),
    dates=["2021-01-01", "2021-12-31"],
    version="005",
    fname_filter="",
    force=False,
    query_only=False,
    fname_python_regex=r"\.h5\Z",
    use_wget="backup",
    download_only_matching_granules=True,
    skip_granules_if_photon_db_exists=True,
    quiet=False,
):
    """An API function for downloading without the command-line, from another
    python module.
    """
    # print(short_name)
    # print(region)
    # print(local_dir)
    # print(dates)
    # print(version)
    # print(fname_filter)
    # print(force)
    # print(query_only)
    # print(fname_python_regex)
    # print(download_only_matching_granules)
    # print(quiet)
    local_files = _main(
        short_name=short_name,
        region=region,
        local_dir=local_dir,
        dates=dates,
        version=version,
        fname_filter=fname_filter,
        force=force,
        query_only=query_only,
        fname_python_regex=fname_python_regex,
        use_wget=use_wget,
        download_only_matching_granules=download_only_matching_granules,
        quiet=quiet,
    )
    return local_files


def _main(
    short_name=None,
    region=None,
    local_dir=None,
    dates=None,
    version=None,
    fname_filter=None,
    force=None,
    query_only=None,
    fname_python_regex=r"\.h5\Z",
    use_wget="backup",
    download_only_matching_granules=True,
    skip_granules_if_photon_db_exists=True,
    quiet=None,
):
    """'short_name' may be either a single ATLXX name ("ATL03", e.g.) or a list of ALTXX names (["ATL03", "ATL08"], e.g.).

    If short_name is more than one dataset (e.g. "ATL03" and "ATL08", for instance), and
    'download_only_matching_granules' is set to True (the default), then
    find only granules that are matching between these two datasets, in which both an
    ATL03 *and* an ATL08 grnaule exist together on the same piece of data.

    This eliminates downloading unmatched granules that the classify_icesat2_photons.py
    script cannot use because it can only use matching pairs of ATL03 and ATL08 granules.

    The 'use_wget' parameter includes a few different options: -- Not yet implemented. May do this later. (maybe not)
        - False: Just use the default NSIDC cmr_download function.
        - True: Use wget instead. (This will write a temporary script of URLs to which to write, in the destination directory.)
        - "backup" (string): This will use cmr_download unless it breaks or returns an exception,
                             in which case it will resort to wget to download the files.
    """
    # If we ran this from the command-line, all these arg will be None, in which
    # case get them all from the argparse command-line arguments.
    if (
        short_name is None
        and region is None
        and local_dir is None
        and dates is None
        and version is None
        and fname_filter is None
        and force is None
        and query_only is None
        and quiet is None
    ):
        args = define_and_parse_args()

        force = args.force
        quiet = args.quiet

        short_names = [args.dataset_name]
        bounding_box, polygon = get_region_as_bbox_or_polygon(args.region)
        local_dir = args.local_dir
        time_start, time_end = args.dates.split(",")
        version = args.version
        fname_filter = args.fname_filter
        query_only = args.query_only

    else:
        if type(short_name) == str:
            short_names = [short_name]
        else:
            assert type(short_name) in (list, tuple)
            short_names = short_name
        time_start, time_end = dates
        bounding_box, polygon = get_region_as_bbox_or_polygon(region)
        if version is None:
            version = "005"
        elif type(version) == int:
            version = "{0:03d}".format(version)

    # Make sure "time_start" is in a pretty iso-format string.
    if type(time_start) in (datetime.datetime, datetime.date):
        time_start_str = time_start.isoformat() + "Z"
    elif type(time_start) == str:
        time_start_str = dateutil.parser.parse(time_start).isoformat() + "Z"

    if type(time_end) == str:
        time_end = dateutil.parser.parse(time_end)
    # Make sure the dates are inclusive, going to the last second of the last day.
    if type(time_end) == datetime.date:
        time_end = datetime.datetime.combine(time_end, datetime.datetime.max.time())
    elif (
        (time_end.hour == 0)
        and (time_end.minute == 0)
        and (time_end.second == 0)
        and (time_end.microsecond == 0)
    ):
        time_end = datetime.datetime.combine(
            time_end.date(), datetime.datetime.max.time()
        )

    # Make sure "time_end" is in a pretty iso-format string.
    time_end_str = time_end.isoformat() + "Z"

    # Make sure the polygon is counter-clockwise.
    # I'm confused, NSIDC sometimes seems to break with a counter-clockwise polygon, other
    # times break with a clockwise polygon. Not sure what's going on there, but
    # We'll try one, and if it doesn't work, try the other.
    if polygon is not None:
        polygon = put_polygon_string_in_correct_rotation(polygon, direction="clockwise")

    urls_to_download = []
    urls_total = []
    numfiles_existing = 0
    # List through each of the short_names listed.
    for sname in short_names:
        try_again = True
        tried_polygon_switch_already = False
        while try_again:
            try:
                url_list = cmr_search(
                    sname,
                    version,
                    time_start_str,
                    time_end_str,
                    bounding_box=bounding_box,
                    polygon=polygon,
                    filename_filter=fname_filter,
                    quiet=quiet,
                )
            except KeyboardInterrupt as e:
                return e

            except:
                pass

            if isinstance(url_list, Exception):
                # The above cmr_search function has been modified. Rather than raising errors directly, it'll
                # just return them if they happen, and we can handle them as we like.

                err_str = str(url_list).lower()
                if err_str.find("too many requests") >= 0:
                    print(
                        "HTTP Error 429: Too Many Requests. Waiting 1 minute and trying again."
                    )
                    wait_time_sec = 60
                    for i in range(wait_time_sec):
                        print(
                            "\r  {0}:{1:02d}".format(
                                int(wait_time_sec / 60), wait_time_sec % 60
                            ),
                            end="",
                        )
                        time.sleep(1)
                        wait_time_sec -= 1
                    print("\r  0:00")
                    try_again = True  # This is redundant but just make sure.

                elif (
                    err_str.find("name resolution") >= 0
                    or err_str.find("temporary") >= 0
                ):
                    print(str(url_list) + "; trying again...")
                    time.sleep(1)
                    try_again = True

                # Due to the weird polygon orientation bug that I haven't yet fully diagnosed, I'm just trying
                # to fix it by going the other way if it doesn't work at first.
                elif polygon is not None and not tried_polygon_switch_already:
                    polygon = put_polygon_string_in_correct_rotation(
                        polygon, direction="counterclockwise"
                    )
                    tried_polygon_switch_already = True
                    print(str(url_list) + "; swapping polygon and trying again...")
                    try_again = True

                # If it's *still* returning an Exception error of some other kind, try again?
                else:
                    print(str(url_list) + "; trying again...")
                    time.sleep(1)
                    try_again = True

            else:
                try_again = False

        # fname_bases = [url.split("/")[-1] for url in url_list]

        # Filter out ones that don't fit the python regex (this often gets rid of things like XML files that we may not need.)
        if fname_python_regex is not None:
            # fname_bases = [fn for fn in fname_bases if (re.search(fname_python_regex, fn) is not None)]
            url_list = [
                url
                for url in url_list
                if (re.search(fname_python_regex, url.split("/")[-1]) is not None)
            ]

        # assert len(fname_bases) == len(url_list)

        if not quiet:
            print(
                len(url_list), sname, "granules found within bounding box & date range."
            )

        urls_total.append(
            url_list
        )  # urls_total is a list of lists, corresponding with each dataset short_name

    if len(short_names) == 1:
        urls_total = urls_total[0]

    elif download_only_matching_granules and len(short_names) > 1:
        urls_total_original = urls_total
        # Often, we get more ATL03 granules returned than ATL08 granules in the same bounding box,
        # perhaps because not all of the ATL03 granules necessarily overlap land (ATL08 is primarily a land-cover product, not meant for clouds, etc).
        # If we don't need ATL03 granules that don't have a matching ATL08 file,
        # filter them out here to skip them.
        # 1. Put ATLXX in each dataset URL in place of the dataset name.
        #    Matching granules SHOULD have the exact same name then. Ignore the full URLs for now.
        #    Put these ATLXX names in a Python set() for each dataset.
        fname_sets = []
        for sname, url_list in zip(short_names, urls_total):
            url_set = set(
                [url.split("/")[-1].replace(sname, "[ATLXX]") for url in url_list]
            )
            fname_sets.append(url_set)

        # 2. Get the intersection of all the url sets. These will be matching names.
        fname_master_set = fname_sets[0]
        for fset in fname_sets[1:]:
            fname_master_set = fname_master_set.intersection(fset)
        # fname_master_set is now a set with unique filenames (not paths) with "[ATLXX]" in place of the
        # dataet name, in which all datasets have a match with that name.

        # 3. Create a master list for full-length URLs in the set of matching names.
        url_master_list = []
        for sname, url_list in zip(short_names, urls_total):
            for fname in fname_master_set:
                fname_with_sname = fname.replace("[ATLXX]", sname)
                url_master_list.append(
                    [
                        url
                        for url in url_list
                        if url.split("/")[-1].find(fname_with_sname) > -1
                    ][0]
                )

        urls_total = sorted(url_master_list)

        if not quiet:
            print(
                len(fname_master_set),
                "common granules found between",
                ",".join(short_names),
                "for",
                len(urls_total),
                "granules total.",
            )

        # In some weird cases, we're getting granules returned for both data sets, but
        # no "matching" granules between ATL03 and ATL08. If so, print out the granule names here, just for bug-searching sake.
        if (
            (not quiet)
            and len(urls_total) == 0
            and numpy.all([len(url_list) > 0 for url_list in urls_total_original])
        ):
            print("List of granules:")
            for url_list in urls_total:
                for url in sorted(url_list):
                    print("\t", url.split("/")[-1])

    else:
        # If there's more than one dataset short_name and we didn't specify download_only_matching_granules
        # just flatten the list of lists into one long list of urls together.
        urls_total = [url for urls_sublist in urls_total for url in urls_sublist]

    fname_bases = [url.split("/")[-1] for url in urls_total]

    # Look for existing files.
    local_files = [os.path.join(local_dir, fn) for fn in fname_bases]
    for url, lfile in zip(urls_total, local_files):
        if os.path.exists(lfile) and not force:
            numfiles_existing += 1
        else:
            urls_to_download.append(url)

    if query_only not in (None, False):
        if not quiet:
            # Printing the URLs is handy for a parent-process call that may be using STDOUT to get the list of files to download.
            for url in url_list:
                print(url)
        return local_files

    if not quiet:
        print(
            "{0} of {1} granules already exist locally. Downloading {2} new granules from NSIDC.".format(
                numfiles_existing,
                len(urls_to_download) + numfiles_existing,
                len(urls_to_download),
            )
        )

    if len(urls_to_download) == 0:
        return local_files

    # If we've already createed a _photon.h5 file of an ATL03 granule, do not download the ATL03 or ATL08 granule.
    if skip_granules_if_photon_db_exists:
        local_atl03_files = [
            fn for fn in local_files if os.path.split(fn)[1].find("ATL03") > -1
        ]
        urls_removed = 0
        for atl03 in local_atl03_files:
            db_file = os.path.splitext(atl03)[0] + "_photons.h5"
            if os.path.exists(db_file):
                # Remove the atl03 from urls_to_download
                atl03_granule_name = os.path.split(atl03)[1]
                for url in urls_to_download:
                    if url.find(atl03_granule_name) > -1:
                        urls_to_download.remove(url)
                        urls_removed += 1
                # Remove the atl08 from urls_to_download
                atl08_granule_name = atl03_granule_name.replace("ATL03", "ATL08")
                for url in urls_to_download:
                    if url.find(atl08_granule_name) > -1:
                        urls_to_download.remove(url)
                        urls_removed += 1

        if not quiet and urls_removed > 0:
            print(
                urls_removed,
                "granules already have a _photons.h5 database present. Downloading",
                len(urls_to_download),
                "new granules.",
            )

    # Download the urls that need to be downloaded.
    if use_wget == True:
        # TODO: Put a wget script here.
        pass
    else:
        try:
            cmr_download(
                urls_to_download, download_dir=local_dir, force=force, quiet=quiet
            )
        except Exception as e:
            return e

    return local_files


if __name__ == "__main__":
    # Re-download a granule that's acting up, here.
    download_granules(
        short_name=["ATL03", "ATL08"],
        region=[-70, 58, 100, 60],
        fname_filter="*20210127165305_05301003*",
        local_dir="/home/mmacferrin/Research/DATA/ETOPO/data/icesat2/granules/",
    )

    # Just testing how quickly NSIDC queries are.
    # time1 = time.time()
    # urls = cmr_search("ATL03", "004", "2021-01-01", "2022-01-01", polygon="-72,0,-72,2,-70,2,-70,0,-72,0") # bounding_box="-72,0,-70,2",) # polygon='-171.0005555555413,-14.500555546905522,-171.0005555555413,-13.999444442853925,-169.24981479479877,-13.999444442853925,-169.24981479479877,-14.500555546905522,-171.0005555555413,-14.500555546905522')
    # time2 = time.time()
    # print(len(urls), "urls returned in", time2-time1, "seconds.")
    # for i in range(15):
    #     print('\t', urls[i])
    # main()
