from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


def _existing_columns(connection, table_name):
    rows = connection.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return {row[1] for row in rows}


def ensure_sqlite_schema():
    """
    Apply additive, backward-compatible schema updates for SQLite deployments.

    Some installations started without Alembic migrations. This helper makes sure
    newly introduced nullable columns exist so ORM SELECTs do not fail.
    """
    if not engine.dialect.name.startswith("sqlite"):
        return

    table_column_types = {
        "devices": {
            "agent_version": "VARCHAR(64)",
            "os_version": "VARCHAR(128)",
            "last_error": "VARCHAR(512)",
            "last_state": "VARCHAR(64)",
        },
        "playlists": {
            "type": "VARCHAR(32) DEFAULT 'normal' NOT NULL",
            "valid_from": "DATETIME",
            "valid_to": "DATETIME",
            "priority": "INTEGER DEFAULT 0 NOT NULL",
            "loop_mode": "VARCHAR(32) DEFAULT 'sequential' NOT NULL",
        },
        "playlist_items": {
            "media_type": "VARCHAR(64) DEFAULT 'video'",
            "duration_sec": "INTEGER",
            "checksum": "VARCHAR(128)",
            "source_url": "VARCHAR(1024)",
        },
        "device_groups": {
            "is_active": "BOOLEAN DEFAULT 1 NOT NULL",
            # SQLite cannot ALTER TABLE ... ADD COLUMN with CURRENT_TIMESTAMP
            # defaults. Add as nullable first, then backfill existing rows.
            "assigned_at": "DATETIME",
            "unassigned_at": "DATETIME",
        },
    }

    with engine.begin() as connection:
        for table_name, wanted_columns in table_column_types.items():
            existing_columns = _existing_columns(connection, table_name)
            if not existing_columns:
                continue

            for column_name, column_type in wanted_columns.items():
                if column_name in existing_columns:
                    continue
                connection.execute(
                    text(
                        f"ALTER TABLE {table_name} "
                        f"ADD COLUMN {column_name} {column_type}"
                    )
                )

        # Backfill timestamps for legacy rows after assigned_at is introduced.
        if "device_groups" in table_column_types:
            connection.execute(
                text(
                    "UPDATE device_groups "
                    "SET assigned_at = CURRENT_TIMESTAMP "
                    "WHERE assigned_at IS NULL"
                )
            )
