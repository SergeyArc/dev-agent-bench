from collections.abc import Callable

import pytest

from tracksvc.config import (
    ConfigurationError,
    read_consumer_max_retries,
    read_database_url,
    read_rabbitmq_url,
)


def test_urls_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:1/name")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://u:p@broker:2/vhost")

    assert read_database_url() == "postgresql+asyncpg://u:p@db:1/name"
    assert read_rabbitmq_url() == "amqp://u:p@broker:2/vhost"


@pytest.mark.parametrize(
    ("variable", "read"), [("DATABASE_URL", read_database_url), ("RABBITMQ_URL", read_rabbitmq_url)]
)
def test_a_missing_url_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, variable: str, read: Callable[[], str]
) -> None:
    monkeypatch.delenv(variable, raising=False)

    with pytest.raises(ConfigurationError, match=variable):
        read()


def test_max_retries_defaults_to_three(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONSUMER_MAX_RETRIES", raising=False)

    assert read_consumer_max_retries() == 3


@pytest.mark.parametrize(("raw", "value"), [("0", 0), ("1", 1), ("3", 3), ("25", 25)])
def test_max_retries_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, raw: str, value: int
) -> None:
    monkeypatch.setenv("CONSUMER_MAX_RETRIES", raw)

    assert read_consumer_max_retries() == value


@pytest.mark.parametrize("raw", ["-1", "", "three", "1.5"])
def test_an_invalid_max_retries_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("CONSUMER_MAX_RETRIES", raw)

    with pytest.raises(ConfigurationError, match="CONSUMER_MAX_RETRIES"):
        read_consumer_max_retries()
