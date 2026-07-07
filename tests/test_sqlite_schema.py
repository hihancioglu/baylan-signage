import importlib
import os
import sqlite3
import sys
import tempfile
import unittest


class TestSqliteSchema(unittest.TestCase):
    def test_ensure_sqlite_schema_removes_legacy_unique_hostname(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "legacy.db")
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    "CREATE TABLE devices ("
                    "id INTEGER PRIMARY KEY, "
                    "hostname VARCHAR(128) NOT NULL UNIQUE, "
                    "alias VARCHAR(128)"
                    ")"
                )
                connection.execute("INSERT INTO devices (hostname, alias) VALUES ('duplicate-host', 'first')")
                connection.commit()
            finally:
                connection.close()

            previous_database_url = os.environ.get("DATABASE_URL")
            os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
            for module_name in ["app.db", "app.config"]:
                sys.modules.pop(module_name, None)
            try:
                db = importlib.import_module("app.db")
                db.ensure_sqlite_schema()

                with db.engine.begin() as migrated:
                    migrated.execute(
                        db.text(
                            "INSERT INTO devices (hostname, mac_address, alias) "
                            "VALUES ('duplicate-host', '00:11:22:33:44:55', 'second')"
                        )
                    )
                    rows = migrated.execute(
                        db.text("SELECT hostname, alias FROM devices WHERE hostname = 'duplicate-host' ORDER BY id")
                    ).fetchall()
                    unique_hostname_indexes = db._unique_hostname_indexes(migrated)

                self.assertEqual([tuple(row) for row in rows], [("duplicate-host", "first"), ("duplicate-host", "second")])
                self.assertEqual(unique_hostname_indexes, [])
            finally:
                db.engine.dispose()
                sys.modules.pop("app.db", None)
                sys.modules.pop("app.config", None)
                if previous_database_url is None:
                    os.environ.pop("DATABASE_URL", None)
                else:
                    os.environ["DATABASE_URL"] = previous_database_url
