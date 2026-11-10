"""ORM models: the rows the service stores. Alembic revisions create these tables."""

from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, Identity, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base of the ORM models; its metadata describes the schema."""


class TrackRow(Base):
    """Row of ``tracks``: one per track."""

    __tablename__ = "tracks"
    __table_args__ = (UniqueConstraint("code", name="uq_tracks_code"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    code: Mapped[str] = mapped_column(String(32))
    capacity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))


class SegmentRow(Base):
    """Row of ``track_segments``: one per segment of a track."""

    __tablename__ = "track_segments"
    __table_args__ = (
        UniqueConstraint("track_id", "label", name="uq_track_segments_track_id_label"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    track_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tracks.id", name="fk_track_segments_track_id", ondelete="CASCADE")
    )
    label: Mapped[str] = mapped_column(String(16))
    span_start: Mapped[int] = mapped_column(Integer)
    span_end: Mapped[int] = mapped_column(Integer)


class ProcessedMessageRow(Base):
    """Row of ``processed_messages``: one per message whose effect has been applied."""

    __tablename__ = "processed_messages"

    message_id: Mapped[str] = mapped_column(String(255), primary_key=True)
