"""Ambiente Alembic do Aumigão.

Reaproveita o Base e a URL do próprio projeto, e importa app.models para que
TODO o schema (32 modelos) esteja presente em Base.metadata. Isso é essencial:
um modelo ausente faria o autogenerate achar que a tabela deve ser removida.
"""
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# URL e metadata vindos do projeto (fonte única da verdade).
from app.core.database import Base, _database_url
import app.models  # noqa: F401 — importa todos os modelos -> popula Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Injeta a URL real do projeto na config do Alembic em runtime.
config.set_main_option("sqlalchemy.url", _database_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Gera SQL sem conectar (modo --sql)."""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Conecta e aplica migrations."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
