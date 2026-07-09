import sys


def query_yes_no(question: str, default: str = "yes") -> bool:
    """Ask a yes/no question via raw_input() and return their answer.

    "question" is a string that is presented to the user.
    "default" is the presumed answer if the user just hits <Enter>.
            It must be "yes" (the default), "no" or None (meaning
            an answer is required of the user).

    The "answer" return value is True for "yes" or False for "no".
    """
    # valid = {"yes": True, "y": True, "ye": True, "no": False, "n": False}
    if default is None:
        prompt = " [y/n] "
    elif default.strip().lower() in ("yes", "y", "si", "s"):
        prompt = " [Y/n] "
    elif default.strip().lower() in ("no", "n"):
        prompt = " [y/N] "
    else:
        raise ValueError("invalid default answer: '%s'" % default)

    while True:
        sys.stdout.write(question + prompt)
        choice = input().strip()
        if default is not None and choice == "":
            return interpret_yes_no(default)
        else:
            try:
                return interpret_yes_no(choice)
            except ValueError:
                sys.stdout.write("Please respond with 'yes' or 'no' " "(or 'y' or 'n').\n")


def interpret_yes_no(input_str: str) -> bool:
    """Interpret a yes/no or true/false input string as a boolean."""
    instr = input_str.strip().lower()
    # "" or None will be interpreted as False
    if not instr:
        return False
    # Yes, True, Si, Y, T, S, etc will be intrepreted as True
    elif instr[0] in ("y", "t", "s"):
        return True
    # No, False, N, F, etc will be intrepreted as False
    elif instr[0] in ("n", "f"):
        return False
    # Anything else is invalid
    else:
        raise ValueError("invalid boolean input: '%s'" % input_str)