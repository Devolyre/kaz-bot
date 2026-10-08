"""Discord presentation helpers. Game data is always displayed as data."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlsplit

import discord

from .config import BUDGETS, GOALS, REGIONS, ROLES, ConfigError, normalize_region
from .store import changes

COLOR = 0xB68B51
CLASS_RU = {
    "gladiator": "Гладиатор", "templar": "Страж", "assassin": "Убийца",
    "ranger": "Лучник", "sorcerer": "Волшебник", "spiritmaster": "Заклинатель",
    "elementalist": "Заклинатель", "cleric": "Целитель", "chanter": "Чародей",
}


def text(value, limit=1000) -> str:
    if value is None:
        return "нет данных"
    value = re.sub(r"[\x00-\x08\x0b-\x1f]", "", str(value))
    value = discord.utils.escape_mentions(discord.utils.escape_markdown(value))
    return value if len(value) <= limit else value[: max(0, limit - 1)] + "…"


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:,.0f}".replace(",", " ")
    return "нет данных"


def class_name(profile):
    value = str(profile.get("class_name") or "")
    return CLASS_RU.get(value.casefold(), value or "класс не получен")


def profile_link(value: str | None) -> str | None:
    if not value:
        return None
    url = urlsplit(value)
    return value if url.scheme == "https" and url.hostname == "aion2.plaync.com" and not url.username and not url.password and url.port in (None, 443) else None


def parse_character_url(value: str) -> dict | None:
    """Do not fetch an arbitrary URL: extract identity from the official URL only."""
    if "://" not in value:
        return None
    try:
        url = urlsplit(value.strip())
        if url.scheme != "https" or url.hostname != "aion2.plaync.com" or url.username or url.password or url.port not in (None, 443):
            raise ValueError
        parts = url.path.strip("/").split("/")
        if len(parts) != 4 or parts[1] != "characters" or not re.fullmatch(r"[a-z]{2}-[a-z]{2}", parts[0]):
            raise ValueError
        region = normalize_region(parse_qs(url.query).get("region", [""])[0])
        server_id = int(parts[2])
        cid = unquote(parts[3])
        if server_id <= 0 or not re.fullmatch(r"[A-Za-z0-9_=-]{8,500}", cid):
            raise ValueError
        return {"region": region, "server_id": server_id, "character_id": cid}
    except (ValueError, TypeError, ConfigError):
        raise ConfigError("Нужна ссылка на персонажа Global с официального aion2.plaync.com и параметром region. Ссылки на другие сайты здесь не открываются.") from None


def fetched_line(snapshot):
    try:
        when = datetime.fromisoformat(snapshot["fetched_at"].replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError
        return f"Данные получены <t:{int(when.timestamp())}:f> (<t:{int(when.timestamp())}:R>)."
    except (KeyError, TypeError, ValueError):
        return "Время получения данных не указано."


def add_field(embed, name, value, limit=850):
    # Discord limits: field 1024, total embed characters 6000. Leave room for footer.
    available = max(0, 5600 - len(embed) - min(len(name), 256))
    if available < 40 or len(embed.fields) >= 24:
        return
    embed.add_field(name=text(name, 256), value=text(value or "Нет данных", min(limit, 1024, available)), inline=False)


def profile_embed(snapshot: dict, previous: dict | None = None):
    p = snapshot.get("profile", {})
    link = profile_link(snapshot.get("profile_url"))
    embed = discord.Embed(title=text(f"{p.get('name') or 'Персонаж'} · {class_name(p)}", 240), color=COLOR, url=link)
    embed.description = text(f"{REGIONS.get(p.get('region'), p.get('region', 'Global'))} · {p.get('server_name') or p.get('server_id') or 'сервер не получен'}", 300)
    embed.add_field(name="Уровень", value=number(p.get("level")), inline=True)
    embed.add_field(name="ГС", value=number(p.get("item_level")), inline=True)
    embed.add_field(name="Боевая мощь", value=number(p.get("combat_power")), inline=True)
    gear = []
    for item in snapshot.get("equipment", [])[:24]:
        enchant = item.get("enchant_level")
        label = f"+{enchant}" if isinstance(enchant, int) else "+?"
        gear.append(f"{label} {item.get('name') or item.get('slot_name') or 'Предмет'}")
    if gear:
        for start in range(0, len(gear), 8):
            add_field(embed, "Экипировка" if start == 0 else "Экипировка · продолжение", "\n".join(gear[start:start+8]), 950)
    diffs = changes(previous, snapshot)
    if diffs:
        add_field(embed, "Изменения с прошлого запроса", "\n".join(diffs), 850)
    warnings = snapshot.get("warnings") or []
    if warnings:
        add_field(embed, "Данные оружейной", "\n".join(str(v) for v in warnings[:3]), 500)
    embed.add_field(name="Свежесть", value=fetched_line(snapshot), inline=False)
    embed.set_footer(text="KAZEROS · Публичная оружейная Global")
    return embed


def report_embed(snapshot: dict, report: dict, preferences: dict, previous: dict | None = None, *, page=0):
    p = snapshot.get("profile", {})
    embed = discord.Embed(title=text(f"План усиления · {p.get('name') or 'персонаж'}", 220), color=COLOR, url=profile_link(snapshot.get("profile_url")))
    embed.description = (
        f"**{text(class_name(p))} · ГС {number(p.get('item_level'))}**\n"
        f"{text(GOALS.get(preferences.get('goal'), 'PvE'))} · {text(ROLES.get(preferences.get('role'), 'Урон'))} · {text(BUDGETS.get(preferences.get('budget'), 'Небольшой бюджет'))}\n\n"
        f"{text(report.get('summary') or report.get('headline') or 'Результат проверки', 420)}"
    )
    actions = report.get("actions") or []
    for idx, action in enumerate(actions[page*5:page*5+5], start=page*5+1):
        content = (
            f"Обнаружено: {action.get('observed') or 'См. профиль'}\n"
            f"Что сделать: {action.get('action') or 'Проверить в игре'}\n"
            f"Почему: {action.get('reason') or 'Уточнить выбранный билд'}\n"
            f"Затраты: {action.get('cost') or 'Зависят от материалов'}"
        )
        if action.get("confidence"):
            content += f"\nОценка: {action['confidence']}"
        add_field(embed, f"{idx}. {action.get('title') or 'Рекомендация'}", content, 780)
    if not actions:
        add_field(embed, "Проверка завершена", "По доступным данным надёжных отдельных рекомендаций не найдено. Для более точного разбора сравни персонажа с выбранным образцом того же класса и региона.")
    limitations = report.get("limitations") or []
    if limitations:
        add_field(embed, "Что учитывать", "\n".join(str(v) for v in limitations[:3]), 450)
    diffs = changes(previous, snapshot)
    if diffs and page == 0:
        add_field(embed, "Твой прогресс", "\n".join(diffs[:4]), 300)
    urls = []
    for url in report.get("source_urls") or []:
        if isinstance(url, str) and url.startswith("https://") and len(url) < 400 and url not in urls:
            urls.append(url)
    if urls and len(embed) < 5000:
        # URLs are from bundled rules or the fixed official provider, never user markup.
        embed.add_field(name="Источники проверки", value="\n".join(urls[:2])[:800], inline=False)
    embed.add_field(name="Свежесть", value=fetched_line(snapshot), inline=False)
    embed.set_footer(text=f"KAZEROS · рекомендаций {len(actions)} · без расчёта DPS и цен")
    return embed


def panel_embed(config):
    embed = discord.Embed(
        title="KAZEROS  |  ЦЕНТР РАЗВИТИЯ",
        color=COLOR,
        description=(
            "**Твой персонаж. Твой маршрут усиления.**\n"
            "Проверка открытого профиля Aion 2 Global и персональные рекомендации без лишних команд.\n\n"
            "**01  ·  ПРИВЯЗАТЬ ПЕРСОНАЖА**\n"
            "Укажи ник или ссылку на официальную оружейную.\n\n"
            "**02  ·  НАСТРОИТЬ ЦЕЛЬ**\n"
            "Выбери PvE или PvP, свою роль и бюджет.\n\n"
            "**03  ·  МОЙ ПЛАН УСИЛЕНИЯ**\n"
            "Посмотри приоритеты, заточку и изменения с прошлого анализа."
        ),
    )
    embed.add_field(name="🌍 Регион и сервер", value=text(f"{REGIONS[config['game_region']]}  •  {config['game_server_name']}"), inline=False)
    embed.add_field(name="🔒 Личный доступ", value="Результаты и настройки видны только тебе. Общая панель остаётся чистой.", inline=False)
    embed.set_footer(text="KAZEROS  •  AION 2 GLOBAL  •  CHARACTER INTELLIGENCE")
    return embed
