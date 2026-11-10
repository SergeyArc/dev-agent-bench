"""Process configuration, read from the environment when an entry point starts."""

import os

DATABASE_URL_VARIABLE = "DATABASE_URL"
RABBITMQ_URL_VARIABLE = "RABBITMQ_URL"
CONSUMER_MAX_RETRIES_VARIABLE = "CONSUMER_MAX_RETRIES"
DEFAULT_CONSUMER_MAX_RETRIES = 3


class ConfigurationError(Exception):
    """A required environment variable is not set, or a variable has an invalid value."""


def _read_required(variable: str) -> str:
    try:
        return os.environ[variable]
    except KeyError:
        raise ConfigurationError(f"{variable} is not set") from None


def read_database_url() -> str:
    """Return the database URL the current process was started with.

    Raises:
        ConfigurationError: If ``DATABASE_URL`` is not set.
    """
    return _read_required(DATABASE_URL_VARIABLE)


def read_rabbitmq_url() -> str:
    """Return the broker URL the current process was started with.

    Raises:
        ConfigurationError: If ``RABBITMQ_URL`` is not set.
    """
    return _read_required(RABBITMQ_URL_VARIABLE)


def read_consumer_max_retries() -> int:
    """Return how many times a failing message is retried before it is dead-lettered.

    ``CONSUMER_MAX_RETRIES`` is optional and defaults to ``DEFAULT_CONSUMER_MAX_RETRIES``.

    Raises:
        ConfigurationError: If the variable is set to anything but an integer >= 0.
    """
    raw = os.environ.get(CONSUMER_MAX_RETRIES_VARIABLE)
    if raw is None:
        return DEFAULT_CONSUMER_MAX_RETRIES
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        raise ConfigurationError(
            f"{CONSUMER_MAX_RETRIES_VARIABLE} must be an integer >= 0, got {raw!r}"
        )
    return value
