"""Connection pool and idempotent schema setup."""

from pathlib import Path

from pgvector.psycopg import register_vector
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

_SCHEMA = Path(__file__).with_name("schema.sql")


def _configure(conn: Connection[DictRow]) -> None:
    register_vector(conn)


def create_pool(database_url: str) -> ConnectionPool[Connection[DictRow]]:
    # The vector type must exist before register_vector can look it up.
    with Connection.connect(database_url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
        database_url,
        connection_class=Connection[DictRow],
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
        configure=_configure,
        open=True,
    )
    return pool


def apply_schema(pool: ConnectionPool[Connection[DictRow]], embedding_dim: int) -> None:
    sql = _SCHEMA.read_text().replace("{dim}", str(int(embedding_dim)))
    with pool.connection() as conn:
        conn.execute(sql)  # type: ignore[arg-type]
