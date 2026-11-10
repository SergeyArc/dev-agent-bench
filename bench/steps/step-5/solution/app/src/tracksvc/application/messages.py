"""Use cases about the messages the service has processed."""

from tracksvc.application.ports import UnitOfWork


class IsMessageProcessed:
    """Tell whether the effect of a message has already been applied."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def execute(self, message_id: str) -> bool:
        """Return whether the message ``message_id`` is recorded as processed.

        Raises:
            StorageUnavailableError: If the storage fails.
        """
        async with self._uow:
            return await self._uow.processed_messages.contains(message_id)
