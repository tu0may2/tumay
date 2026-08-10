"""Подключение к БД и управление сессиями."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

logger = logging.getLogger(__name__)

_connect_args = (
    {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
)

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)


if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):  # noqa: ANN001
        """WAL даёт одновременное чтение из API и запись из сборщика."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Создать таблицы и дописать недостающие колонки."""
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Лёгкая миграция: добавить колонки, появившиеся в новых версиях.

    ``create_all`` создаёт только отсутствующие таблицы и не трогает уже
    существующие, поэтому на базе, созданной прежней версией, новые поля
    (НКД, средневзвешенная цена предыдущего дня) иначе не появятся.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = column.type.compile(dialect=engine.dialect)
                # Новые колонки всегда nullable, поэтому значение по умолчанию
                # не нужно — старые строки просто получат NULL
                connection.execute(
                    text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {ddl}')
                )
                logger.info(
                    "Миграция: в таблицу %s добавлена колонка %s",
                    table.name,
                    column.name,
                )


@contextmanager
def session_scope() -> Iterator[Session]:
    """Транзакция с автокоммитом и откатом при ошибке."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI-зависимость."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
