"""Alembic environment: migrates the database in DATABASE_URL (countertalk.config)."""

import logging

from alembic import context

from countertalk.db import get_engine
from countertalk.models import Base

if context.config.cmd_opts is not None:
    # Run from the command line, not from tests: show each migration as it runs.
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5.5s [%(name)s] %(message)s")

if context.is_offline_mode():
    raise SystemExit("Offline (--sql) migrations aren't supported; run against a database.")

# The app's engine, so migrations get the same lock and idle-transaction timeouts.
with get_engine().connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
