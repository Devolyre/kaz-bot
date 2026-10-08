"""Local and hosting settings, with environment overrides and no secret logging."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGIONS = {"eu": "Europe", "nae": "North America East", "naw": "North America West", "as": "Asia", "la": "South America"}
GOALS = {"pve": "PvE", "pvp": "PvP"}
ROLES = {"damage": "Урон", "tank": "Выживаемость / танк", "support": "Лечение / поддержка"}
BUDGETS = {"free": "Без покупок", "low": "Небольшой бюджет", "flexible": "Готов вкладываться"}
DEFAULTS = {
    "discord_guild_id": 0, "game_region": "eu", "game_server_id": 1312,
    "game_server_name": "Luteros", "guild_name": "KAZEROS", "cache_minutes": 15,
    "default_goal": "pve", "default_role": "damage", "default_budget": "low",
}


class ConfigError(ValueError):
    pass


def normalize_region(value: str) -> str:
    value = value.strip().casefold()
    aliases = {"europe": "eu", "европа": "eu", "asia": "as", "азия": "as", "sa": "la", "latam": "la", "na-east": "nae", "na-west": "naw"}
    value = aliases.get(value, value)
    if value not in REGIONS:
        raise ConfigError("Регион Global: eu, nae, naw, as или la. Корея и Тайвань в этом боте не используются.")
    return value


def validate_config(data: dict) -> dict:
    result = dict(DEFAULTS)
    result.update(data)
    result["game_region"] = normalize_region(str(result["game_region"]))
    try:
        guild_id = int(result["discord_guild_id"])
        if guild_id <= 0 or guild_id >= 2**64:
            raise ValueError
        result["discord_guild_id"] = guild_id
    except (TypeError, ValueError):
        raise ConfigError("Нужен DISCORD_GUILD_ID: длинное число из «Копировать ID сервера» в Discord. На Bothost добавь его в переменные окружения; на Windows используй 02_SETTINGS.bat.") from None
    server = result.get("game_server_id")
    if server in (None, "", 0, "0"):
        result["game_server_id"] = None
        result["game_server_name"] = "Все серверы региона"
    else:
        try:
            result["game_server_id"] = int(server)
            if result["game_server_id"] <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ConfigError("Неверный GAME_SERVER_ID. Укажи числовой ID игрового сервера или 0 для поиска по всему региону.") from None
    for name, allowed in (("default_goal", GOALS), ("default_role", ROLES), ("default_budget", BUDGETS)):
        if result[name] not in allowed:
            raise ConfigError(f"Неверное значение настройки {name}.")
    try:
        result["cache_minutes"] = max(5, min(int(result["cache_minutes"]), 60))
    except (TypeError, ValueError):
        raise ConfigError("cache_minutes должен быть числом от 5 до 60.") from None
    result["guild_name"] = str(result.get("guild_name") or "KAZEROS")[:60]
    return result


ENV_SETTINGS = {
    "DISCORD_GUILD_ID": "discord_guild_id",
    "GAME_REGION": "game_region",
    "GAME_SERVER_ID": "game_server_id",
    "GAME_SERVER_NAME": "game_server_name",
    "GUILD_NAME": "guild_name",
    "CACHE_MINUTES": "cache_minutes",
    "DEFAULT_GOAL": "default_goal",
    "DEFAULT_ROLE": "default_role",
    "DEFAULT_BUDGET": "default_budget",
}
TOKEN_KEYS = ("DISCORD_TOKEN", "DISCORD_BOT_TOKEN", "BOT_TOKEN")


def load_config(root: Path = ROOT) -> dict:
    """Environment values override optional local config.json, never the reverse."""
    path = root / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        data = {}
    except (OSError, json.JSONDecodeError):
        raise ConfigError("Не удалось прочитать config.json. Исправь файл или убери его и используй переменные окружения.") from None
    if not isinstance(data, dict):
        raise ConfigError("config.json должен содержать объект настроек.")
    for env_key, config_key in ENV_SETTINGS.items():
        value = os.environ.get(env_key)
        if value is not None and value.strip():
            data[config_key] = value.strip()
    return validate_config(data)


def data_dir(root: Path = ROOT) -> Path:
    """Bothost supplies durable /app/data; local bot.py keeps its old runtime path."""
    value = os.environ.get("KAZEROS_DATA_DIR", "").strip() or os.environ.get("DATA_DIR", "").strip()
    if value:
        path = Path(value).expanduser()
        return path if path.is_absolute() else root / path
    if os.environ.get("BOT_ID", "").strip() or root == Path("/app"):
        return Path("/app/data")
    return root / "runtime"


def validate_token(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("bot "):
        value = value[4:].strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{30,}", value):
        raise ConfigError("Токен имеет неверный формат. Скопируй Bot Token из Discord Developer Portal и обнови поле токена на Bothost (локально — 02_SETTINGS.bat).")
    return value


def load_token(root: Path = ROOT) -> str:
    # Bothost uses several aliases depending on the deployment template.
    for key in TOKEN_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return validate_token(value)
    local_values = {}
    try:
        for line in (root / ".env").read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and key in TOKEN_KEYS:
                local_values[key] = value.strip().strip("\"'")
    except FileNotFoundError:
        pass
    except OSError:
        raise ConfigError("Не удалось прочитать локальный .env. На Bothost укажи токен в панели.") from None
    for key in TOKEN_KEYS:
        if local_values.get(key):
            return validate_token(local_values[key])
    raise ConfigError("Токен Discord не задан. Заполни Bot Token на Bothost или DISCORD_TOKEN в переменных окружения. Не отправляй токен в чат.")


def default_preferences(config: dict) -> dict:
    return {"goal": config["default_goal"], "role": config["default_role"], "budget": config["default_budget"]}
