"""The unit of work over a database session."""

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession

from tracksvc.application.errors import StorageUnavailableError
from tracksvc.application.ports import TrackRepository
from tracksvc.infrastructure.database import DATABASE_FAILURES
from tracksvc.infrastructure.repositories import SqlAlchemyTrackRepository


class SqlAlchemyUnitOfWork:
    """One database transaction on the session it is given.

    The session belongs to whoever created it: the unit of work begins and ends the
    transaction and never closes the session. Driver and ORM errors raised inside the
    block, by the commit or by the rollback leave it as ``StorageUnavailableError``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._tracks = SqlAlchemyTrackRepository(session)

    @property
    def tracks(self) -> TrackRepository:
        """The tracks, read and written within this transaction."""
        return self._tracks

    async def __aenter__(self) -> Self:
        """Begin the transaction."""
        await self._session.begin()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back whatever has not been committed.

        Raises:
            StorageUnavailableError: If the block failed with a database error or the
                rollback fails.
        """
        try:
            await self._session.rollback()
        except DATABASE_FAILURES as error:
            raise StorageUnavailableError from error
        if isinstance(exc, DATABASE_FAILURES):
            raise StorageUnavailableError from exc

    async def commit(self) -> None:
        """Commit everything written in this transaction.

        Raises:
            StorageUnavailableError: If the database fails to commit.
        """
        try:
            await self._session.commit()
        except DATABASE_FAILURES as error:
            raise StorageUnavailableError from error
