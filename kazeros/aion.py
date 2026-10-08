"""Read-only adapter for the public AION 2 Global character website.

This uses the website's undocumented JSON interfaces, not an NC-supported SDK.
Routes were checked against nuriland/aion2-api v0.4.1 and live Global replies
on 2026-10-08. No account credentials, cookies, game client or packet capture
are used. Missing values remain None; an absent collection is not an empty one.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import html
import json
import math
import re
import time
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from urllib.request import getproxies, proxy_bypass

import aiohttp


SITE = "https://aion2.plaync.com"
SEARCH = "https://api-search.plaync.com/aion2global/search/v2/character"
REGIONS = {
    "eu": "Европа",
    "nae": "Северная Америка — восток",
    "naw": "Северная Америка — запад",
    "la": "Южная Америка",
    "as": "Азия",
}
_MAX_BODY = 4 * 1024 * 1024


class AionError(Exception):
    """A safe, user-readable source error; code can distinguish rate limits."""

    def __init__(self, message: str, *, code: str = "source_error"):
        super().__init__(message)
        self.code = code


def normalize_region(region: str) -> str:
    value = str(region).strip().lower()
    value = {"sa": "la", "asia": "as"}.get(value, value)
    if value not in REGIONS:
        raise AionError("Выберите регион Global: EU, NAE, NAW, LA или AS.", code="bad_input")
    return value


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    # Search results wrap the matching part of a nickname in <strong>.
    return html.unescape(re.sub(r"</?strong(?:\s[^>]*)?>", "", value, flags=re.I)).strip()


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    return None


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (1, "1", "true", "True"):
        return True
    if value in (0, "0", "false", "False"):
        return False
    return None


def _rows(container: Any, key: str) -> list[dict] | None:
    """Reject absent/null/wrong-shaped collections; preserve explicit []."""
    value = container.get(key) if isinstance(container, dict) else None
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        return None
    return value


def _ref(ref: dict) -> dict:
    if not isinstance(ref, dict):
        raise AionError("Не удалось прочитать привязку персонажа. Привяжите его заново.", code="bad_input")
    region = normalize_region(ref.get("region", ""))
    server = _int(ref.get("server_id"))
    char = ref.get("character_id")
    if not isinstance(char, str):
        char = ""
    # Decode once before aiohttp encodes the query once. Never use unquote_plus.
    char = unquote(char)
    if server is None or server <= 0 or not char or len(char) > 512 or re.search(r"[\s%]", char):
        raise AionError("В привязке нет корректного сервера или ID персонажа.", code="bad_input")
    return {"region": region, "server_id": server, "character_id": char}


def profile_url(ref: dict) -> str:
    ref = _ref(ref)
    return f"{SITE}/en-us/characters/{ref['server_id']}/{quote(ref['character_id'], safe='')}?region={ref['region']}"


def _stats(rows: list[dict] | None) -> list[dict] | None:
    if rows is None:
        return None
    result = []
    for row in rows:
        result.append({
            "id": row.get("id", row.get("type")),
            "type": row.get("type"),
            "name": "Уровень предметов" if row.get("type") == "ItemLevel" else _text(row.get("name")),
            "value": row.get("value"),
            "description": _text(row.get("desc")),
            "min_value": row.get("minValue"),
            "extra": row.get("extra"),
        })
    return result


def _stones(rows: list[dict] | None) -> list[dict] | None:
    if rows is None:
        return None
    return [{
        "id": row.get("id"), "slot": _int(row.get("slotPos")),
        "name": _text(row.get("name")), "value": row.get("value"),
        "description": _text(row.get("desc")), "grade": _text(row.get("grade")),
    } for row in rows]


def _equipment_row(slot: dict, detail: dict | None) -> dict:
    # A failed detail request must leave detailed fields unknown, not zero.
    detail = detail if isinstance(detail, dict) else None
    d = detail or {}
    enchant = _int(d.get("enchantLevel"))
    if enchant is None:
        enchant = _int(slot.get("enchantLevel"))
    return {
        "slot": _int(slot.get("slotPos")), "slot_name": _text(slot.get("slotPosName")),
        "id": _int(slot.get("id")), "name": _text(d.get("name")) or _text(slot.get("name")),
        "grade": _text(d.get("grade")) or _text(slot.get("grade")),
        "grade_name": _text(d.get("gradeName")), "category": _text(d.get("categoryName")),
        "item_type": _text(d.get("type")), "icon_url": _text(d.get("icon")) or _text(slot.get("icon")),
        "enchant_level": enchant, "max_enchant_level": _int(d.get("maxEnchantLevel")),
        "exceed_level": _int(slot.get("exceedLevel")),
        "max_exceed_level": _int(d.get("maxExceedEnchantLevel")),
        "item_level": _int(d.get("level")), "equip_level": _int(d.get("equipLevel")),
        "enchantable": _flag(d.get("enchantable")), "soul_bind_rate": d.get("soulBindRate"),
        "magic_stone_slots": _int(d.get("magicStoneSlotCount")),
        "magic_stones": _stones(_rows(d, "magicStoneStat")),
        "god_stone_slots": _int(d.get("godStoneSlotCount")),
        "god_stones": _stones(_rows(d, "godStoneStat")),
        "main_stats": _stats(_rows(d, "mainStats")), "sub_stats": _stats(_rows(d, "subStats")),
        "sub_skills": deepcopy(_rows(d, "subSkills")),
        "detail_loaded": detail is not None,
    }


class AionClient:
    def __init__(self, *, cache_seconds: int = 900):
        self.cache_seconds = max(0, int(cache_seconds))
        self._session: aiohttp.ClientSession | None = None
        self._closed = False
        self._cache: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
        self._snapshots: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
        self._pending: dict[tuple, asyncio.Task] = {}
        self._rate_lock = asyncio.Lock()
        self._connections = asyncio.Semaphore(3)
        self._next_request = 0.0
        self._blocked_until = 0.0
        self._interval = 0.4  # at most 2.5 request starts/second for this bot

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def close(self):
        self._closed = True
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
        if self._session is not None:
            await self._session.close()

    @staticmethod
    def _cached(cache: OrderedDict, key: tuple) -> Any:
        entry = cache.get(key)
        if entry is None:
            return None
        expires, value = entry
        if time.monotonic() >= expires:
            del cache[key]
            return None
        cache.move_to_end(key)
        return deepcopy(value)

    @staticmethod
    def _remember(cache: OrderedDict, key: tuple, value: Any, seconds: int, limit: int):
        if seconds <= 0:
            return
        cache[key] = (time.monotonic() + seconds, deepcopy(value))
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)

    async def _request(self, url: str, params: dict, *, cache_seconds: int = 0) -> dict:
        if self._closed:
            raise AionError("Соединение с оружейной уже закрыто.")
        key = (url, tuple(sorted((k, str(v)) for k, v in params.items())))
        cached = self._cached(self._cache, key)
        if cached is not None:
            return cached
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25, connect=10),
                headers={"User-Agent": "KAZEROS/1.0 (public-character-info)", "Accept": "application/json"},
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        # Respect the user's configured transport proxy and NO_PROXY, like
        # urllib/curl. Avoid trust_env=True: aiohttp may otherwise read .netrc
        # and add website credentials, which this public reader never needs.
        host = urlsplit(url).hostname
        proxy = None if not host or proxy_bypass(host) else getproxies().get(urlsplit(url).scheme)
        for attempt in range(2):
            async with self._connections:
                async with self._rate_lock:
                    now = time.monotonic()
                    if now < self._blocked_until:
                        wait = max(1, math.ceil(self._blocked_until - now))
                        raise AionError(f"Оружейная ограничила запросы. Повторите через {wait} сек.", code="rate_limited")
                    delay = self._next_request - now
                    if delay > 0:
                        await asyncio.sleep(delay)
                    self._next_request = time.monotonic() + self._interval
                try:
                    async with self._session.get(url, params=params, allow_redirects=False, proxy=proxy) as response:
                        if response.status == 429:
                            seconds = _int(response.headers.get("Retry-After"))
                            seconds = max(1, min(seconds if seconds is not None else 60, 300))
                            self._blocked_until = time.monotonic() + seconds
                            raise AionError(f"Оружейная ограничила запросы. Повторите через {seconds} сек.", code="rate_limited")
                        if response.status in (401, 403):
                            raise AionError("Официальный сайт сейчас не разрешает чтение данных из этой сети. Попробуйте позднее.", code="access_denied")
                        if response.status == 404:
                            raise AionError("Оружейная не нашла запрошенные данные или изменила адрес сервиса.", code="not_found")
                        if response.status >= 500 and attempt == 0:
                            await asyncio.sleep(0.5)
                            continue
                        if response.status != 200:
                            raise AionError(f"Оружейная временно не ответила корректно (HTTP {response.status}).")
                        chunks = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            chunks.extend(chunk)
                            if len(chunks) > _MAX_BODY:
                                raise AionError("Оружейная прислала слишком большой ответ. Данные не обработаны.", code="schema_changed")
                        try:
                            body = json.loads(chunks)
                        except (ValueError, UnicodeError) as exc:
                            raise AionError("Вместо данных оружейная прислала страницу или некорректный ответ.", code="schema_changed") from exc
                        if not isinstance(body, dict):
                            raise AionError("Формат данных оружейной изменился.", code="schema_changed")
                        self._remember(self._cache, key, body, cache_seconds, 256)
                        return body
                except AionError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    if attempt == 0:
                        await asyncio.sleep(0.5)
                        continue
                    raise AionError("Не удалось связаться с официальной оружейной. Проверьте интернет и повторите позднее.", code="network_error") from exc
        raise AionError("Оружейная временно недоступна.")

    async def list_servers(self, region: str) -> list[dict]:
        region = normalize_region(region)
        body = await self._request(f"{SITE}/en-us/api/gameinfo/servers", {"region": region, "lang": "en-US"}, cache_seconds=3600)
        rows = _rows(body, "serverList")
        if not rows:
            raise AionError("Не удалось получить список серверов Global. Сервер не выбран.", code="schema_changed")
        result = []
        for row in rows:
            server_id, name = _int(row.get("serverId")), _text(row.get("serverName"))
            if server_id is None or server_id <= 0 or not name:
                raise AionError("В списке серверов есть неполные данные. Сервер не выбран.", code="schema_changed")
            result.append({"id": server_id, "name": name, "race_id": _int(row.get("raceId"))})
        return result

    async def _class_map(self, region: str) -> tuple[dict, dict]:
        params = {"region": region, "lang": "en-US"}
        classes, pcs = await asyncio.gather(
            self._request(f"{SITE}/en-us/api/gameinfo/classes", params, cache_seconds=86400),
            self._request(f"{SITE}/en-us/api/gameinfo/pcdata", params, cache_seconds=86400),
        )
        by_name, by_pc = {}, {}
        rows, pcrows = _rows(classes, "classList"), _rows(pcs, "pcDataList")
        if rows is None or pcrows is None:
            raise AionError("Оружейная не прислала справочник классов.", code="schema_changed")
        for row in rows:
            key, class_id = _text(row.get("name")), _int(row.get("id"))
            if key and class_id is not None and class_id > 0:
                value = {"class_id": class_id, "class_name": _text(row.get("text")) or key}
                by_name[key.casefold()] = value
                by_name[value["class_name"].casefold()] = value
        for row in pcrows:
            pc_id, name = _int(row.get("id")), _text(row.get("className"))
            if pc_id is not None and name:
                by_pc[pc_id] = by_name.get(name.casefold(), {"class_id": None, "class_name": _text(row.get("classText"))})
        return by_name, by_pc

    async def search_characters(self, nickname: str, region: str, server_id: int | None = None) -> list[dict]:
        region = normalize_region(region)
        if not isinstance(nickname, str) or not nickname.strip() or len(nickname.strip()) > 64:
            raise AionError("Введите ник персонажа (до 64 символов).", code="bad_input")
        nickname = nickname.strip()
        if server_id is not None:
            server_id = _int(server_id)
            if server_id is None or server_id <= 0:
                raise AionError("Выберите сервер из списка.", code="bad_input")
            servers = await self.list_servers(region)
            if server_id not in {server["id"] for server in servers}:
                raise AionError("Выбранный сервер не относится к этому региону Global.", code="bad_input")
        params = {"keyword": nickname, "region": region, "localeInfo": "en-US", "page": 1, "size": 200}
        if server_id is not None:
            params["serverId"] = server_id
        body = await self._request(SEARCH, params, cache_seconds=30)
        rows = _rows(body, "list")
        pagination = body.get("pagination")
        if rows is None or not isinstance(pagination, dict):
            raise AionError("Оружейная не прислала результаты поиска в ожидаемом формате.", code="schema_changed")
        end_page = _int(pagination.get("endPage"))
        total = _int(pagination.get("total"))
        if end_page is None or total is None:
            raise AionError("Поиск вернул неполную информацию. Уточните сервер и повторите.", code="schema_changed")
        if end_page > 1 or total > 200:
            raise AionError("Слишком много совпадений. Введите полный ник и выберите конкретный сервер.", code="too_many_results")
        if not rows:
            return []
        try:
            _, by_pc = await self._class_map(region)
        except AionError:
            by_pc = {}
        results, seen = [], set()
        for row in rows:
            if row.get("region") is not None and row.get("region") != region:
                raise AionError("Поиск вернул персонажа из другого региона. Привязка отменена.", code="schema_changed")
            hit = _ref({"region": region, "server_id": row.get("serverId"), "character_id": row.get("characterId")})
            if server_id is not None and hit["server_id"] != server_id:
                raise AionError("Поиск вернул данные с другого сервера. Привязка отменена.", code="schema_changed")
            name, server_name = _text(row.get("name")), _text(row.get("serverName"))
            if not name or not server_name:
                raise AionError("В результатах поиска отсутствует ник или сервер.", code="schema_changed")
            hit.update(name=name, server_name=server_name, level=_int(row.get("level")), race_id=_int(row.get("race")))
            hit.update(by_pc.get(_int(row.get("pcId")), {"class_id": None, "class_name": _text(row.get("className"))}))
            identity = (hit["server_id"], hit["character_id"])
            if identity not in seen:
                results.append(hit)
                seen.add(identity)
        # Full-nickname matches first; callers still ask the player to select.
        results.sort(key=lambda h: (h["name"].casefold() != nickname.casefold(), h["name"].casefold(), h["server_id"]))
        return results

    async def get_snapshot(self, ref: dict, *, force: bool = False) -> dict:
        ref = _ref(ref)
        key = (ref["region"], ref["server_id"], ref["character_id"])
        if self._closed:
            raise AionError("Соединение с оружейной уже закрыто.")
        if not force:
            cached = self._cached(self._snapshots, key)
            if cached is not None:
                return cached
        task = self._pending.get(key)
        if task is None:
            task = asyncio.create_task(self._load_snapshot(ref))
            self._pending[key] = task
            task.add_done_callback(lambda done: self._pending.pop(key, None))
        result = await asyncio.shield(task)
        self._remember(self._snapshots, key, result, self.cache_seconds, 128)
        return deepcopy(result)

    async def _load_snapshot(self, ref: dict) -> dict:
        servers = await self.list_servers(ref["region"])
        server = next((s for s in servers if s["id"] == ref["server_id"]), None)
        if server is None:
            raise AionError("Привязанный сервер отсутствует в выбранном регионе. Привяжите персонажа заново.", code="bad_input")
        query = {"region": ref["region"], "lang": "en-US", "serverId": ref["server_id"], "characterId": ref["character_id"]}
        # Only whole snapshots cache dynamic data, so fetched_at describes one
        # retrieval rather than a mixture of independently aged endpoint caches.
        info, equipment = await asyncio.gather(
            self._request(f"{SITE}/api/character/info", query),
            self._request(f"{SITE}/api/character/equipment", query),
        )
        raw_profile = info.get("profile")
        if not isinstance(raw_profile, dict):
            raise AionError("Оружейная не прислала профиль персонажа.", code="schema_changed")
        returned_id = raw_profile.get("characterId")
        if returned_id == "":
            raise AionError("Персонаж не найден: возможно, он удалён или перенесён. Проверьте привязку.", code="not_found")
        if not isinstance(returned_id, str) or unquote(returned_id) != ref["character_id"]:
            raise AionError("Оружейная вернула другой или неполный профиль. Разбор отменён.", code="schema_changed")
        if _int(raw_profile.get("serverId")) != ref["server_id"]:
            raise AionError("Сервер профиля не совпадает с привязкой. Разбор отменён.", code="schema_changed")
        name = _text(raw_profile.get("characterName"))
        if not name:
            raise AionError("В профиле отсутствует имя персонажа. Разбор отменён.", code="schema_changed")
        gear_rows = _rows(equipment.get("equipment"), "equipmentList")
        if gear_rows is None:
            raise AionError("Оружейная не прислала список экипировки. Пустые слоты не предполагаются; повторите позднее.", code="incomplete")
        if any(_int(row.get("id")) is None or _int(row.get("id")) <= 0 or _int(row.get("slotPos")) is None for row in gear_rows):
            raise AionError("В списке экипировки нет корректных ID предметов или слотов.", code="schema_changed")
        warnings = []
        try:
            by_name, by_pc = await self._class_map(ref["region"])
        except AionError:
            by_name, by_pc = {}, {}
            warnings.append("Справочник классов недоступен; класс взят только из профиля.")
        class_name = _text(raw_profile.get("className"))
        class_info = by_pc.get(_int(raw_profile.get("pcId")), by_name.get((class_name or "").casefold(), {}))
        raw_stats = _rows(info.get("stat"), "statList")
        stats = _stats(raw_stats)
        item_level = next((_int(s.get("value")) for s in (raw_stats or []) if s.get("type") == "ItemLevel"), None)
        profile = {
            **ref, "name": name, "server_name": _text(raw_profile.get("serverName")) or server["name"],
            "class_name": class_name or class_info.get("class_name"), "class_id": class_info.get("class_id"),
            "level": _int(raw_profile.get("characterLevel")), "item_level": item_level,
            "combat_power": _int(raw_profile.get("combatPower")), "race": _text(raw_profile.get("raceName")),
            "race_id": _int(raw_profile.get("raceId")), "image_url": _text(raw_profile.get("profileImage")),
        }
        if item_level is None:
            warnings.append("Уровень предметов не получен; вместо него не используется боевая мощь.")
        if stats is None:
            warnings.append("Часть характеристик не получена.")

        async def item_detail(row: dict):
            enchant = _int(row.get("enchantLevel"))
            if enchant is None:
                return None, "В списке предметов не указан уровень заточки; подробности такого предмета не запрашиваются."
            params = {**query, "id": row["id"], "enchantLevel": enchant, "slotPos": row["slotPos"]}
            try:
                detail = await self._request(f"{SITE}/api/character/equipment/item", params)
                if _int(detail.get("id")) != _int(row["id"]) or not _text(detail.get("name")):
                    raise AionError("Предмет изменился во время загрузки или его подробности недоступны.")
                detail_enchant = _int(detail.get("enchantLevel"))
                if detail_enchant is not None and detail_enchant != enchant:
                    raise AionError("Заточка предмета изменилась во время загрузки; обновите профиль.")
                return detail, None
            except AionError as exc:
                return None, str(exc)

        boards = _rows(info.get("daevanion"), "boardList")

        async def board_detail(row: dict):
            board_id = _int(row.get("id"))
            if board_id is None or board_id <= 0:
                return None, "В оружейной отсутствует ID доски Дэваиона."
            try:
                detail = await self._request(f"{SITE}/api/character/daevanion/detail", {**query, "boardId": board_id})
                nodes = _rows(detail, "nodeList")
                if nodes is None:
                    raise AionError("Подробные узлы Дэваиона не получены.")
                if any(_int(node.get("nodeId")) is None for node in nodes):
                    raise AionError("Формат узлов Дэваиона изменился.")
                return detail, None
            except AionError as exc:
                return None, str(exc)

        item_results, board_results = await asyncio.gather(
            asyncio.gather(*(item_detail(row) for row in gear_rows)),
            asyncio.gather(*(board_detail(row) for row in (boards or []))),
        )
        normalized_gear = []
        for row, (detail, error) in zip(gear_rows, item_results):
            normalized_gear.append(_equipment_row(row, detail))
            if error:
                warnings.append(f"{_text(row.get('name')) or 'Предмет'}: {error}")
        normalized_boards = None if boards is None else []
        for row, (detail, error) in zip(boards or [], board_results):
            nodes = _rows(detail, "nodeList")
            normalized_boards.append({
                "id": _int(row.get("id")), "name": _text(row.get("name")), "open": _flag(row.get("open")),
                "open_nodes": _int(row.get("openNodeCount")), "total_nodes": _int(row.get("totalNodeCount")),
                "nodes": None if nodes is None else [{
                    "id": _int(n.get("nodeId")), "name": _text(n.get("name")),
                    "type": _text(n.get("type")), "grade": _text(n.get("grade")),
                    "open": _flag(n.get("open")), "row": _int(n.get("row")), "col": _int(n.get("col")),
                    "effects": _stats(_rows(n, "effectList")),
                } for n in nodes],
                "skill_effects": _stats(_rows(detail, "openSkillEffectList")),
                "stat_effects": _stats(_rows(detail, "openStatEffectList")),
            })
            if error:
                warnings.append(f"{_text(row.get('name')) or 'Дэваион'}: {error}")
        if boards is None:
            warnings.append("Данные Дэваиона не получены; оставшиеся узлы неизвестны.")
        raw_skills = _rows(equipment.get("skill"), "skillList")
        skills = None if raw_skills is None else [{
            "id": _int(s.get("id")), "name": _text(s.get("name")), "level": _int(s.get("skillLevel")),
            "type": (_text(s.get("category")) or "").lower() or None,
            "equipped": _flag(s.get("equip")), "acquired": _flag(s.get("acquired")),
            "need_level": _int(s.get("needLevel")), "max_level": _int(s.get("maxLevel")),
        } for s in raw_skills]
        if skills is None:
            warnings.append("Навыки не получены; их уровни и распределение очков не оцениваются.")
        raw_titles = _rows(info.get("title"), "titleList")
        titles = None if raw_titles is None else [{
            "id": _int(t.get("id")), "name": _text(t.get("name")), "grade": _text(t.get("grade")),
            "category": _text(t.get("equipCategory")), "equipped": _flag(t.get("equip")),
            "stats": _stats(_rows(t, "statList")), "equip_stats": _stats(_rows(t, "equipStatList")),
        } for t in raw_titles]
        if titles is None:
            warnings.append("Данные титулов не получены.")
        petwing = equipment.get("petwing") if isinstance(equipment.get("petwing"), dict) else {}
        return {
            "schema_version": 1, "source": "ncsoft", "profile_url": profile_url(ref),
            "fetched_at": datetime.now(timezone.utc).isoformat(), "profile": profile,
            "equipment": normalized_gear, "skills": skills, "daevanion": normalized_boards,
            "titles": titles, "stats": stats,
            "pet": deepcopy(petwing.get("pet")), "wing": deepcopy(petwing.get("wing")),
            "warnings": list(dict.fromkeys(warnings)),
            "completeness": {
                "equipment": True, "item_details": all(item["detail_loaded"] for item in normalized_gear),
                "skills": skills is not None, "daevanion": boards is not None and all(d is not None for d, _ in board_results),
                "titles": titles is not None, "stats": stats is not None,
            },
        }
