"""The message handler: decides how a delivery ends."""

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from tracksvc.application.errors import TrackNotFoundError
from tracksvc.application.messages import IsMessageProcessed
from tracksvc.application.ports import UnitOfWork
from tracksvc.application.tracks import ExtendTrack
from tracksvc.domain.errors import CapacityLimitExceededError, TrackSealedError
from tracksvc.worker.messages import Delivery, is_valid_message_id, parse_body, parse_retry_count

logger = logging.getLogger(__name__)

INVALID_MESSAGE = "invalid_message"
INTERNAL_ERROR = "internal_error"
_FAILURE_CODES: dict[type[Exception], str] = {
    TrackNotFoundError: "track_not_found",
    TrackSealedError: "track_sealed",
    CapacityLimitExceededError: "capacity_limit_exceeded",
}

type UnitOfWorkProvider = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


@dataclass(frozen=True, slots=True)
class Done:
    """The delivery needs nothing more: its effect is applied, now or by an earlier delivery."""


@dataclass(frozen=True, slots=True)
class Retry:
    """Processing failed and is to be attempted again; ``retry_count`` is the new counter."""

    retry_count: int


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """The message is given up with the code ``failure``.

    ``retry_count`` is the counter the dead copy carries; ``None`` leaves the headers of
    the message as they came.
    """

    failure: str
    retry_count: int | None = None


type Outcome = Done | Retry | DeadLetter


class MessageHandler:
    """Turn one delivery into its outcome, applying the extension through the use cases.

    The handler takes no broker action: whoever calls it publishes the copies the
    outcome asks for and acknowledges the delivery afterwards.
    """

    def __init__(self, unit_of_work: UnitOfWorkProvider, max_retries: int) -> None:
        self._unit_of_work = unit_of_work
        self._max_retries = max_retries

    async def handle(self, delivery: Delivery) -> Outcome:
        """Process ``delivery`` in its own unit of work and return how it ends.

        Checks come in this order: the message id, a duplicate, the rest of the message.
        Every failure of processing becomes an outcome; nothing is raised.
        """
        message_id = delivery.message_id
        if message_id is None or not is_valid_message_id(message_id):
            return DeadLetter(INVALID_MESSAGE)
        retry_count = parse_retry_count(delivery.headers)
        try:
            async with self._unit_of_work() as uow:
                if await IsMessageProcessed(uow).execute(message_id):
                    return Done()
                body = parse_body(delivery.body)
                if retry_count is None or body is None:
                    return DeadLetter(INVALID_MESSAGE)
                await ExtendTrack(uow).execute(message_id, body.track_id, body.delta)
                return Done()
        except tuple(_FAILURE_CODES) as error:
            failure = _FAILURE_CODES[type(error)]
        except Exception:
            logger.exception("Processing of message %r failed", message_id)
            failure = INTERNAL_ERROR
        return self._failed(failure, retry_count or 0)

    def _failed(self, failure: str, retry_count: int) -> Outcome:
        if retry_count < self._max_retries:
            return Retry(retry_count + 1)
        return DeadLetter(failure, retry_count)
