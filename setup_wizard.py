"""Interactive local setup. Never prints or transmits a token outside Discord."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import sys
import tempfile
from pathlib import Path

import aiohttp

from kazeros.config import DEFAULTS, REGIONS, ROOT, ConfigError, load_token, normalize_region, validate_config, validate_token
from kazeros.network import proxy_for


def atomic_write(path: Path, content: str, *, secret=False):
    fd, name = tempfile.mkstemp(prefix=".coach-setting-", dir=path.parent)
    try:
        if secret:
            os.chmod(name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def ask(label: str, default="") -> str:
    suffix = f" [{default}]" if str(default) else ""
    value = input(f"{label}{suffix}: ").strip()
    return value if value else str(default)


async def discord_account(token: str, guild_id: int) -> tuple[str, str | None]:
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        headers = {"Authorization": f"Bot {token}"}
        async with session.get("https://discord.com/api/v10/users/@me", headers=headers, proxy=proxy_for("https://discord.com")) as response:
            if response.status == 401:
                raise ConfigError("Discord отклонил токен. Открой Bot → Reset Token и повтори настройку.")
            if response.status != 200:
                raise ConfigError(f"Проверка Discord временно недоступна (HTTP {response.status}).")
            data = await response.json()
            if not data.get("bot"):
                raise ConfigError("Нужен токен бота из Developer Portal → Bot.")
            bot_id, bot_name = str(data["id"]), str(data.get("username") or "бот")
        async with session.get(f"https://discord.com/api/v10/guilds/{guild_id}", headers=headers, proxy=proxy_for("https://discord.com")) as response:
            if response.status == 200:
                guild = await response.json()
                return bot_name, str(guild.get("name") or guild_id)
            if response.status in (403, 404):
                print("\nБот пока не видит указанный сервер Discord. Пригласи его по ссылке:")
                print(f"https://discord.com/oauth2/authorize?client_id={bot_id}&scope=bot%20applications.commands&permissions=19456&guild_id={guild_id}&disable_guild_select=true")
                return bot_name, None
            raise ConfigError(f"Проверка сервера Discord временно недоступна (HTTP {response.status}).")


async def choose_game_server(current: dict):
    from kazeros.aion import AionClient, AionError
    print("\n3. ИГРОВОЙ РЕГИОН И СЕРВЕР")
    print("eu — Европа; nae / naw — Америка; as — Азия; la — Южная Америка.")
    while True:
        try:
            region = normalize_region(ask("Регион", current.get("game_region", "eu")))
            break
        except ConfigError as exc:
            print(exc)
    client = AionClient()
    try:
        servers = await client.list_servers(region)
    except (AionError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        print(f"Список игровых серверов сейчас не загрузился: {exc}")
        print("Сохраняем поиск по всем серверам выбранного региона. При привязке выбери свой персонаж из результатов.")
        return region, None, "Все серверы региона"
    finally:
        await client.close()
    print(f"\nОфициальный список {REGIONS[region]}:")
    for server in servers:
        print(f"  {server['id']:>5}  {server['name']}")
    default_id = current.get("game_server_id") if region == current.get("game_region") else None
    default = next((s["name"] for s in servers if s["id"] == default_id), "0")
    if region == "eu" and default == "Luteros":
        print("\nПредложен Luteros (EU). Сверь написание с экраном выбора сервера в игре.")
        print("Если под Looters ты имел в виду другой сервер, введи его имя или 0 для поиска по всему региону.")
    while True:
        value = ask("Название / ID игрового сервера, 0 — все серверы", default)
        if value.casefold() in {"0", "all", "все"}:
            return region, None, "Все серверы региона"
        match = next((s for s in servers if value.casefold() == str(s["name"]).casefold() or value == str(s["id"])), None)
        if match:
            print(f"Выбран {match['name']} (ID {match['id']}).")
            return region, match["id"], match["name"]
        print("Такого названия/ID нет в полученном списке. Выбери из списка выше или введи 0.")


async def main():
    print("\nKAZEROS COACH · ПЕРВИЧНАЯ НАСТРОЙКА\n")
    print("Открой docs/SETUP_RU.html, если нужна инструкция по кнопкам Discord.")
    current = dict(DEFAULTS)
    if (ROOT / "config.json").exists():
        try:
            loaded = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                current.update(loaded)
        except (OSError, ValueError):
            print("Старые настройки не прочитаны. Мастер создаст новые после ввода.")
    try:
        old_token = load_token()
    except ConfigError:
        old_token = ""
    print("\n1. ТОКЕН БОТА DISCORD")
    print("Developer Portal → твой бот → Bot → Reset Token → Copy.")
    print("Вставь токен ТОЛЬКО сюда. Символы ввода не отображаются; это нормально.")
    if old_token:
        print("Нажми Enter, чтобы сохранить уже введённый токен.")
    while True:
        raw = getpass.getpass("Bot Token: ").strip()
        try:
            token = validate_token(raw or old_token)
            break
        except ConfigError as exc:
            print(exc)
    print("\n2. СЕРВЕР DISCORD")
    print("В Discord включи Настройки пользователя → Расширенные → Режим разработчика.")
    print("ПКМ по значку сервера слева → Копировать ID сервера. Это НЕ ID игрового сервера.")
    while True:
        candidate = ask("ID сервера Discord", current.get("discord_guild_id") or "")
        try:
            guild_id = int(candidate)
            if not (0 < guild_id < 2**64):
                raise ValueError
            break
        except ValueError:
            print("Вставь только длинное число из «Копировать ID сервера».")
    print("Проверяем доступ к Discord…")
    try:
        bot_name, guild_name = await discord_account(token, guild_id)
        print(f"Токен принят. Бот: {bot_name}.")
        if guild_name:
            print(f"Сервер Discord: {guild_name}.")
    except ConfigError as exc:
        print(f"\n{exc}")
        if "отклонил токен" in str(exc) or "Нужен токен" in str(exc):
            print("Настройки не сохранены. Запусти 02_SETTINGS.bat и введи правильный токен.")
            return 1
        print("Сохраним настройки; повторную проверку можно выполнить через 04_CHECK.bat.")
    except (aiohttp.ClientError, asyncio.TimeoutError):
        print("Discord сейчас недоступен по сети. Настройки можно сохранить и проверить через 04_CHECK.bat.")
    region, server_id, server_name = await choose_game_server(current)
    current.update(discord_guild_id=guild_id, game_region=region, game_server_id=server_id, game_server_name=server_name)
    current = validate_config(current)
    # Save only the local settings we own. No token is ever stored in config.json.
    atomic_write(ROOT / "config.json", json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    atomic_write(ROOT / ".env", f"DISCORD_TOKEN={token}\n", secret=True)
    print("\nНАСТРОЙКА СОХРАНЕНА.")
    print("Закрой это окно. Запусти 03_START.bat и дождись строки «Бот готов».")
    print("В нужном канале своего сервера Discord выполни /панель.")
    print("Затем нажми «Персонаж», укажи ник, выбери себя и нажми «Разбор».")
    return 0


if __name__ == "__main__":
    try:
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        sys.exit(asyncio.run(main()))
    except (KeyboardInterrupt, EOFError):
        print("\nНастройка отменена. Её можно повторить через 02_SETTINGS.bat.")
        sys.exit(1)
    except ConfigError as exc:
        print(f"\n{exc}")
        sys.exit(1)
