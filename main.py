"""Non-interactive entrypoint for Bothost. No token is stored in source code."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def main() -> int:
    root = Path(__file__).resolve().parent
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", line_buffering=True)
    # Bothost documents /app/data as the persistent mount. A developer running
    # this outside Bothost uses the project-local data directory instead.
    fallback = "/app/data" if os.environ.get("BOT_ID") or root == Path("/app") else str(root / "data")
    if not os.environ.get("KAZEROS_DATA_DIR", "").strip():
        os.environ["KAZEROS_DATA_DIR"] = os.environ.get("DATA_DIR", "").strip() or fallback
    from kazeros.config import ConfigError, data_dir, load_config, load_token
    try:
        config = load_config()
        load_token()  # validate presence/shape, never print its contents
        data_dir().mkdir(parents=True, exist_ok=True)
    except ConfigError as error:
        print(f"[НАСТРОЙКА] {error}", flush=True)
        print("Bothost → настройки бота → Переменные окружения → DISCORD_GUILD_ID. Сохрани и перезапусти бота.", flush=True)
        return 2
    except OSError:
        print("[ХРАНИЛИЩЕ] Нет доступа на запись. Проверь KAZEROS_DATA_DIR=/app/data и права каталога.", flush=True)
        return 2
    print("KAZEROS · Bothost edition 1.1.1", flush=True)
    print(f"Discord server ID: {config['discord_guild_id']}; AION region: {config['game_region']}", flush=True)
    try:
        from bot import main as run_bot
    except ModuleNotFoundError:
        print("[СБОРКА] Не установлены зависимости. Bothost должен установить requirements.txt. Выполни пересборку.", flush=True)
        return 3
    return run_bot()


if __name__ == "__main__":
    raise SystemExit(main())
