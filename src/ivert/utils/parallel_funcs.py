import logging
import multiprocessing as mp
import os
import shutil
import sys
import time

import numpy as np
import psutil
import tqdm

logger = logging.getLogger(__name__)


def physical_cpu_count():
    """On this machine, get the number of physical cores.

    Not logical cores (when hyperthreading is available), but actual physical cores.
    Things such as multiprocessing.cpu_count often give us the logical cores, which
    means we'll spin off twice as many processes as really helps us when we're
    multiprocessing for performance. We want the physical cores.
    """
    # psutil.cpu_count(logical=False) is cross-platform and returns the number of
    # physical cores. It can return None on some platforms if it can't determine the
    # count, in which case fall back to the logical core count (better than nothing).
    num_physical = psutil.cpu_count(logical=False)
    if num_physical is None:
        return mp.cpu_count()
    return num_physical


# A dictionary for converting numpy array dtypes into carray identifiers.
# For integers & floats... does not handle character/string arrays.
# Reference: https://docs.python.org/3/library/array.html
dtypes_dict = {
    np.int8: "b",
    np.uint8: "B",
    np.int16: "h",
    np.uint16: "H",
    np.int32: "l",
    np.uint32: "L",
    np.int64: "q",
    np.uint64: "Q",
    np.float32: "f",
    np.float64: "d",
    # Repeat for these expressions of dtype as well.
    np.dtype("int8"): "b",
    np.dtype("uint8"): "B",
    np.dtype("int16"): "h",
    np.dtype("uint16"): "H",
    np.dtype("int32"): "l",
    np.dtype("uint32"): "L",
    np.dtype("int64"): "q",
    np.dtype("uint64"): "Q",
    np.dtype("float32"): "f",
    np.dtype("float64"): "d",
}


def process_parallel(
    target_func,
    args_lists,
    kwargs_list=None,
    outfiles=None,
    proc_names=None,
    temp_working_dirs=None,
    overwrite_outfiles: bool = False,
    max_nprocs: int | None = None,
    use_progress_bar_only: bool = False,
    abbreviate_outfile_names_in_stdout: bool = True,
    delete_partially_done_files: bool = True,
) -> None:
    """Most of my parallel processing involves working on a list of files.

    Args:
        target_func (function): process to be executed.
        args_lists (list of lists): A list of arguments to be fed to the function, in the order listed.
        kwargs_list (list of dicts, or dict): A list of keyword-argument dictionaries to be fed to the function.
        outfiles (list, optional): A list of output files that the functions will create.
        proc_names (list, optional): A list of function names to identify each process. Only used if outfiles is not provided.
        temp_working_dirs (list of paths, optional): A list of temporary-directory pathnames to be created as the working-directory
                of each function. Useful if the function creates temporary files. These directories will be created, and then
                destroyed with the function exits, so don't list any directories that contain other data you may need.
                Directories must be in a parent directory that already exists.
        overwrite_outfiles (bool): If output files (listed in outfiles) already exist, delete them and overwrite. Otherwise,
                skip processes in which outfiles already exist.
        max_nprocs (int, optional): Maximum number of processes to run at once. Defaults to the
                physical CPU count.
        use_progress_bar_only (bool): Report progress with the progress bar alone, instead of
                printing a confirmation line as each process finishes.
        abbreviate_outfile_names_in_stdout (bool): Abbreviate the outfile names to the filename only (omit the path) for
                 brevity of output messages.
        delete_partially_done_files (bool): If an exception interrupts the run, delete the
                outfiles of the processes that were still running. Defaults to True.

    """
    # Fill in the number of procs with the default if not provided.
    max_nprocs = physical_cpu_count() if max_nprocs is None else int(max_nprocs)

    # For each optional list, just supply a range of integers if we're not using it. Check for integers later down and
    # ignore them.
    if kwargs_list is None:
        kwargs_list = [None] * len(args_lists)
    elif type(kwargs_list) is dict:
        kwargs_list = [kwargs_list] * len(args_lists)
    elif len(kwargs_list) != len(args_lists):
        raise ValueError(
            f"Length of kwargs_list ({len(kwargs_list)}) != length of args_lists ({len(args_lists)}). Exiting",
        )

    if outfiles is None:
        outfiles = range(len(args_lists))
    elif len(outfiles) != len(args_lists):
        raise ValueError(
            f"Length of outfiles ({len(outfiles)}) != length of args_lists ({len(args_lists)}). Exiting",
        )

    if temp_working_dirs is None:
        temp_working_dirs = range(len(args_lists))
    elif len(temp_working_dirs) != len(args_lists):
        raise ValueError(
            f"Length of temp_working_dirs ({len(temp_working_dirs)}) != length of args_lists ({len(args_lists)}). Exiting",
        )

    if proc_names is None:
        proc_names = range(len(args_lists))
    elif len(proc_names) != len(args_lists):
        raise ValueError(
            f"Length of proc_names ({len(proc_names)}) != length of args_lists ({len(args_lists)}). Exiting",
        )

    running_outfiles = []
    running_procs = []
    running_tempdirs = []
    running_procnames = []

    # The bar is created lazily, on first use: when the caller has given us outfiles or
    # process names to report, we print a line per process instead and never want one.
    progress = None

    def update_bar() -> None:
        """Advance the progress bar to the number of processes finished so far."""
        nonlocal progress
        if progress is None:
            # 'disable=None' tells tqdm to draw the bar only when attached to a
            # terminal, and stay silent when output is redirected to a file or a pipe.
            progress = tqdm.tqdm(
                total=len(args_lists),
                disable=None,
                unit="proc",
                file=sys.stdout,
            )
        progress.update(num_finished - progress.n)

    def report(msg: str) -> None:
        """Print a status line, without clobbering the progress bar if one is up.

        A bar is only up if some earlier process reported through one, in which case
        it is advanced too: this process counts towards 'num_finished' either way, and
        a bar left behind here would never catch up if every later process reports by
        name rather than by bar.
        """
        if not logger.isEnabledFor(logging.INFO):
            return
        if progress is None:
            # 'msg' is already-composed text and may contain a literal '%', so it is
            # passed as an argument rather than used as the format string.
            logger.info("%s", msg)
        else:
            progress.update(num_finished - progress.n)
            progress.write(msg, file=sys.stdout)

    try:
        num_finished = 0
        for i, (args, kwargs, outfile, temp_dir, proc_name) in enumerate(
            zip(
                args_lists,
                kwargs_list,
                outfiles,
                temp_working_dirs,
                proc_names,
                strict=True,
            ),
        ):
            if (outfile is not None) and os.path.exists(outfile) and overwrite_outfiles:
                os.remove(outfile)

            process_started = False
            # Keep looping as long as (a) the process we've iterated to hasn't started yet, or
            #                         (b) we're at the end and we haven't finished executing all the other processes yet.
            while (not process_started) or (
                (i + 1 == len(args_lists)) and (len(running_procs) > 0)
            ):
                # First, loop through all the running processes and see if we need to do anything.
                procs_to_remove = []
                outfiles_to_check = []
                tempdirs_to_remove = []
                procnames_to_remove = []

                # First, check to see if any processes are finished. If so, add them to the list of ones to handle and remove.
                for r_proc, r_outf, r_tdir, r_pname in zip(
                    running_procs,
                    running_outfiles,
                    running_tempdirs,
                    running_procnames,
                    strict=True,
                ):
                    if not r_proc.is_alive():
                        r_proc.join()
                        r_proc.close()
                        procs_to_remove.append(r_proc)
                        outfiles_to_check.append(r_outf)
                        tempdirs_to_remove.append(r_tdir)
                        procnames_to_remove.append(r_pname)

                # Remove any processes and other process metadata that has finished.
                for d_proc, d_outf, d_tdir, d_pname in zip(
                    procs_to_remove,
                    outfiles_to_check,
                    tempdirs_to_remove,
                    procnames_to_remove,
                    strict=True,
                ):
                    num_finished += 1
                    # Print a confirmation line if we've asked it to. Either confirm:
                    # (a) the file has been written,
                    # (b) the process name has completed,
                    # (c) or just a count using the progress bar.
                    if use_progress_bar_only:
                        update_bar()
                    elif type(d_outf) is str:
                        written_qualifier = "" if os.path.exists(d_outf) else "NOT "
                        outf_name = (
                            os.path.basename(d_outf)
                            if abbreviate_outfile_names_in_stdout
                            else d_outf
                        )
                        report(
                            f"{num_finished:,}/{len(args_lists):,} {outf_name} {written_qualifier}written.",
                        )
                    elif type(d_pname) is str:
                        report(
                            f"{num_finished:,}/{len(args_lists):,} {d_pname} finished.",
                        )
                    else:
                        # If we've given no identifying information for the processes, either a file to check
                        # or a process name, just output a progress bar.
                        update_bar()

                    # Delete the temporary directory if it was created.
                    if type(d_tdir) is str and os.path.exists(d_tdir):
                        shutil.rmtree(d_tdir, ignore_errors=True)

                    running_procs.remove(d_proc)
                    running_outfiles.remove(d_outf)
                    running_tempdirs.remove(d_tdir)
                    running_procnames.remove(d_pname)

                if (not process_started) and len(running_procs) < max_nprocs:
                    if (
                        type(outfile) is str
                        and os.path.exists(outfile)
                        and not overwrite_outfiles
                    ):
                        num_finished += 1
                        outfile_name = (
                            os.path.basename(outfile)
                            if abbreviate_outfile_names_in_stdout
                            else outfile
                        )
                        report(
                            f"{num_finished:,}/{len(args_lists):,} {outfile_name} already exists.",
                        )
                        process_started = True
                        continue

                    if kwargs is not None:
                        proc = mp.Process(
                            target=target_func,
                            name=proc_name if (type(proc_name) is str) else None,
                            args=args,
                            kwargs=kwargs,
                        )
                    else:
                        proc = mp.Process(
                            target=target_func,
                            name=proc_name if (type(proc_name) is str) else None,
                            args=args,
                        )

                    if type(temp_dir) is str and not os.path.exists(temp_dir):
                        os.mkdir(temp_dir)

                    running_procs.append(proc)
                    running_outfiles.append(outfile)
                    running_tempdirs.append(temp_dir)
                    running_procnames.append(proc_name)

                    # Since (annoyingly), multiprocessing does not have a "cwd=" keyword like subprocess,
                    # we can simply change the directory of the parent process (temporarily), and then change it back
                    # after starting the funciton.
                    old_cwd = None
                    if type(temp_dir) is str:
                        old_cwd = os.getcwd()
                        os.chdir(temp_dir)
                    proc.start()
                    # Then, change it back to the old one so we stay where we were.
                    if type(temp_dir) is str:
                        os.chdir(old_cwd)

                    process_started = True
                else:
                    # To keep the process from eating CPU, just rest for a tiny fraction of a second here before iterating again.
                    # It's not long enough a time for us to notice, but it's long enough to significantly reduce CPU usage
                    # by this parent process.
                    time.sleep(0.001)

    # If this process crashes or is keyboard-interrupted,
    # clean up the tempdirs and running procs, then re-raise the error to be handled elsewhere.
    except (Exception, KeyboardInterrupt):
        # Kill any running processes.
        for rproc in running_procs:
            rproc.kill()
            rproc.close()
        # Delete all the temp directories we'd created.
        for tdir in running_tempdirs:
            if type(tdir) is str and os.path.exists(tdir):
                shutil.rmtree(tdir, ignore_errors=True)
        if delete_partially_done_files:
            for fn in running_outfiles:
                if type(fn) is str and os.path.exists(fn):
                    os.remove(fn)
        raise

    finally:
        if progress is not None:
            progress.close()
