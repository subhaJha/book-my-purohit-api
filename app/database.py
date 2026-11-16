from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        return self.connection.execute(sql.replace("?", "%s"), params)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def close(self):
        self.connection.close()


class Database:
    def __init__(self, path: Path, database_url: str | None = None):
        self.path = path
        self.database_url = database_url

    def connect(self):
        if self.database_url:
            import psycopg
            from psycopg.rows import dict_row

            connection = psycopg.connect(self.database_url, connect_timeout=8, row_factory=dict_row)
            return PostgresConnection(connection)

        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self, *schemas):
        if not self.database_url:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self.connect()) as db:
                db.execute("PRAGMA journal_mode = WAL")
                db.executescript(";\n".join(schemas))
            return

        with self.write() as db:
            db.execute("SET LOCAL TIME ZONE 'UTC'")
            for schema in schemas:
                schema = (
                    schema.replace("CREATE TABLE IF NOT EXISTS customers (",
                                   "CREATE TABLE IF NOT EXISTS customers (rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,")
                    .replace("CREATE TABLE IF NOT EXISTS bookings (",
                             "CREATE TABLE IF NOT EXISTS bookings (rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,")
                    .replace(" BLOB", " BYTEA")
                    .replace("expires_at INTEGER", "expires_at BIGINT")
                    .replace("DEFAULT CURRENT_TIMESTAMP",
                             "DEFAULT to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')")
                )
                for statement in schema.split(";"):
                    if statement.strip():
                        db.execute(statement)

    @contextmanager
    def write(self):
        with closing(self.connect()) as db, db:
            if self.database_url:
                # Serialize short writes, matching SQLite's single-writer behavior.
                db.execute("SELECT pg_advisory_xact_lock(553410259)")
            else:
                db.execute("BEGIN IMMEDIATE")
            yield db
