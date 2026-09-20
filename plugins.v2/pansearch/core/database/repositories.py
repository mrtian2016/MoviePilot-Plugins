"""PanSearch 业务数据 Repository。"""

import copy
import hashlib
import json
import re
from collections import defaultdict
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import Integer, case, cast, delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .manager import PanSearchDatabaseManager, DbOper, db_query, db_update
from .models import (
    AccountSnapshot,
    AuthSession,
    CheckinHistoryRecord,
    CheckinScheduleState,
    HistoryRecord,
    OfflinePendingTask,
    PointBudgetRecord,
)
from ..history import history_group_key
from ...search.types import normalize_resource_type, resource_type_from_url


def _positive_int(value: Any) -> Optional[int]:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _episode_number(record: Dict[str, Any]) -> Optional[int]:
    episode = _positive_int(record.get("episode"))
    if episode:
        return episode
    values = record.get("target_episodes")
    candidates = (
        values
        if isinstance(values, (list, tuple, set))
        else re.findall(r"\d+", str(values or ""))
    )
    episodes = [
        number
        for value in candidates
        if (number := _positive_int(value)) is not None
    ]
    return max(episodes, default=None)


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _timestamp(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


class HistoryRepository(DbOper):
    def __init__(self, manager: PanSearchDatabaseManager, db: Session = None):
        super().__init__(manager, db)
        self._filter_options_lock = RLock()
        self._filter_options: Optional[Dict[str, List[str]]] = None

    def _load_filter_options(self, db: Session) -> Dict[str, List[str]]:
        with self._filter_options_lock:
            if self._filter_options is None:
                rows = db.execute(select(
                    HistoryRecord.resource_type,
                    HistoryRecord.source,
                ).distinct()).all()
                self._filter_options = {
                    "resource_types": sorted({
                        row.resource_type for row in rows if row.resource_type
                    }),
                    "sources": sorted({
                        row.source for row in rows if row.source
                    }),
                }
            return copy.deepcopy(self._filter_options)

    def _invalidate_filter_options(self) -> None:
        with self._filter_options_lock:
            self._filter_options = None

    @staticmethod
    def _record_id(record: Dict[str, Any], index: int) -> str:
        record_id = str(record.get("record_id") or "").strip()
        if record_id:
            return record_id
        identity = "\0".join(str(record.get(key) or "") for key in (
            "share_url", "file_name", "tmdb_id", "season", "episode",
        ))
        if identity.strip("\0"):
            return hashlib.sha1(identity.encode("utf-8")).hexdigest()
        return hashlib.sha1(
            f"{_canonical_digest(record)}:{index}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _source(record: Dict[str, Any]) -> str:
        source = str(record.get("source") or "unknown").strip().lower()
        return "manual" if source in {"manual", "手动添加", "手动资源"} else source

    @staticmethod
    def _resource_type(record: Dict[str, Any]) -> str:
        return (
                normalize_resource_type(
                    record.get("resource_type") or record.get("pan_type") or ""
                )
                or resource_type_from_url(record.get("share_url"))
                or "unknown"
        )

    @staticmethod
    def _task_types(record: Dict[str, Any]) -> List[str]:
        values = {
            str(value or "").strip().lower()
            for value in (record.get("task_types") or [])
            if str(value or "").strip()
        }
        if str(record.get("transfer_mode") or "").strip().lower() == "cross":
            values.add("cross_transfer")
        upgrade = record.get("upgrade")
        if upgrade is True or str(upgrade or "").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            values.add("upgrade")
        return sorted(values)

    @classmethod
    def normalize(cls, record: Dict[str, Any], index: int) -> Dict[str, Any]:
        payload = copy.deepcopy(record)
        record_id = cls._record_id(payload, index)
        payload["record_id"] = record_id
        group_key = history_group_key(payload)
        task_types = cls._task_types(payload)
        return {
            "record_id": record_id,
            "sort_index": index,
            "group_key": group_key,
            "record_time": str(payload.get("time") or ""),
            "status": str(payload.get("status") or ""),
            "resource_type": cls._resource_type(payload),
            "source": cls._source(payload),
            "task_types": "|" + "|".join(task_types) + "|",
            "title": str(payload.get("title") or ""),
            "file_name": str(payload.get("file_name") or ""),
            "media_type": str(payload.get("type") or ""),
            "tmdb_id": str(payload.get("tmdb_id") or ""),
            "season": _positive_int(payload.get("season")),
            "episode": _episode_number(payload),
            "subscribe_id": _positive_int(payload.get("subscribe_id")),
            "payload": payload,
        }

    @classmethod
    def normalize_records(
            cls, records: Iterable[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        normalized = []
        used_ids = set()
        for index, value in enumerate(records or []):
            if not isinstance(value, dict):
                continue
            item = cls.normalize(value, index)
            if item["record_id"] in used_ids:
                item["record_id"] = hashlib.sha1(
                    f"{item['record_id']}:{index}".encode("utf-8")
                ).hexdigest()
                item["payload"]["record_id"] = item["record_id"]
            used_ids.add(item["record_id"])
            normalized.append(item)
        return normalized

    @db_update
    def replace_all(
            self, records: Iterable[Dict[str, Any]], db: Session = None
    ) -> int:
        normalized = self.normalize_records(records)
        current = {
            row.record_id: row
            for row in db.scalars(select(HistoryRecord)).all()
        }
        desired_ids = {item["record_id"] for item in normalized}
        for record_id in current.keys() - desired_ids:
            db.delete(current[record_id])
        for item in normalized:
            row = current.get(item["record_id"])
            if row is None:
                HistoryRecord(**item).create(db)
                continue
            changes = {
                key: copy.deepcopy(value)
                for key, value in item.items()
                if getattr(row, key) != value
            }
            if changes:
                row.update(db, changes)
        self._invalidate_filter_options()
        return len(normalized)

    @db_update
    def update_status_by_finalize_keys(
            self,
            finalize_keys: Iterable[str],
            status: str,
            reason: str = "",
            db: Session = None,
    ) -> List[Dict[str, Any]]:
        """按 finalize_key 定向更新状态，返回更新后的记录副本。

        v1.5.14：历史实现走 ``list_all`` + ``replace_all`` —— 一次状态变更
        要拉出全部历史行、逐字段深比较、再整体写回。该方法位于后处理监控
        的锁内（``_offline_pending_lock``），历史行数一多就把监控器、前端
        刷新 API、后续调度 tick 全部堵在锁上，表现为「后处理长期卡住且
        页面很慢」。这里改为只 UPDATE 命中行，复杂度从 O(全部历史) 降为
        O(命中行数)。
        """
        keys = {
            str(value or "").strip() for value in finalize_keys
            if str(value or "").strip()
        }
        if not keys:
            return []

        changed_rows: List[HistoryRecord] = []
        rows = db.scalars(
            select(HistoryRecord).order_by(HistoryRecord.sort_index)
        ).all()
        for row in rows:
            payload = row.payload or {}
            item_key = str(
                payload.get("finalize_key") or row.record_id or ""
            ).strip()
            if item_key not in keys:
                continue

            new_payload = copy.deepcopy(payload)
            new_payload["status"] = status
            new_payload.pop("finalize_key", None)
            if status == "失败":
                # 失败终态必须携带原因：优先本次原因，退回历史已有原因，
                # 兜底占位文案，绝不允许失败记录无原因（v1.5.3 T3）。
                new_payload["failure_reason"] = (
                        str(reason or "").strip()
                        or str(payload.get("failure_reason") or "").strip()
                        or "未提供失败原因"
                )
            elif reason:
                new_payload["failure_reason"] = reason
            else:
                new_payload.pop("failure_reason", None)

            changes = {"payload": new_payload, "status": status}
            row.update(db, changes)
            changed_rows.append(row)

        self._invalidate_filter_options()
        return [copy.deepcopy(row.payload) for row in changed_rows]

    @db_update
    def repair_group_keys(self, db: Session = None) -> int:
        """幂等回填旧记录的媒体分组键。"""
        repaired = 0
        for row in db.scalars(select(HistoryRecord)).all():
            expected = history_group_key(row.payload or {})
            if row.group_key == expected:
                continue
            row.update(db, {"group_key": expected})
            repaired += 1
        return repaired

    @db_query
    def list_all(self, db: Session = None) -> List[Dict[str, Any]]:
        rows = db.scalars(
            select(HistoryRecord).order_by(HistoryRecord.sort_index)
        ).all()
        return [copy.deepcopy(row.payload) for row in rows]

    @staticmethod
    def _filters(
            keyword: str = "",
            resource_types: Iterable[str] = (),
            sources: Iterable[str] = (),
            task_types: Iterable[str] = (),
            statuses: Iterable[str] = (),
    ) -> list:
        filters = []
        normalized_resource_types = {
            str(value).strip().lower() for value in resource_types if str(value).strip()
        }
        normalized_sources = {
            str(value).strip().lower() for value in sources if str(value).strip()
        }
        normalized_statuses = {
            str(value).strip().lower() for value in statuses if str(value).strip()
        }
        normalized_task_types = {
            str(value).strip().lower() for value in task_types if str(value).strip()
        }
        if normalized_resource_types:
            filters.append(HistoryRecord.resource_type.in_(normalized_resource_types))
        if normalized_sources:
            filters.append(HistoryRecord.source.in_(normalized_sources))
        if normalized_statuses:
            filters.append(func.lower(HistoryRecord.status).in_(normalized_statuses))
        if normalized_task_types:
            filters.append(or_(*[
                HistoryRecord.task_types.like(f"%|{value}|%")
                for value in normalized_task_types
            ]))
        normalized_keyword = str(keyword or "").strip().lower()
        if normalized_keyword:
            pattern = f"%{normalized_keyword}%"
            filters.append(or_(
                func.lower(HistoryRecord.title).like(pattern),
                func.lower(HistoryRecord.file_name).like(pattern),
                func.lower(HistoryRecord.media_type).like(pattern),
                func.lower(HistoryRecord.tmdb_id).like(pattern),
                func.lower(HistoryRecord.resource_type).like(pattern),
                func.lower(HistoryRecord.source).like(pattern),
            ))
        return filters

    @db_query
    def query_group_page(
            self,
            *,
            page: int,
            page_size: int,
            keyword: str = "",
            resource_types: Iterable[str] = (),
            sources: Iterable[str] = (),
            task_types: Iterable[str] = (),
            statuses: Iterable[str] = (),
            db: Session = None,
    ) -> Dict[str, Any]:
        filters = self._filters(
            keyword, resource_types, sources, task_types, statuses
        )
        normalized_page_size = min(50, max(1, int(page_size or 10)))
        filter_options = self._load_filter_options(db)
        latest_time = func.max(HistoryRecord.record_time).label("latest_time")
        success_count = func.sum(case(
            (HistoryRecord.status == "成功", 1), else_=0
        )).label("success_count")
        pending_count = func.sum(case(
            (HistoryRecord.status.in_(["处理中", "下载中"]), 1), else_=0
        )).label("pending_count")
        failed_count = func.sum(case(
            (HistoryRecord.status == "成功", 0),
            (HistoryRecord.status.in_(["处理中", "下载中"]), 0),
            else_=1,
        )).label("failed_count")
        total_size = func.coalesce(func.sum(cast(
            func.json_extract(HistoryRecord.payload, "$.file_size"), Integer
        )), 0).label("total_size")
        total_groups = func.count().over().label("total_groups")
        grouped = select(
            HistoryRecord.group_key,
            latest_time,
            success_count,
            pending_count,
            failed_count,
            total_size,
            total_groups,
        ).where(*filters).group_by(HistoryRecord.group_key)

        requested_page = max(1, int(page or 1))

        def load_groups(page_number: int):
            return db.execute(
                grouped.order_by(latest_time.desc(), HistoryRecord.group_key)
                .offset((page_number - 1) * normalized_page_size)
                .limit(normalized_page_size)
            ).all()

        group_rows = load_groups(requested_page)
        if group_rows:
            total = int(group_rows[0].total_groups or 0)
        elif requested_page > 1:
            total = int(db.scalar(
                select(func.count()).select_from(grouped.subquery())
            ) or 0)
        else:
            total = 0
        total_pages = max(
            1, (total + normalized_page_size - 1) // normalized_page_size
        )
        normalized_page = min(requested_page, total_pages)
        if normalized_page != requested_page:
            group_rows = load_groups(normalized_page)
        group_keys = [row.group_key for row in group_rows]
        records_by_group = defaultdict(list)
        if group_keys:
            rows = db.scalars(
                select(HistoryRecord)
                .where(HistoryRecord.group_key.in_(group_keys), *filters)
                .order_by(
                    HistoryRecord.season.desc(),
                    HistoryRecord.episode.desc(),
                    HistoryRecord.record_time.desc(),
                    HistoryRecord.sort_index.desc(),
                )
            ).all()
            for row in rows:
                payload = copy.deepcopy(row.payload)
                payload["record_id"] = row.record_id
                records_by_group[row.group_key].append(payload)
        groups = [{
            "group_key": row.group_key,
            "latest_time": str(row.latest_time or ""),
            "success_count": int(row.success_count or 0),
            "pending_count": int(row.pending_count or 0),
            "failed_count": int(row.failed_count or 0),
            "total_size": int(row.total_size or 0),
            "records": records_by_group.get(row.group_key, []),
        } for row in group_rows]
        return {
            "groups": groups,
            "page": normalized_page,
            "page_size": normalized_page_size,
            "total": total,
            "total_pages": total_pages,
            "filter_options": {
                "resource_types": filter_options["resource_types"],
                "sources": filter_options["sources"],
            },
        }

    @staticmethod
    def _summary(db: Session, today: str) -> Dict[str, int]:
        stats = db.execute(select(
            func.count(HistoryRecord.id),
            func.sum(case(
                (HistoryRecord.record_time.like(f"{today}%"), 1), else_=0
            )),
            func.sum(case((HistoryRecord.status == "成功", 1), else_=0)),
            func.sum(case((HistoryRecord.status == "失败", 1), else_=0)),
        )).one()
        return {
            "total": int(stats[0] or 0),
            "today": int(stats[1] or 0),
            "success": int(stats[2] or 0),
            "failed": int(stats[3] or 0),
        }

    @db_query
    def summary(
            self, today: str, db: Session = None
    ) -> Dict[str, int]:
        return self._summary(db, today)

    @db_query
    def overview(
            self, today: str, recent_limit: int = 20,
            db: Session = None,
    ) -> Dict[str, Any]:
        summary = self._summary(db, today)
        recent = db.scalars(
            select(HistoryRecord)
            .order_by(HistoryRecord.record_time.desc(), HistoryRecord.id.desc())
            .limit(max(0, min(int(recent_limit or 0), 20)))
        ).all()
        return {
            **summary,
            "recent": [copy.deepcopy(row.payload) for row in recent],
        }


class OfflinePendingRepository(DbOper):

    @staticmethod
    def normalize_values(values: Dict[str, Any]) -> Dict[str, Any]:
        return {
            str(key): copy.deepcopy(value)
            for key, value in (values or {}).items()
            if str(key)
        }

    @db_update
    def replace_all(self, values: Dict[str, Any], db: Session = None) -> int:
        normalized = self.normalize_values(values)
        current = {
            row.pending_key: row
            for row in db.scalars(select(OfflinePendingTask)).all()
        }
        for key in current.keys() - normalized.keys():
            db.delete(current[key])
        for key, payload in normalized.items():
            task_id = str(payload.get("task_id") or "") \
                if isinstance(payload, dict) else ""
            status = str(payload.get("status") or "") \
                if isinstance(payload, dict) else ""
            created_at = int(payload.get("created_at") or 0) \
                if isinstance(payload, dict) else 0
            row = current.get(key)
            if row is None:
                OfflinePendingTask(
                    pending_key=key,
                    task_id=task_id,
                    status=status,
                    created_at=created_at,
                    payload=payload,
                ).create(db)
            else:
                values = {
                    "task_id": task_id,
                    "status": status,
                    "created_at": created_at,
                    "payload": payload,
                }
                changes = {
                    field: copy.deepcopy(value)
                    for field, value in values.items()
                    if getattr(row, field) != value
                }
                if changes:
                    row.update(db, changes)
        return len(normalized)

    @db_query
    def load_all(self, db: Session = None) -> Dict[str, Any]:
        rows = db.scalars(select(OfflinePendingTask)).all()
        return {row.pending_key: copy.deepcopy(row.payload) for row in rows}


class CheckinRepository(DbOper):

    @staticmethod
    def _id(provider: str, record: Dict[str, Any], index: int) -> str:
        return str(record.get("id") or "").strip() or hashlib.sha1(
            f"{provider}:{record.get('executed_at')}:{_canonical_digest(record)}:{index}".encode("utf-8")
        ).hexdigest()

    @classmethod
    def normalize_records(
            cls, provider: str, records: Iterable[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        normalized = []
        for index, item in enumerate(records or []):
            if not isinstance(item, dict):
                continue
            record = copy.deepcopy(item)
            record["id"] = cls._id(provider, record, index)
            normalized.append(record)
        return normalized

    @db_update
    def replace_provider(
            self, provider: str, records: Iterable[Dict[str, Any]],
            db: Session = None,
    ) -> int:
        provider = str(provider or "").strip().lower()
        normalized = self.normalize_records(provider, records)

        current = {
            row.id: row
            for row in db.scalars(select(CheckinHistoryRecord).where(
                CheckinHistoryRecord.provider == provider
            )).all()
        }
        desired_ids = {record["id"] for record in normalized}
        for record_id in current.keys() - desired_ids:
            db.delete(current[record_id])
        for index, record in enumerate(normalized):
            record_id = record["id"]
            values = {
                "provider": provider,
                "sort_index": index,
                "executed_at": str(record.get("executed_at") or ""),
                "success": bool(record.get("success")),
                "payload": record,
            }
            row = current.get(record_id)
            if row is None:
                CheckinHistoryRecord(id=record_id, **values).create(db)
                continue
            changes = {
                key: copy.deepcopy(value)
                for key, value in values.items()
                if getattr(row, key) != value
            }
            if changes:
                row.update(db, changes)
        return len(normalized)

    @db_query
    def list_provider(
            self, provider: str, db: Session = None
    ) -> List[Dict[str, Any]]:
        rows = db.scalars(
            select(CheckinHistoryRecord)
            .where(CheckinHistoryRecord.provider == provider)
            .order_by(CheckinHistoryRecord.sort_index)
        ).all()
        return [copy.deepcopy(row.payload) for row in rows]

    @db_query
    def load_all(
            self, db: Session = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        rows = db.scalars(
            select(CheckinHistoryRecord).order_by(
                CheckinHistoryRecord.provider,
                CheckinHistoryRecord.sort_index,
            )
        ).all()
        for row in rows:
            grouped[row.provider].append(copy.deepcopy(row.payload))
        return dict(grouped)


class CheckinScheduleRepository(DbOper):

    @staticmethod
    def normalize(value: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(value or {})
        return {
            "date": str(data.get("date") or ""),
            "full_completed": bool(data.get("full_completed")),
            "retry_count": int(data.get("retry_count") or 0),
            "pending_providers": [
                str(item) for item in (data.get("pending_providers") or [])
                if str(item)
            ],
            "completed_retry_count": int(
                data.get("completed_retry_count") or 0
            ),
        }

    @db_update
    def save(self, value: Dict[str, Any], db: Session = None) -> None:
        data = self.normalize(value)
        values = {
            "id": 1,
            "schedule_date": str(data.get("date") or ""),
            "full_completed": bool(data.get("full_completed")),
            "retry_count": int(data.get("retry_count") or 0),
            "pending_providers": list(data.get("pending_providers") or []),
            "completed_retry_count": int(
                data.get("completed_retry_count") or 0
            ),
        }
        statement = sqlite_insert(CheckinScheduleState).values(**values)
        db.execute(statement.on_conflict_do_update(
            index_elements=[CheckinScheduleState.id],
            set_={
                key: getattr(statement.excluded, key)
                for key in values if key != "id"
            },
        ))

    @db_query
    def load(self, db: Session = None) -> Dict[str, Any]:
        row = CheckinScheduleState.get(db, 1)
        if row is None:
            return {}
        return {
            "date": row.schedule_date,
            "full_completed": bool(row.full_completed),
            "retry_count": int(row.retry_count or 0),
            "pending_providers": list(row.pending_providers or []),
            "completed_retry_count": int(row.completed_retry_count or 0),
        }


class PointBudgetRepository(DbOper):

    @staticmethod
    def normalize_values(values: Dict[str, Any]) -> Dict[str, int]:
        normalized = {}
        for key, value in (values or {}).items():
            normalized_key = str(key)
            if not normalized_key:
                continue
            try:
                normalized[normalized_key] = max(0, int(value or 0))
            except (TypeError, ValueError):
                continue
        return normalized

    @db_update
    def replace_provider(
            self, provider: str, values: Dict[str, Any], db: Session = None
    ) -> int:
        provider = str(provider or "").strip().lower()
        normalized = self.normalize_values(values)
        current = {
            row.subscribe_key: row
            for row in db.scalars(select(PointBudgetRecord).where(
                PointBudgetRecord.provider == provider
            )).all()
        }
        for key in current.keys() - normalized.keys():
            db.delete(current[key])
        for key, points in normalized.items():
            row = current.get(key)
            if row is None:
                PointBudgetRecord(
                    provider=provider,
                    subscribe_key=key,
                    points=points,
                ).create(db)
            elif int(row.points or 0) != points:
                row.update(db, {"points": points})
        return len(normalized)

    @db_query
    def load_provider(
            self, provider: str, db: Session = None
    ) -> Dict[str, int]:
        provider = str(provider or "").strip().lower()
        rows = db.scalars(select(PointBudgetRecord).where(
            PointBudgetRecord.provider == provider
        )).all()
        return {row.subscribe_key: int(row.points or 0) for row in rows}

    @db_query
    def load_all(
            self, db: Session = None
    ) -> Dict[str, Dict[str, int]]:
        grouped: Dict[str, Dict[str, int]] = defaultdict(dict)
        rows = db.scalars(select(PointBudgetRecord).order_by(
            PointBudgetRecord.provider,
            PointBudgetRecord.subscribe_key,
        )).all()
        for row in rows:
            grouped[row.provider][row.subscribe_key] = int(row.points or 0)
        return {provider: dict(values) for provider, values in grouped.items()}


class AccountSnapshotRepository(DbOper):

    @staticmethod
    def normalize_values(values: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {
            str(key).strip().lower(): copy.deepcopy(value)
            for key, value in (values or {}).items()
            if str(key).strip() and isinstance(value, dict) and value
        }

    @db_update
    def replace_all(self, values: Dict[str, Any], db: Session = None) -> int:
        normalized = self.normalize_values(values)
        current = {
            row.account_key: row
            for row in db.scalars(select(AccountSnapshot)).all()
        }
        for account_key in current.keys() - normalized.keys():
            db.delete(current[account_key])
        for account_key, payload in normalized.items():
            values = {
                "refreshed_at": _timestamp(payload.get("refreshed_at")),
                "payload": payload,
            }
            row = current.get(account_key)
            if row is None:
                AccountSnapshot(
                    account_key=account_key,
                    **values,
                ).create(db)
            else:
                changes = {
                    field: copy.deepcopy(value)
                    for field, value in values.items()
                    if getattr(row, field) != value
                }
                if changes:
                    row.update(db, changes)
        return len(normalized)

    @db_update
    def save_account(
            self, account_key: str, value: Any, db: Session = None
    ) -> None:
        normalized_key = str(account_key or "").strip().lower()
        if not normalized_key:
            raise ValueError("账户快照 account_key 不能为空")
        payload = copy.deepcopy(value) if isinstance(value, dict) and value else {}

        if not payload:
            db.execute(delete(AccountSnapshot).where(
                AccountSnapshot.account_key == normalized_key
            ))
            return
        values = {
            "account_key": normalized_key,
            "refreshed_at": _timestamp(payload.get("refreshed_at")),
            "payload": payload,
        }
        statement = sqlite_insert(AccountSnapshot).values(**values)
        db.execute(statement.on_conflict_do_update(
            index_elements=[AccountSnapshot.account_key],
            set_={
                "refreshed_at": statement.excluded.refreshed_at,
                "payload": statement.excluded.payload,
            },
        ))

    @db_query
    def load_account(
            self, account_key: str, db: Session = None
    ) -> Dict[str, Any]:
        normalized_key = str(account_key or "").strip().lower()
        row = AccountSnapshot.get(db, normalized_key)
        return copy.deepcopy(row.payload) if row else {}

    @db_query
    def load_all(self, db: Session = None) -> Dict[str, Dict[str, Any]]:
        return {
            row.account_key: copy.deepcopy(row.payload)
            for row in AccountSnapshot.list(db)
        }


class AuthSessionRepository(DbOper):

    @staticmethod
    def normalize_value(value: Any) -> Dict[str, Any]:
        return copy.deepcopy(value) if isinstance(value, dict) and value else {}

    @db_update
    def save_provider(
            self, provider: str, value: Any, db: Session = None
    ) -> None:
        normalized_provider = str(provider or "").strip().lower()
        if not normalized_provider:
            raise ValueError("认证会话 provider 不能为空")
        payload = self.normalize_value(value)

        if not payload:
            db.execute(delete(AuthSession).where(
                AuthSession.provider == normalized_provider
            ))
            return
        values = {
            "provider": normalized_provider,
            "updated_at": _timestamp(payload.get("updated_at")),
            "expires_at": _timestamp(payload.get("expires_at")),
            "payload": payload,
        }
        statement = sqlite_insert(AuthSession).values(**values)
        db.execute(statement.on_conflict_do_update(
            index_elements=[AuthSession.provider],
            set_={
                key: getattr(statement.excluded, key)
                for key in values if key != "provider"
            },
        ))

    @db_update
    def replace_all(
            self, values: Dict[str, Any], db: Session = None
    ) -> int:
        normalized = {}
        for provider, value in (values or {}).items():
            normalized_provider = str(provider).strip().lower()
            normalized_value = self.normalize_value(value)
            if normalized_provider and normalized_value:
                normalized[normalized_provider] = normalized_value

        current = {row.provider: row for row in AuthSession.list(db)}
        for provider in current.keys() - normalized.keys():
            db.delete(current[provider])
        for provider, payload in normalized.items():
            row = current.get(provider)
            if (
                    row is not None
                    and int(row.updated_at or 0)
                    == _timestamp(payload.get("updated_at"))
                    and int(row.expires_at or 0)
                    == _timestamp(payload.get("expires_at"))
                    and row.payload == payload
            ):
                continue
            self.save_provider(provider, payload, db=db)
        return len(normalized)

    @db_query
    def load_provider(
            self, provider: str, db: Session = None
    ) -> Dict[str, Any]:
        normalized_provider = str(provider or "").strip().lower()
        row = AuthSession.get(db, normalized_provider)
        return copy.deepcopy(row.payload) if row else {}

    @db_query
    def load_all(self, db: Session = None) -> Dict[str, Dict[str, Any]]:
        return {
            row.provider: copy.deepcopy(row.payload)
            for row in AuthSession.list(db)
        }


class PanSearchRepositories:
    def __init__(
            self,
            manager: PanSearchDatabaseManager,
            db: Session = None,
    ):
        self.history = HistoryRepository(manager, db)
        self.offline = OfflinePendingRepository(manager, db)
        self.checkin = CheckinRepository(manager, db)
        self.schedule = CheckinScheduleRepository(manager, db)
        self.budget = PointBudgetRepository(manager, db)
        self.account = AccountSnapshotRepository(manager, db)
        self.auth = AuthSessionRepository(manager, db)
