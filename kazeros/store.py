"""Small SQLite store. All user records are scoped to one Discord guild."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS members (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, ref TEXT NOT NULL,
            preferences TEXT NOT NULL, snapshot TEXT, previous_snapshot TEXT,
            PRIMARY KEY (guild_id, user_id)
        )""")
        self.connection.commit()

    def close(self):
        self.connection.close()

    def get(self, guild_id: int, user_id: int) -> dict | None:
        row = self.connection.execute("SELECT * FROM members WHERE guild_id=? AND user_id=?", (str(guild_id), str(user_id))).fetchone()
        if row is None:
            return None
        return {name: json.loads(row[name]) if row[name] else None for name in ("ref", "preferences", "snapshot", "previous_snapshot")}

    def bind(self, guild_id: int, user_id: int, ref: dict, preferences: dict, snapshot: dict | None = None):
        old = self.get(guild_id, user_id)
        same = old and ref_identity(old["ref"]) == ref_identity(ref)
        previous = old.get("previous_snapshot") if same else None
        if same and old.get("snapshot") and snapshot and old["snapshot"].get("fetched_at") != snapshot.get("fetched_at"):
            previous = old["snapshot"]
        if same and snapshot is None:
            snapshot = old.get("snapshot")
        with self.connection:
            self.connection.execute("""INSERT INTO members VALUES (?,?,?,?,?,?)
            ON CONFLICT(guild_id,user_id) DO UPDATE SET ref=excluded.ref,
            preferences=excluded.preferences, snapshot=excluded.snapshot,
            previous_snapshot=excluded.previous_snapshot""", (
                str(guild_id), str(user_id), dumps(ref), dumps(preferences), dumps(snapshot), dumps(previous)
            ))

    def set_preferences(self, guild_id: int, user_id: int, preferences: dict):
        with self.connection:
            self.connection.execute("UPDATE members SET preferences=? WHERE guild_id=? AND user_id=?", (dumps(preferences), str(guild_id), str(user_id)))

    def save_snapshot(self, guild_id: int, user_id: int, snapshot: dict):
        old = self.get(guild_id, user_id)
        if old is None:
            raise ValueError("Персонаж ещё не привязан.")
        if ref_identity(old["ref"]) != ref_identity(snapshot.get("profile", {})):
            raise ValueError("Ответ оружейной относится к другому персонажу. Привязка не изменена.")
        if old.get("snapshot") and old["snapshot"].get("fetched_at") == snapshot.get("fetched_at"):
            return
        with self.connection:
            self.connection.execute("UPDATE members SET previous_snapshot=snapshot, snapshot=? WHERE guild_id=? AND user_id=?", (dumps(snapshot), str(guild_id), str(user_id)))

    def forget(self, guild_id: int, user_id: int):
        with self.connection:
            self.connection.execute("DELETE FROM members WHERE guild_id=? AND user_id=?", (str(guild_id), str(user_id)))


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value is not None else None


def ref_identity(ref: dict) -> tuple:
    # Integer IDs may arrive as strings. Character IDs remain opaque strings.
    from urllib.parse import unquote
    return (str(ref.get("region", "")).lower(), str(ref.get("server_id", "")), unquote(str(ref.get("character_id", ""))))


def changes(previous: dict | None, current: dict) -> list[str]:
    if not previous or ref_identity(previous.get("profile", {})) != ref_identity(current.get("profile", {})):
        return []
    result = []
    before, after = previous.get("profile", {}), current.get("profile", {})
    for field, label in (("level", "Уровень"), ("item_level", "ГС"), ("combat_power", "Боевая мощь")):
        a, b = before.get(field), after.get(field)
        if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool) and a != b:
            result.append(f"{label}: {a:g} → {b:g} ({b-a:+g})")
    slots = {str(e.get("slot")): e for e in previous.get("equipment", [])}
    for item in current.get("equipment", []):
        old = slots.get(str(item.get("slot")))
        if not old:
            continue
        if old.get("id") != item.get("id"):
            result.append(f"{item.get('slot_name') or 'Слот'}: {old.get('name') or 'предмет'} → {item.get('name') or 'предмет'}")
        else:
            a, b = old.get("enchant_level"), item.get("enchant_level")
            if isinstance(a, int) and isinstance(b, int) and a != b:
                result.append(f"{item.get('name') or 'Предмет'}: +{a} → +{b}")
    return result[:12]
