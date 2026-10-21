from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection


def alembic_config() -> Config:
    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return config


def schema_is_current(connection: Connection) -> bool:
    expected = set(ScriptDirectory.from_config(alembic_config()).get_heads())
    actual = set(MigrationContext.configure(connection).get_current_heads())
    return actual == expected
