"""PanSearch 独立 SQLite 引擎与会话生命周期。"""

from configparser import ConfigParser
from contextlib import contextmanager
from functools import wraps
from inspect import signature
from pathlib import Path
from threading import RLock
from typing import Callable, Generator, Optional

from alembic.config import Config as AlembicConfig
from app.core.config import settings
from app.log import logger
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from alembic import command
from .models import PanSearchBase


def _database_operation(*, write: bool):
    """参考平台 db_query/db_update，为私有数据库操作对象注入会话。"""

    def decorator(func: Callable):
        operation_signature = signature(func)
        if "db" not in operation_signature.parameters:
            raise TypeError(f"{func.__qualname__} 必须声明 db 参数")

        @wraps(func)
        def wrapper(self, *args, **kwargs):
            bound = operation_signature.bind_partial(self, *args, **kwargs)
            explicit_db = bound.arguments.get("db")
            active_db = (
                explicit_db
                if explicit_db is not None
                else getattr(self, "_db", None)
            )
            if active_db is not None:
                bound.arguments["db"] = active_db
                return func(*bound.args, **bound.kwargs)
            with self.manager.session(write=write) as db:
                bound.arguments["db"] = db
                return func(*bound.args, **bound.kwargs)

        return wrapper

    return decorator


db_query = _database_operation(write=False)
db_update = _database_operation(write=True)


class DbOper:
    """插件私有数据库操作基类，统一保存管理器和会话上下文。"""

    def __init__(
            self,
            manager: "PanSearchDatabaseManager",
            db: Session = None,
    ):
        self.manager = manager
        self._db = db


class PanSearchDatabaseManager:
    """管理插件私有数据库，不注册到 MoviePilot 主库 metadata。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = RLock()
        self._engine: Optional[Engine] = None
        self._session_factory: Optional[sessionmaker] = None

    @staticmethod
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(
                f"PRAGMA busy_timeout={max(1000, int(settings.DB_TIMEOUT * 1000))};"
            )
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.execute("PRAGMA temp_store=MEMORY;")
            cursor.execute("PRAGMA cache_size=-20000;")
        finally:
            cursor.close()

    def init_db(self) -> None:
        """按 ORM metadata 初始化新数据库和缺失表。"""
        self.open()
        if self._engine is None:
            raise RuntimeError("PanSearch 数据库引擎未初始化")
        PanSearchBase.metadata.create_all(bind=self._engine)

    def update_db(self) -> None:
        """按程序化 Alembic 配置和 revision 链升级已有数据库结构。"""
        self.open()
        if self._engine is None:
            raise RuntimeError("PanSearch 数据库引擎未初始化")
        script_location = Path(__file__).with_name("alembic")
        config = AlembicConfig()
        config.file_config = ConfigParser(interpolation=None)
        config.set_main_option("script_location", str(script_location))
        config.set_main_option(
            "sqlalchemy.url",
            self._engine.url.render_as_string(hide_password=False),
        )
        config.attributes["target_metadata"] = PanSearchBase.metadata
        command.upgrade(config, "head")

    def open(self) -> None:
        """只打开数据库引擎和会话，不执行建表或结构升级。"""
        with self._lock:
            if self._engine is not None:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            engine_options = {
                "url": f"sqlite:///{self.db_path}",
                "poolclass": QueuePool,
                "pool_pre_ping": False,
                "pool_size": max(1, min(
                    4, int(getattr(settings, "DB_SQLITE_POOL_SIZE", 4))
                )),
                "pool_timeout": getattr(settings, "DB_POOL_TIMEOUT", 30),
                "max_overflow": max(0, min(
                    2, int(getattr(settings, "DB_SQLITE_MAX_OVERFLOW", 2))
                )),
                "connect_args": {
                    "timeout": settings.DB_TIMEOUT,
                    "check_same_thread": False,
                },
            }
            engine = create_engine(**engine_options)
            event.listen(engine, "connect", self._configure_sqlite)
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL;")
            session_factory = sessionmaker(
                bind=engine,
                expire_on_commit=False,
                autoflush=False,
            )
            self._engine = engine
            self._session_factory = session_factory

    @contextmanager
    def session(self, *, write: bool = False) -> Generator[Session, None, None]:
        self.open()
        if self._session_factory is None:
            raise RuntimeError("PanSearch 数据库会话工厂未初始化")
        db = self._session_factory()
        try:
            yield db
            if write:
                db.commit()
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def close(self) -> None:
        with self._lock:
            if self._engine is not None:
                try:
                    with self._engine.connect() as connection:
                        connection.exec_driver_sql(
                            "PRAGMA wal_checkpoint(TRUNCATE);"
                        )
                except Exception as error:
                    logger.debug(f"PanSearch WAL checkpoint 失败：{error}")
                self._engine.dispose()
            self._engine = None
            self._session_factory = None
