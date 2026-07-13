"""Database models."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Integer, String, Text, ForeignKey, UniqueConstraint, DateTime, BigInteger,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    user = "user"
    admin = "admin"


class AttemptStatus(str, enum.Enum):
    not_started = "not_started"
    in_progress = "in_progress"
    solved = "solved"
    abandoned = "abandoned"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=Role.user.value)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    sheets: Mapped[list["SolveSheet"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class PlatformConfig(Base):
    """Singleton row (id=1), admin-editable runtime config."""
    __tablename__ = "platform_config"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    registration_code: Mapped[str] = mapped_column(String(128))
    max_concurrent_per_user: Mapped[int] = mapped_column(Integer, default=3)
    public_base_url: Mapped[str] = mapped_column(String(256))
    allow_direct_port: Mapped[bool] = mapped_column(Boolean, default=True)


class SolveSheet(Base):
    __tablename__ = "solve_sheets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    api_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    user: Mapped["User"] = relationship(back_populates="sheets")
    attempts: Mapped[list["Attempt"]] = relationship(
        back_populates="sheet", cascade="all, delete-orphan"
    )


class Challenge(Base):
    __tablename__ = "challenges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    benchmark: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # XBEN-xxx-24
    title: Mapped[str | None] = mapped_column(String(256))
    level: Mapped[str | None] = mapped_column(String(8))
    tags: Mapped[str] = mapped_column(Text, default="")          # comma-joined
    win_condition: Mapped[str] = mapped_column(String(16), default="flag")
    supported: Mapped[bool] = mapped_column(Boolean, default=True)
    # flag injection metadata (from manifest)
    service: Mapped[str | None] = mapped_column(String(128))
    flag_type: Mapped[str | None] = mapped_column(String(16))   # file|env|embedded
    flag_path: Mapped[str | None] = mapped_column(String(256))
    original_flag: Mapped[str | None] = mapped_column(String(128))


class Attempt(Base):
    """One current attempt per (sheet, challenge). Carries the live state."""
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("sheet_id", "challenge_id", name="uq_sheet_challenge"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sheet_id: Mapped[int] = mapped_column(ForeignKey("solve_sheets.id", ondelete="CASCADE"), index=True)
    challenge_id: Mapped[int] = mapped_column(ForeignKey("challenges.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default=AttemptStatus.in_progress.value)
    dynamic_flag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    compose_project: Mapped[str | None] = mapped_column(String(128), nullable=True)
    host_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    solved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    solve_duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    sheet: Mapped["SolveSheet"] = relationship(back_populates="attempts")
