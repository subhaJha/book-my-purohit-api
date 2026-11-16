from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=None):
        return self.connection.execute(sql.replace("?", "%s"), params)


class Database:
    def __init__(self, path: Path, database_url: str | None = None):
        self.path = path
        self.database_url = database_url
        self.pool = None

    @contextmanager
    def connect(self):
        if self.database_url:
            with self.pool.connection() as connection:
                yield PostgresConnection(connection)
            return

        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection

    def initialize(self, *schemas):
        if not self.database_url:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as db:
                db.execute("PRAGMA journal_mode = WAL")
                db.executescript(";\n".join(schemas))
            return

        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.pool = ConnectionPool(
            self.database_url,
            kwargs={"autocommit": True, "connect_timeout": 8, "row_factory": dict_row, "prepare_threshold": None},
            min_size=1,
            max_size=4,
            max_idle=60,
            timeout=10,
            open=False,
        )
        self.pool.open()
        ddl = ";\n".join(schemas)
        ddl = (
            ddl.replace("CREATE TABLE IF NOT EXISTS customers (",
                        "CREATE TABLE IF NOT EXISTS customers (rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,")
            .replace("CREATE TABLE IF NOT EXISTS bookings (",
                     "CREATE TABLE IF NOT EXISTS bookings (rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,")
            .replace(" BLOB", " BYTEA")
            .replace("expires_at INTEGER", "expires_at BIGINT")
            .replace("DEFAULT CURRENT_TIMESTAMP",
                     "DEFAULT to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')")
        )
        with self.write() as db:
            db.execute(ddl)

    def close(self):
        if self.pool is not None:
            self.pool.close()
            self.pool = None

    @contextmanager
    def write(self):
        if self.database_url:
            with self.pool.connection() as connection, connection.transaction():
                db = PostgresConnection(connection)
                # Serialize short writes, matching SQLite's single-writer behavior.
                db.execute("SELECT pg_advisory_xact_lock(553410259)")
                yield db
            return

        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db
