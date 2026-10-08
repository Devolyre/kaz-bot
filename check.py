"""Read-only diagnostics. Does not send Discord messages or register commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import unittest
from pathlib import Path

import aiohttp

from kazeros.aion import AionClient, AionError
from kazeros.coach import analyze
from kazeros.config import ROOT, ConfigError, default_preferences, load_config, load_token
from kazeros.network import proxy_for
from setup_wizard import discord_account


async def live_check(args):
    status = 0
    config = None
    print("KAZEROS · ДИАГНОСТИКА\n")
    try:
        config = load_config()
        print("[OK] Настройки прочитаны.")
        print(f"     Discord ID: {config['discord_guild_id']}")
        print(f"     Игра: {config['game_region']} / {config['game_server_name']}")
    except ConfigError as error:
        print(f"[НАСТРОЙКА] {error}")
        if not args.character:
            status = 1
    if not args.skip_discord:
        try:
            token = load_token()
            if config:
                name, guild = await discord_account(token, config["discord_guild_id"])
                if guild:
                    print(f"[OK] Discord принимает токен. Бот {name} видит сервер {guild}.")
                else:
                    print("[НУЖНО ДЕЙСТВИЕ] Пригласи бота на указанный Discord-сервер по ссылке выше.")
                    status = 1
        except ConfigError as error:
            print(f"[НАСТРОЙКА] {error}")
            status = 1
        except (aiohttp.ClientError, asyncio.TimeoutError):
            print("[СЕТЬ] Discord недоступен. Проверь соединение на этом компьютере.")
            status = 1
    client = AionClient()
    try:
        region = args.region or (config or {}).get("game_region", "eu")
        servers = await client.list_servers(region)
        print(f"[OK] Оружейная Global: получено серверов {len(servers)} в {region}.")
        server_id = args.server if args.server is not None else (config or {}).get("game_server_id")
        if server_id:
            match = next((s for s in servers if s["id"] == server_id), None)
            if match:
                print(f"[OK] Игровой сервер: {match['name']} ({match['id']}).")
            else:
                print("[ОШИБКА] Указанного ID игрового сервера нет в этом регионе. Измени GAME_SERVER_ID в настройках Bothost (0 — поиск по региону).")
                return 1
        if args.character:
            refs = await client.search_characters(args.character, region, server_id)
            exact = [r for r in refs if str(r.get("name", "")).casefold() == args.character.casefold()]
            selected = exact or refs
            if len(selected) != 1:
                print(f"Найдено кандидатов: {len(selected)}. Для диагностики нужен точный ник и сервер.")
                return 1
            snapshot = await client.get_snapshot(selected[0], force=True)
            prefs = default_preferences(config) if config else {"goal": "pve", "role": "damage", "budget": "low"}
            report = analyze(snapshot, **prefs)
            print(f"[OK] Профиль: {snapshot['profile'].get('name')} / ГС {snapshot['profile'].get('item_level')}.")
            print(f"[OK] Предметов: {len(snapshot.get('equipment') or [])}; рекомендаций: {len(report.get('actions') or [])}.")
            for action in report.get("actions", [])[:5]:
                print(f"\n{action['title']}\n{action['observed']}\n{action['action']}")
            if snapshot.get("warnings"):
                print("\nПредупреждения источника:")
                for warning in snapshot["warnings"]:
                    print(f"  {warning}")
    except (AionError, aiohttp.ClientError, asyncio.TimeoutError) as error:
        print(f"[ИСТОЧНИК] {error if isinstance(error, AionError) else 'Не удалось соединиться с оружейной.'}")
        status = 1
    finally:
        await client.close()
    print("\nДиагностика завершена. Это проверка доступа; запуск бота — 03_START.bat.")
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Проверки без токена и сетевых запросов")
    parser.add_argument("--skip-discord", action="store_true", help="Проверить только оружейную")
    parser.add_argument("--character", help="Точный публичный ник для дополнительной проверки")
    parser.add_argument("--region", choices=["eu", "nae", "naw", "as", "la"])
    parser.add_argument("--server", type=int, help="ID игрового сервера")
    args = parser.parse_args()
    if args.offline:
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    return asyncio.run(live_check(args))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПроверка остановлена.")
        sys.exit(1)
