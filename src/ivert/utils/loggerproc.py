
import io
import multiprocessing as mp
import sys
import typing

if vars(sys.modules[__name__])['__package__'] == 'ivert_utils':
    # When this is built a setup.py package, it names the modules 'ivert' and 'ivert_utils'. This reflects that.
    import ivert_utils.unformat_text as ut
else:
    try:
        import unformat_text as ut
    except ModuleNotFoundError:
        # If this script is imported from another module in the src/ directory, import this way.
        import utils.unformat_text as ut


class LoggerProc(mp.Process):
    """A class for creating and running a sub-process while logging stdout to a file.

    Inherits from multiprocessing.Process. Run just as you would a Process, but include a filename and an optional
    'include_terminal' argument. If True, print to the terminal as well as return in the logfile. If False, just write
    to the logfile (not to the terminal)."""

    def __init__(self,
                 target: callable,
                 filename_out: str,
                 args: typing.Union[typing.List, typing.Tuple, None] = None,
                 kwargs: typing.Union[typing.Dict, None] = None,
                 output_to_terminal: bool = False):

        self.filename = filename_out
        self.target = target

        if args is None:
            self.args = ()
        else:
            self.args = args
        if kwargs is None:
            self.kwargs = {}
        else:
            self.kwargs = kwargs

        self.output_to_terminal = output_to_terminal

        # The runproc method will be run as a sub-process and will call the
        # target function after setting up the Logger.
        super().__init__(target=self.runproc)

    def runproc(self):
        """Redirect outputs and run the logger sub-process.

        This is not called explicitly by the user. It is called by running the inherited mp.Process.start() method
        from this object (see the mp.Process setup at the end of the __init__ method).
        """
        # Assign a logger to stdout and stderr in this sub-process. Then call the target function.
        logger = Logger(filename=self.filename,
                        output_to_terminal=self.output_to_terminal)

        # Redirect this process's stdout and stderr to the logger.
        sys.stdout = logger
        sys.stderr = logger

        # Call target function directly with args & kwargs after redirecting stdout and stderr.
        self.target(*self.args, **self.kwargs)

        return


class Logger(io.TextIOWrapper):
    """A class for logging stdout to a file. Used by LoggerProc.

    Contains a "write" method that can act as an output stream/file.
    If output_to_termianl, this writes all output (including stderr) to the terminal's stdout as well as the logfile.

    At some point we may add support for logging stderr to a separate file."""

    def __init__(self,
                 filename: str,
                 output_to_terminal: bool = True):

        self.output_to_terminal = output_to_terminal
        if self.output_to_terminal:
            self.terminal_stdout = sys.stdout
        else:
            self.terminal_stdout = None

        self.filename_out = filename
        self.log = open(self.filename_out, "a", buffering=1)
        # Apparently this should have a flush attribute to be compatible with stdout print(flush=True) statements.

    def flush(self):
        """Flush the log and stdout."""
        if self.terminal_stdout:
            self.terminal_stdout.flush()
        self.log.flush()

    @staticmethod
    def isatty():
        """Return True if the log is a terminal."""
        return False

    def fileno(self):
        """Return the file descriptor for the log."""
        return self.log.fileno()

    def write(self, message):
        """Write a message to both the terminal (if selected) and the log.

        For the log, use unformatted text without ASCII escape codes or carriage returned-lines."""
        if self.terminal_stdout:
            self.terminal_stdout.write(message)

        # Write the message to the log. Flush the output.
        self.log.write(ut.unformat_and_delete_cr_lines(message))


def dummy_test():
    """Test this module."""
    import time
    import os

    def do_stuff(foobar, barfoo=None):
        """A dummy function that prints random crap to the screen, both to stdout and stderr (raising expection)."""
        print(foobar)
        time.sleep(1)
        for i in range(10):
            print(i, barfoo)
            time.sleep(1.25)
        print(foobar, "again")
        raise ValueError("Testing stderr too.")

    var1 = "hello"
    kwvar2 = "world"
    outfile = os.path.abspath(os.path.join("..", "scratch_data", "loggerproc_test_out.txt"))

    proc = LoggerProc(do_stuff, outfile, args=(var1,), kwargs={"barfoo": kwvar2}, output_to_terminal=False)
    proc.start()
    proc.join()

    print("Process 1 done! Now test with enabling terminal output.")

    proc = LoggerProc(do_stuff, outfile, args=(var1,), kwargs={"barfoo": kwvar2}, output_to_terminal=True)
    proc.start()
    proc.join()


if __name__ == "__main__":
    dummy_test()
