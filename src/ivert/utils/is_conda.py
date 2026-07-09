import os


def is_conda():
    "Return True if running in an Anaconda Python environment. False otherwise."

    # Check for the CONDA_DEFAULT_ENV environment variable, which should be set in any conda python environment.
    # Most non-conda environments don't use this.
    # If we're in an AWS Lambda function, 'AWS_LAMBDA_FUNCTION_NAME' will be set in the environment.
    # Check for either of these.
    return os.environ.get('CONDA_DEFAULT_ENV') is not None or os.environ.get('AWS_LAMBDA_FUNCTION_NAME') is not None


if __name__ == "__main__":
    print(is_conda())
