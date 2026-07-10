# -*- coding: utf-8 -*-

"""Function for traversing and getting all the files from a directory, including recursively into sub-directories."""

import re
import os
import argparse


def list_files(dirname: str,
               regex_match: str = r"\A[\w\-\.]*",
               ordered: bool = True,
               depth: int = -1,
               include_base_directory: bool = True) -> list:
    file_list = _list_files_recurse(dirname, regex_match=regex_match, depth=depth)

    if not include_base_directory:
        file_list = [fnn[len(dirname):].lstrip(os.sep) for fnn in file_list]

    if ordered:
        file_list.sort()

    return file_list


def _list_files_recurse(dirname, regex_match=None, depth=-1):
    fpath_list = os.listdir(dirname)
    file_list = []

    for entryname in fpath_list:
        try:
            fpath = os.path.join(dirname, entryname)
        except TypeError as e:
            print("dirname:", dirname)
            print("entryname:", entryname)
            raise e
        if (depth == -1 or depth > 0) and os.path.isdir(fpath):
            file_list.extend(_list_files_recurse(fpath, regex_match=regex_match, depth=(-1 if (depth == -1) else (depth-1))))
        elif (regex_match is None) or (re.search(regex_match, entryname) is not None):
            file_list.append(fpath)
    return file_list


def define_and_parse_args():
    parser = argparse.ArgumentParser(
        description="A utility for recursively finding (or deleting) files in a directory and sub-directories.")
    parser.add_argument("DIR", type=str, default=os.getcwd(),
                        help="Directory to search within. Default: Current working directory.")
    parser.add_argument("-text", "-t", type=str, default=r"\A[\.\-\w]*",
                        help="Regular expression to match.")
    parser.add_argument("-depth", type=int, default=-1,
                        help="Maximum directory depth to search. -1 is no limit. 0 is only the local directory. 1 or more delves that many sub-directories. Default -1.")
    parser.add_argument("--delete", "-d", action="store_true", default=False,
                        help="Delete the files matching the search query. NOTE: Suggest to call first without this option to see what will be deleted, then re-call with -d.")

    return parser.parse_args()


if "__main__" == __name__:
    args = define_and_parse_args()

    fnames = list_files(args.DIR,
                        regex_match=args.text,
                        depth=args.depth)

    if len(fnames) > 0 and args.delete:
        response = input(
            "{0} files found matching pattern '{1}' found for deletion. Do you want to proceed (y/n)? ".format(len(fnames), args.text))
        response = response.strip().lower()[0]
    else:
        response = None

    for fn in fnames:
        if args.delete and response == "y":
            print("Removing", fn)
            os.remove(fn)
        else:
            print(fn)
