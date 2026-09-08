"""为 PanSearch 私有数据库生成 Alembic 迁移脚本。"""

import argparse
from configparser import ConfigParser
from pathlib import Path

from alembic.command import revision
from alembic.config import Config as AlembicConfig
from app.core.config import settings

from .models import PanSearchBase


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 PanSearch 数据库迁移")
    parser.add_argument("message", help="迁移说明或版本号")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=(
                settings.PLUGIN_DATA_PATH
                / "PanSearch"
                / "pansearch.db"
        ),
        help="用于比较旧结构的 SQLite 数据库路径",
    )
    args = parser.parse_args()
    db_path = args.db_path.expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"数据库不存在：{db_path}")

    config = AlembicConfig()
    config.file_config = ConfigParser(interpolation=None)
    config.set_main_option(
        "script_location", str(Path(__file__).with_name("alembic"))
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    config.attributes["target_metadata"] = PanSearchBase.metadata
    revision(config, message=args.message, autogenerate=True)


if __name__ == "__main__":
    main()
