"""PanSearch 独立 SQLite ORM 模型。"""

from typing import Any, Dict, List, Optional

from sqlalchemy import Boolean, Index, Integer, JSON, String, delete, inspect, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class PanSearchBase(DeclarativeBase):
    """私有库模型基类，提供与平台 Base 一致的通用 CRUD 能力。"""

    __abstract__ = True
    __allow_unmapped__ = True
    id: Any
    __name__: str

    def create(self, db: Session):
        db.add(self)
        return self

    @classmethod
    def get(cls, db: Session, identity: Any):
        return db.get(cls, identity)

    def update(self, db: Session, payload: Dict[str, Any]):
        for key, value in payload.items():
            setattr(self, key, value)
        if inspect(self).detached:
            db.add(self)
        return self

    @classmethod
    def delete(cls, db: Session, identity: Any) -> bool:
        row = db.get(cls, identity)
        if row is None:
            return False
        db.delete(row)
        return True

    @classmethod
    def truncate(cls, db: Session) -> None:
        db.execute(delete(cls))

    @classmethod
    def list(cls, db: Session):
        return list(db.scalars(select(cls)).all())

    def to_dict(self) -> Dict[str, Any]:
        return {
            column.name: getattr(self, column.name, None)
            for column in self.__table__.columns
        }


class HistoryRecord(PanSearchBase):
    __tablename__ = "history_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    sort_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    group_key: Mapped[str] = mapped_column(String(255), nullable=False)
    record_time: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    task_types: Mapped[str] = mapped_column(String(255), nullable=False, default="|")
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    file_name: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    media_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    tmdb_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    season: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    episode: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    subscribe_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_history_group_time", "group_key", "record_time"),
        Index(
            "ix_history_group_filter",
            "group_key", "status", "resource_type", "source",
        ),
        Index("ix_history_filter", "status", "resource_type", "source"),
        Index("ix_history_sort", "sort_index"),
    )


class OfflinePendingTask(PanSearchBase):
    __tablename__ = "offline_pending_tasks"

    pending_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_offline_task_id", "task_id"),
        Index("ix_offline_status", "status"),
    )


class CheckinHistoryRecord(PanSearchBase):
    __tablename__ = "checkin_history"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    sort_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    executed_at: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_checkin_provider_time", "provider", "executed_at"),
        Index("ix_checkin_provider_sort", "provider", "sort_index"),
    )


class CheckinScheduleState(PanSearchBase):
    __tablename__ = "checkin_schedule_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    schedule_date: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    full_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_providers: Mapped[List[str]] = mapped_column(JSON, default=list)
    completed_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PointBudgetRecord(PanSearchBase):
    __tablename__ = "point_budget_records"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    subscribe_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_point_budget_provider", "provider"),
    )


class AccountSnapshot(PanSearchBase):
    __tablename__ = "account_snapshots"

    account_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    refreshed_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)


class AuthSession(PanSearchBase):
    __tablename__ = "auth_sessions"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)
