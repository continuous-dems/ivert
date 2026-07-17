"""Function for traversing and getting all the files from a directory, including recursively into sub-directories."""

import os
import re

import click


def list_files(
    dirname: str,
    regex_match: str = r"\A[\w\-\.]*",
    ordered: bool = True,
    depth: int = -1,
    include_base_directory: bool = True,
) -> list:
    file_list = _list_files_recurse(dirname, regex_match=regex_match, depth=depth)

    if not include_base_directory:
        file_list = [fnn[len(dirname) :].lstrip(os.sep) for fnn in file_list]

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
            file_list.extend(
                _list_files_recurse(
                    fpath,
                    regex_match=regex_match,
                    depth=(-1 if (depth == -1) else (depth - 1)),
                ),
            )
        elif (regex_match is None) or (re.search(regex_match, entryname) is not None):
            file_list.append(fpath)
    return file_list


@click.command(
    help="A utility for recursively finding (or deleting) files in a directory and sub-directories.",
)
@click.argument("directory", type=str, required=False, default=None)
@click.option(
    "-text",
    "-t",
    "text",
    type=str,
    default=r"\A[\.\-\w]*",
    help="Regular expression to match.",
)
@click.option(
    "-depth",
    "depth",
    type=int,
    default=-1,
    help="Maximum directory depth to search. -1 is no limit. 0 is only the local directory. 1 or more delves that many sub-directories. Default -1.",
)
@click.option(
    "--delete",
    "-d",
    is_flag=True,
    default=False,
    help="Delete the files matching the search query. NOTE: Suggest to call first without this option to see what will be deleted, then re-call with -d.",
)
def main(directory, text, depth, delete):
    """Recursively find (or delete) files in a directory and sub-directories.

    DIRECTORY is the directory to search within. Default: Current working directory.
    """
    if directory is None:
        directory = os.getcwd()

    fnames = list_files(directory, regex_match=text, depth=depth)

    if len(fnames) > 0 and delete:
        response = input(
            f"{len(fnames)} files found matching pattern '{text}' found for deletion. Do you want to proceed (y/n)? ",
        )
        response = response.strip().lower()[0]
    else:
        response = None

    for fn in fnames:
        if delete and response == "y":
            print("Removing", fn)
            os.remove(fn)
        else:
            print(fn)


if __name__ == "__main__":
    main()
