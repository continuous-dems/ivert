# -*- coding: utf-8 -*-

import sys
import os
import multiprocessing as mp
import numpy
import time
import subprocess

if vars(sys.modules[__name__])['__package__'] == 'ivert_utils':
    # When this is built a setup.py package, it names the modules 'ivert' and 'ivert_utils'. This reflects that.
    import ivert_utils.progress_bar as progress_bar
else:
    try:
        import progress_bar
    except ModuleNotFoundError:
        import utils.progress_bar as progress_bar

def physical_cpu_count():
    """On this machine, get the number of physical cores.

    Not logical cores (when hyperthreading is available), but actual physical cores.
    Things such as multiprocessing.cpu_count often give us the logical cores, which
    means we'll spin off twice as many processes as really helps us when we're
    multiprocessing for performance. We want the physical cores."""
    if sys.platform == "linux" or sys.platform == "linux2":
        # On linux. The "linux2" variant is no longer used but here for backward-compatibility.
        lines = os.popen('lscpu').readlines()
        line_with_sockets = [l for l in lines if l[0:11] == "Socket(s): "][0]
        line_with_cps = [l for l in lines if l[0:20] == "Core(s) per socket: "][0]

        num_sockets = int(line_with_sockets.split()[-1])
        num_cores_per_socket = int(line_with_cps.split()[-1])

        return num_sockets * num_cores_per_socket

    elif sys.platform == "darwin":
        # On a mac
        # TODO: Flesh this out from https://stackoverflow.com/questions/12902008/python-how-to-find-out-whether-hyperthreading-is-enabled
        return mp.cpu_count()

    elif sys.platform == "win32" or sys.platform == "win64" or sys.platform == "cygwin":
        # On a windows machine.
        # TODO: Flesh this out from https://stackoverflow.com/questions/12902008/python-how-to-find-out-whether-hyperthreading-is-enabled
        return mp.cpu_count()

    else:
        # If we don't know what platform they're using, just default to the cpu_count()
        # It will only get logical cores, but it's better than nothing.
        return mp.cpu_count()

# A dictionary for converting numpy array dtypes into carray identifiers.
# For integers & floats... does not hangle character/string arrays.
# Reference: https://docs.python.org/3/library/array.html
dtypes_dict = {numpy.int8:    'b',
               numpy.uint8:   'B',
               numpy.int16:   'h',
               numpy.uint16:  'H',
               numpy.int32:   'l',
               numpy.uint32:  'L',
               numpy.int64:   'q',
               numpy.uint64:  'Q',
               numpy.float32: 'f',
               numpy.float64: 'd',
               # Repeat for these expressions of dtype as well.
               numpy.dtype('int8'):    'b',
               numpy.dtype('uint8'):   'B',
               numpy.dtype('int16'):   'h',
               numpy.dtype('uint16'):  'H',
               numpy.dtype('int32'):   'l',
               numpy.dtype('uint32'):  'L',
               numpy.dtype('int64'):   'q',
               numpy.dtype('uint64'):  'Q',
               numpy.dtype('float32'): 'f',
               numpy.dtype('float64'): 'd'}


def process_parallel(target_func,
                     args_lists,
                     kwargs_list=None,
                     outfiles=None,
                     proc_names=None,
                     temp_working_dirs=None,
                     overwrite_outfiles: bool = False,
                     max_nprocs: int = physical_cpu_count(),
                     use_progress_bar_only: bool = False,
                     abbreviate_outfile_names_in_stdout: bool = True,
                     delete_partially_done_files: bool = True,
                     verbose=True) -> None:
    """Most of my parallel processing involves working on a list of files.

    parameters:
    ----------
        target_func (function): process to be executed.
        args_lists (list of lists): A list of arguments to be fed to the function, in the order listed.
        kwargs_lists (list of dicts, or dict): A list of keyword-argument dictinoaries to be fed to the function.
        outfiles (list, optional): A list of output files that the functions will create.
        proc_names (list, optional): A list of function names to identify each process. Only used if outfiles is not provided.
        temp_working_dirs (list of paths, optional): A list of temporary-directory pathnames to be created as the working-directory
                of each function. Useful if the function creates temporary files. These directories will be created, and then
                destroyed with the function exits, so don't list any directories that contain other data you may need.
                Directories must be in a parent directory that already exists.
        overwrite_outfiles (bool): If output files (listed in outfiles) already exist, delete them and overwrite. Otherwise,
                skip processes in which outfiles already exist.
        abbreviate_outfile_names_in_stdout (bool): Abbreviate the outfile names to the filename only (omit the path) for
                 brevity of output messages.
        verbose (bool): Print output message for each file created or process executed.
    """

    # For each optional list, just supply a range of integers if we're not using it. Check for integers later down and
    # ignore them.
    if kwargs_list is None:
        kwargs_list = [None] * len(args_lists)
    elif type(kwargs_list) == dict:
        kwargs_list = [kwargs_list] * len(args_lists)
    elif len(kwargs_list) != len(args_lists):
        raise ValueError("Length of kwargs_list ({0}) != length of args_lists ({1}). Exiting".format(len(kwargs_list),
                                                                                                     len(args_lists)))

    if outfiles is None:
        outfiles = range(len(args_lists))
    elif len(outfiles) != len(args_lists):
        raise ValueError("Length of outfiles ({0}) != length of args_lists ({1}). Exiting".format(len(outfiles),
                                                                                                  len(args_lists)))

    if temp_working_dirs is None:
        temp_working_dirs = range(len(args_lists))
    elif len(temp_working_dirs) != len(args_lists):
        raise ValueError("Length of temp_working_dirs ({0}) != length of args_lists ({1}). Exiting".format(len(temp_working_dirs),
                                                                                                    len(args_lists)))

    if proc_names is None:
        proc_names = range(len(args_lists))
    elif len(proc_names) != len(args_lists):
        raise ValueError("Length of proc_names ({0}) != length of args_lists ({1}). Exiting".format(len(proc_names),
                                                                                                    len(args_lists)))

    running_outfiles = []
    running_procs = []
    running_tempdirs = []
    running_procnames = []

    try:
        num_finished = 0
        for i, (args, kwargs, outfile, temp_dir, proc_name) in \
                enumerate(zip(args_lists, kwargs_list, outfiles, temp_working_dirs, proc_names)):

            if (outfile is not None) and os.path.exists(outfile):
                if overwrite_outfiles:
                    os.remove(outfile)

            process_started = False
            # Keep looping as long as (a) the process we've iterated to hasn't started yet, or
            #                         (b) we're at the end and we haven't finished executing all the other processes yet.
            while (not process_started) or ((i+1 == len(args_lists)) and (len(running_procs) > 0)):
                # First, loop through all the running processes and see if we need to do anything.
                procs_to_remove = []
                outfiles_to_check = []
                tempdirs_to_remove = []
                procnames_to_remove = []

                # First, check to see if any processes are finished. If so, add them to the list of ones to handle and remove.
                for r_proc, r_outf, r_tdir, r_pname in zip(running_procs,
                                                           running_outfiles,
                                                           running_tempdirs,
                                                           running_procnames):
                    if not r_proc.is_alive():
                        r_proc.join()
                        r_proc.close()
                        procs_to_remove.append(r_proc)
                        outfiles_to_check.append(r_outf)
                        tempdirs_to_remove.append(r_tdir)
                        procnames_to_remove.append(r_pname)

                # Remove any processes and other process metadata that has finished.
                for d_proc, d_outf, d_tdir, d_pname in zip(procs_to_remove,
                                                           outfiles_to_check,
                                                           tempdirs_to_remove,
                                                           procnames_to_remove):
                    num_finished += 1
                    # Print a confirmation line if we've asked it to. Either confirm:
                    # (a) the file has been written,
                    # (b) the process name has completed,
                    # (c) or just a count using the progress bar.
                    if verbose:
                        if use_progress_bar_only:
                            progress_bar.ProgressBar(i + 1, len(args_lists),
                                                     suffix="{0:,}/{1:,}".format(num_finished,
                                                     len(args_lists)))
                        elif type(d_outf) == str:
                            print("{0:,}/{1:,} ".format(num_finished, len(args_lists)), end="")
                            written_qualifier = "" if os.path.exists(d_outf) else "NOT "
                            print(os.path.basename(d_outf) if abbreviate_outfile_names_in_stdout else d_outf,
                                  "{0}written.".format(written_qualifier))
                        elif type(d_pname) == str:
                            print("{0:,}/{1:,} ".format(num_finished, len(args_lists)), end="")
                            print(d_pname, "finished.")
                        else:
                            # If we've given no identifying information for the processes, either a file to check
                            # or a process name, just output a progress bar.
                            progress_bar.ProgressBar(i + 1, len(args_lists),
                                                     suffix="{0:,}/{1:,}".format(num_finished,
                                                     len(args_lists)))

                    # Delete the temporary directory if it was created.
                    if type(d_tdir) == str and os.path.exists(d_tdir):
                        rm_cmd = ["rm", "-rf", d_tdir]
                        subprocess.run(rm_cmd, capture_output=True)

                    running_procs.remove(d_proc)
                    running_outfiles.remove(d_outf)
                    running_tempdirs.remove(d_tdir)
                    running_procnames.remove(d_pname)

                if (not process_started) and len(running_procs) < max_nprocs:
                    if type(outfile) == str and os.path.exists(outfile):
                        if not overwrite_outfiles:
                            num_finished += 1
                            if verbose:
                                print("{0:,}/{1:,} ".format(num_finished, len(args_lists)), end="")
                                print(os.path.basename(outfile) if abbreviate_outfile_names_in_stdout else outfile,
                                      "already exists.")
                            process_started = True
                            continue

                    if kwargs is not None:
                        proc = mp.Process(target=target_func,
                                          name=proc_name if (type(proc_name) == str) else None,
                                          args=args,
                                          kwargs=kwargs,
                                          )
                    else:
                        proc = mp.Process(target=target_func,
                                          name=proc_name if (type(proc_name) == str) else None,
                                          args=args,
                                          )

                    if type(temp_dir) == str and not os.path.exists(temp_dir):
                        os.mkdir(temp_dir)

                    running_procs.append(proc)
                    running_outfiles.append(outfile)
                    running_tempdirs.append(temp_dir)
                    running_procnames.append(proc_name)

                    # Since (annoyingly), multiprocessing does not have a "cwd=" keyword like subprocess,
                    # we can simply change the directory of the parent process (temporarily), and then change it back
                    # after starting the funciton.
                    old_cwd = None
                    if type(temp_dir) == str:
                        old_cwd = os.getcwd()
                        os.chdir(temp_dir)
                    proc.start()
                    # Then, change it back to the old one so we stay where we were.
                    if type(temp_dir) == str:
                        os.chdir(old_cwd)

                    process_started = True
                else:
                    # To keep the process from eating CPU, just rest for a tiny fraction of a second here before iterating again.
                    # It's not long enough a time for us to notice, but it's long enough to significantly reduce CPU usage
                    # by this parent process.
                    time.sleep(0.001)

    # If this process crashes or is keyboard-interrupted,
    # clean up the tempdirs and running procs, then re-raise the error to be handled elsewhere.
    except (Exception, KeyboardInterrupt) as e:
        # Kill any running processes.
        for rproc in running_procs:
            rproc.kill()
            rproc.close()
        # Delete all the temp directories we'd created.
        for tdir in running_tempdirs:
            if type(tdir) == str and os.path.exists(tdir):
                rm_cmd = ["rm", "-rf", tdir]
                subprocess.run(rm_cmd, capture_output=True)
        if delete_partially_done_files:
            for fn in running_outfiles:
                if type(fn) == str and os.path.exists(fn):
                    os.remove(fn)
        raise e

    return
