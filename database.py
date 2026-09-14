from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from config import DB_PATH, DEFAULT_SETTINGS


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path=DB_PATH):
        self.path = str(path)
        self.lock = threading.RLock()
        self.init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY(chat_id, key),
            FOREIGN KEY(chat_id) REFERENCES groups(chat_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS command_registry (
            name TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            usage TEXT NOT NULL,
            category TEXT NOT NULL,
            default_access TEXT NOT NULL,
            enabled_by_default INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS command_access (
            chat_id INTEGER NOT NULL,
            command_name TEXT NOT NULL,
            access_level TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(chat_id, command_name),
            FOREIGN KEY(chat_id) REFERENCES groups(chat_id) ON DELETE CASCADE,
            FOREIGN KEY(command_name) REFERENCES command_registry(name) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS roles (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            PRIMARY KEY(chat_id, user_id),
            FOREIGN KEY(chat_id) REFERENCES groups(chat_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            moderator_id INTEGER NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS filters (
            chat_id INTEGER NOT NULL,
            word TEXT NOT NULL,
            PRIMARY KEY(chat_id, word),
            FOREIGN KEY(chat_id) REFERENCES groups(chat_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            actor_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            target_id INTEGER,
            details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_warnings_target
        ON warnings(chat_id, user_id);

        CREATE INDEX IF NOT EXISTS idx_audit_chat
        ON audit_log(chat_id, id);
        """
        with self.lock, self.connect() as conn:
            conn.executescript(schema)

    # ---------- group/settings ----------

    def ensure_group(self, chat_id: int, title: str = "") -> None:
        with self.lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO groups(chat_id, title, created_at)
                VALUES(?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title
                """,
                (chat_id, title or "", now_iso()),
            )
            existing = {
                row["key"]
                for row in conn.execute(
                    "SELECT key FROM settings WHERE chat_id = ?", (chat_id,)
                )
            }
            for key, value in DEFAULT_SETTINGS.items():
                if key not in existing:
                    conn.execute(
                        "INSERT INTO settings(chat_id, key, value) VALUES(?, ?, ?)",
                        (chat_id, key, str(value)),
                    )

    def get_setting(self, chat_id: int, key: str, default: Any = None) -> Any:
        with self.lock, self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE chat_id=? AND key=?",
                (chat_id, key),
            ).fetchone()
        if not row:
            return default
        value = row["value"]
        if value in {"True", "False"}:
            return value == "True"
        try:
            if "." in value:
                return float(value)
            return int(value)
        except ValueError:
            return value

    def set_setting(self, chat_id: int, key: str, value: Any) -> None:
        self.ensure_group(chat_id)
        with self.lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(chat_id, key, value) VALUES(?, ?, ?)
                ON CONFLICT(chat_id,key)
                DO UPDATE SET value=excluded.value
                """,
                (chat_id, key, str(value)),
            )

    def get_all_settings(self, chat_id: int) -> dict[str, Any]:
        self.ensure_group(chat_id)
        with self.lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM settings WHERE chat_id=? ORDER BY key",
                (chat_id,),
            ).fetchall()
        result = {}
        for row in rows:
            value = row["value"]
            if value == "True":
                value = True
            elif value == "False":
                value = False
            else:
                try:
                    value = int(value)
                except ValueError:
                    pass
            result[row["key"]] = value
        return result

    # ---------- command registry ----------

    def seed_commands(self, commands: list[dict]) -> None:
        with self.lock, self.connect() as conn:
            for command in commands:
                conn.execute(
                    """
                    INSERT INTO command_registry(
                        name, description, usage, category, default_access, enabled_by_default
                    )
                    VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        description=excluded.description,
                        usage=excluded.usage,
                        category=excluded.category,
                        default_access=excluded.default_access,
                        enabled_by_default=excluded.enabled_by_default
                    """,
                    (
                        command["name"],
                        command["description"],
                        command["usage"],
                        command["category"],
                        command["default_access"],
                        int(command.get("enabled_by_default", True)),
                    ),
                )

    def get_command(self, name: str):
        with self.lock, self.connect() as conn:
            return conn.execute(
                "SELECT * FROM command_registry WHERE name=?", (name,)
            ).fetchone()

    def list_commands(self):
        with self.lock, self.connect() as conn:
            return conn.execute(
                "SELECT * FROM command_registry ORDER BY category, name"
            ).fetchall()

    def get_command_config(self, chat_id: int, name: str):
        row = self.get_command(name)
        if not row:
            return None
        with self.lock, self.connect() as conn:
            custom = conn.execute(
                """
                SELECT access_level, enabled
                FROM command_access
                WHERE chat_id=? AND command_name=?
                """,
                (chat_id, name),
            ).fetchone()
        if custom:
            return {
                "access_level": custom["access_level"],
                "enabled": bool(custom["enabled"]),
            }
        return {
            "access_level": row["default_access"],
            "enabled": bool(row["enabled_by_default"]),
        }

    def set_command_config(
        self, chat_id: int, name: str, access_level: str | None = None,
        enabled: bool | None = None
    ) -> None:
        current = self.get_command_config(chat_id, name)
        if not current:
            return
        access_level = access_level or current["access_level"]
        enabled = current["enabled"] if enabled is None else enabled
        self.ensure_group(chat_id)
        with self.lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO command_access(
                    chat_id, command_name, access_level, enabled
                )
                VALUES(?, ?, ?, ?)
                ON CONFLICT(chat_id,command_name)
                DO UPDATE SET
                    access_level=excluded.access_level,
                    enabled=excluded.enabled
                """,
                (chat_id, name, access_level, int(enabled)),
            )

    # ---------- roles ----------

    def set_role(self, chat_id: int, user_id: int, role: str | None) -> None:
        self.ensure_group(chat_id)
        with self.lock, self.connect() as conn:
            if role is None:
                conn.execute(
                    "DELETE FROM roles WHERE chat_id=? AND user_id=?",
                    (chat_id, user_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO roles(chat_id,user_id,role) VALUES(?,?,?)
                    ON CONFLICT(chat_id,user_id)
                    DO UPDATE SET role=excluded.role
                    """,
                    (chat_id, user_id, role),
                )

    def get_role(self, chat_id: int, user_id: int) -> str | None:
        with self.lock, self.connect() as conn:
            row = conn.execute(
                "SELECT role FROM roles WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
        return row["role"] if row else None

    def list_roles(self, chat_id: int):
        with self.lock, self.connect() as conn:
            return conn.execute(
                "SELECT user_id, role FROM roles WHERE chat_id=? ORDER BY role, user_id",
                (chat_id,),
            ).fetchall()

    # ---------- warnings ----------

    def add_warning(
        self, chat_id: int, user_id: int, moderator_id: int, reason: str = ""
    ) -> int:
        with self.lock, self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO warnings(chat_id,user_id,moderator_id,reason,created_at)
                VALUES(?,?,?,?,?)
                """,
                (chat_id, user_id, moderator_id, reason, now_iso()),
            )
            return int(cur.lastrowid)

    def warning_count(self, chat_id: int, user_id: int) -> int:
        with self.lock, self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM warnings WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
        return int(row["count"])

    def get_warnings(self, chat_id: int, user_id: int, limit: int = 20):
        with self.lock, self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM warnings
                WHERE chat_id=? AND user_id=?
                ORDER BY id DESC LIMIT ?
                """,
                (chat_id, user_id, limit),
            ).fetchall()

    def remove_last_warning(self, chat_id: int, user_id: int) -> bool:
        with self.lock, self.connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM warnings
                WHERE chat_id=? AND user_id=?
                ORDER BY id DESC LIMIT 1
                """,
                (chat_id, user_id),
            ).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM warnings WHERE id=?", (row["id"],))
            return True

    # ---------- filters ----------

    def add_filter(self, chat_id: int, word: str) -> bool:
        self.ensure_group(chat_id)
        normalized = word.strip().lower()
        if not normalized:
            return False
        with self.lock, self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO filters(chat_id,word) VALUES(?,?)",
                (chat_id, normalized),
            )
            return cur.rowcount > 0

    def remove_filter(self, chat_id: int, word: str) -> bool:
        with self.lock, self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM filters WHERE chat_id=? AND word=?",
                (chat_id, word.strip().lower()),
            )
            return cur.rowcount > 0

    def get_filters(self, chat_id: int) -> list[str]:
        with self.lock, self.connect() as conn:
            return [
                row["word"]
                for row in conn.execute(
                    "SELECT word FROM filters WHERE chat_id=? ORDER BY word",
                    (chat_id,),
                ).fetchall()
            ]

    # ---------- audit ----------

    def audit(
        self,
        chat_id: int,
        actor_id: int,
        action: str,
        target_id: int | None = None,
        details: str = "",
    ) -> None:
        with self.lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_log(
                    chat_id,actor_id,action,target_id,details,created_at
                )
                VALUES(?,?,?,?,?,?)
                """,
                (chat_id, actor_id, action, target_id, details, now_iso()),
            )

    def recent_audit(self, chat_id: int, limit: int = 20):
        with self.lock, self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM audit_log
                WHERE chat_id=?
                ORDER BY id DESC LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
