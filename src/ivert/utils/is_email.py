import re


def is_email(email: str) -> bool:
    """
    Check if a string is an email address.

    Just checks syntax, doesn't check if the email address actually exists on an SMTP server or points to the right person.

    Args:
        email (str): The email address to check.

    Returns:
        bool: True if the string is a syntactically valid email address, False otherwise.
    """
    return bool(re.match(r"[^@]+@[^@]+\.[^@]+", email))


def return_email(email: str) -> str:
    """
    Return the email address if it is syntactically valid.

    Args:
        email (str): The email address to check.

    Returns:
        str: The email address if it is syntactically valid.

    Raises:
        ValueError: If the email address is not syntactically valid.
    """
    # Strip any whitespace.
    email = email.strip()
    if is_email(email):
        return email
    else:
        raise ValueError(f"{email} is not a valid email address.")