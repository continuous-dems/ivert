## A small utility for checking any IVERT jobs that are currently running.
# import argparse
import psutil

import jobs_database

def is_pid_an_active_ivert_job(pid: int) -> bool:
    """Check if the pid is an IVERT job.

    Checks whether 'ivert' is in the command (it should be in the path of our ivert conda environment) and 'python3' in the path.

    Args:
        pid (int): The pid to check.

    Returns:
        bool: True if the pid is an IVERT job, False otherwise."""
    try:
        proc = psutil.Process(pid)

        cmd_line_0 = proc.cmdline()[0]
        if "ivert" in cmd_line_0 and "python3" in cmd_line_0:
            return True
        else:
            return False

    except psutil.NoSuchProcess:
        return False


def list_running_ivert_jobs() -> list:
    """List all running IVERT jobs.

    Returns:
        list: A list of all running IVERT jobs.
    """
    jobs_db = jobs_database.JobsDatabaseServer()

    try:
        running_jobs = [job for job in jobs_db.list_unfinished_jobs(return_rows=True)
                       if is_pid_an_active_ivert_job(job['job_pid'])]
    except FileNotFoundError:
        return []

    return running_jobs


def are_any_ivert_jobs_running() -> bool:
    """Check if any IVERT jobs are still running.

    Returns:
        bool: True if any IVERT jobs are still running, False otherwise."""
    running_jobs = list_running_ivert_jobs()

    if len(running_jobs) > 0:
        return True
    else:
        return False

# If this is run from the command-line, list all the currently-running IVERT jobs on the server.
if __name__ == "__main__":
    jobs = list_running_ivert_jobs()
    print(["{0}_{1}".format(job['username'],job['job_id']) for job in jobs])
