"""Alembic environment; URLs stay out of ConfigParser and logs."""

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
section = config.get_section(config.config_ini_section, {})
section["sqlalchemy.url"] = config.attributes["database_url"]
connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
with connectable.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        # Keep the lock and all DDL on one backend through transaction pooling.
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(724918230104)")
        context.run_migrations()
