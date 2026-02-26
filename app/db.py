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
            "assigned_at": "DATETIME",
            "unassigned_at": "DATETIME",
        },
    }

    Base.metadata.create_all(bind=engine)

    with engine.begin() as connection:
        if not _existing_columns(connection, "command_logs"):
            connection.execute(text(
                "CREATE TABLE command_logs ("
                "id INTEGER PRIMARY KEY, "
                "command_id VARCHAR(64) UNIQUE NOT NULL, "
                "command_type VARCHAR(64) NOT NULL, "
                "target_type VARCHAR(16) NOT NULL, "
                "target_value VARCHAR(128) NOT NULL, "
                "ttl_sec INTEGER DEFAULT 30 NOT NULL, "
                "payload VARCHAR(2048), "
                "expected_count INTEGER DEFAULT 0 NOT NULL, "
                "sent_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL"
                ")"
            ))

        if not _existing_columns(connection, "command_acks"):
            connection.execute(text(
                "CREATE TABLE command_acks ("
                "id INTEGER PRIMARY KEY, "
                "command_id VARCHAR(64) NOT NULL, "
                "hostname VARCHAR(128) NOT NULL, "
                "status VARCHAR(32) DEFAULT 'ok' NOT NULL, "
                "error_detail VARCHAR(1024), "
                "ack_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL"
                ")"
            ))

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

        if _existing_columns(connection, "device_groups"):
            connection.execute(
                text(
                    "UPDATE device_groups "
                    "SET assigned_at = CURRENT_TIMESTAMP "
                    "WHERE assigned_at IS NULL"
                )
            )
