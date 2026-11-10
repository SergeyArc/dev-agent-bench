"""Process configuration, read from the environment when an entry point starts."""

import os

DATABASE_URL_VARIABLE = "DATABASE_URL"


class ConfigurationError(Exception):
    """A required environment variable is not set."""


def read_database_url() -> str:
    """Return the database URL the current process was started with.

    Raises:
        ConfigurationError: If ``DATABASE_URL`` is not set.
    """
    try:
        return os.environ[DATABASE_URL_VARIABLE]
    except KeyError:
        raise ConfigurationError(f"{DATABASE_URL_VARIABLE} is not set") from None
