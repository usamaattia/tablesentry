"""A thin SQLAlchemy wrapper: run scalar queries and read column metadata.

Anything SQLAlchemy can connect to works (DuckDB, Postgres, Snowflake, BigQuery,
Redshift, MySQL, SQLite, ...). Only standard SQL is generated: ``count(*)``, ``max(col)``
and ``count(col)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool


class Warehouse:
    def __init__(self, url: str, engine: Engine | None = None) -> None:
        self.url = url
        self.engine = engine or create_engine(url, pool_pre_ping=True)
        self._preparer = self.engine.dialect.identifier_preparer

    def close(self) -> None:
        self.engine.dispose()

    # -- naming -------------------------------------------------------------

    @staticmethod
    def split(table: str) -> tuple[str | None, str]:
        parts = table.split(".")
        if len(parts) == 1:
            return None, parts[0]
        return ".".join(parts[:-1]), parts[-1]

    def quote_table(self, table: str) -> str:
        # quote() only adds quotes when the dialect needs them, so Snowflake-style
        # case-insensitive names stay case-insensitive.
        return ".".join(self._preparer.quote(p) for p in table.split("."))

    def quote_column(self, column: str) -> str:
        return self._preparer.quote(column)

    # -- queries --------------------------------------------------------------

    def scalar(self, sql: str) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(text(sql)).scalar()

    def row(self, sql: str) -> tuple:
        with self.engine.connect() as conn:
            return tuple(conn.execute(text(sql)).one())

    def columns(self, table: str) -> list[Column]:
        """Column names and types, from information_schema where the database has one
        (DuckDB, Postgres, Snowflake, Redshift, MySQL, SQL Server, BigQuery), otherwise
        from SQLAlchemy reflection (e.g. SQLite)."""
        schema, name = self.split(table)
        sql = (
            "select column_name, data_type, is_nullable from information_schema.columns "
            "where lower(table_name) = lower(:name)"
            + (" and lower(table_schema) = lower(:schema)" if schema else "")
            + " order by ordinal_position"
        )
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(text(sql), {"name": name, "schema": schema}).all()
        except Exception:  # noqa: BLE001 - no information_schema: fall back to reflection
            return self._reflected_columns(schema, name, table)
        if not rows:
            raise LookupError(f"table {table} not found")
        return [Column(r[0], str(r[1]).upper(), str(r[2]).upper() != "NO") for r in rows]

    def _reflected_columns(self, schema: str | None, name: str, table: str) -> list[Column]:
        insp = inspect(self.engine)
        if not insp.has_table(name, schema=schema):
            raise LookupError(f"table {table} not found")
        cols = []
        for c in insp.get_columns(name, schema=schema):
            try:
                type_name = c["type"].compile(dialect=self.engine.dialect)
            except Exception:
                type_name = str(c["type"])
            cols.append(Column(c["name"], str(type_name).upper(), bool(c.get("nullable", True))))
        return cols


def to_utc(value: Any, tz: str) -> datetime | None:
    """Normalise whatever a driver returns for max(timestamp) into an aware UTC datetime.

    Naive timestamps are interpreted in ``tz`` (the warehouse's timezone). A plain date is
    treated as the end of that day, so a table loaded "today" is not reported as stale.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.max)
    if not isinstance(value, datetime):
        raise TypeError(f"expected a timestamp, got {type(value).__name__}: {value!r}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(tz))
    return value.astimezone(timezone.utc)
