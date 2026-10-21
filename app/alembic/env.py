from alembic import context

from iacp.db import Base, get_engine
from iacp import models  # noqa: F401 — register mapped tables for autogenerate


def run(connection):
    context.configure(
        connection=connection, target_metadata=Base.metadata,
        compare_type=True, compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are disabled: bootstrap adoption requires schema inspection")
elif context.config.attributes.get("connection") is not None:
    run(context.config.attributes["connection"])
else:
    with get_engine().connect() as connection:
        run(connection)
