"""Code for a persistent process that checks to makes sure an server_job_manager.py is running.

This module gets run as a "nohup" process by server_proc.sh."""

import server_job_manager
import argparse
import os
import psutil
import subprocess
import time


class PersistentIvertServer:
    def __init__(self, verbose: bool = False):
        self.subproc = None
        self.verbose = verbose
        self.sub_pid = None

    def run(self):

        while True:
            if self.subproc is None:
                existing_proc = server_job_manager.is_another_manager_running()
                if existing_proc:
                    # If another instance of server_job_manager.py is already running, keep it going.
                    self.subproc = existing_proc
                    self.sub_pid = existing_proc.pid
                    if self.verbose:
                        print(
                            f"Another instance of server_job_manager.py (pid {self.sub_pid}) is already running. Will keep it going."
                        )
                    continue

                else:
                    # If no other instance of server_job_manager.py is running, start one.
                    iv_args = ["python3", "server_job_manager.py"]
                    if self.verbose:
                        iv_args.append("-v")
                        print(" ".join(iv_args))

                    self.subproc = subprocess.Popen(
                        iv_args, cwd=os.path.dirname(__file__)
                    )
                    self.sub_pid = self.subproc.pid

            # Handle if the process goes down.
            if (
                isinstance(self.subproc, psutil.Process) and self.subproc.is_running()
            ) or (
                isinstance(self.subproc, subprocess.Popen)
                and self.subproc.poll() is None
            ):
                if self.sub_pid != self.subproc.pid:
                    if self.verbose:
                        print(
                            f"Subprocess {self.sub_pid} has apparently restarted. Reassigning sub_pid."
                        )

                    self.sub_pid = self.subproc.pid

            else:
                if self.verbose:
                    print(
                        f"Subprocess {self.sub_pid} terminated. Restarting server_job_manager.py."
                    )
                self.subproc = None

            # Wait 5 seconds before checking again.
            time.sleep(5)
            continue

    def __del__(self):
        if self.subproc is not None:
            self.subproc.kill()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-v",
        "--verbose",
        dest="verbose",
        action="store_true",
        default=False,
        help="Print verbose output",
    )
    args = parser.parse_args()

    # If for whatever reason the .run() process breaks, just restart it and keep doing that forever.
    while True:
        try:
            PersistentIvertServer(verbose=args.verbose).run()

        except KeyboardInterrupt:
            break

        except:
            pass
