"""Evidence-first progression advice for the public AION 2 Global armory.

This module uses no LLM, game credentials, price feed, or hardcoded game caps.
It reports observations and conditional next checks, not simulated DPS or a
claim that every unfinished system should be completed.  The companion
``resources/rules.json`` records the provenance and scope of these rules.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any
from urllib.parse import urlsplit


RULESET_VERSION = "2026-10-08.1"
RULES_CHECKED_AT = "2026-10-08"
ARMORY_URL = "https://aion2.plaync.com/en-us/characters/index"

_GOALS = {"pve": "PvE", "pvp": "PvP"}
_ROLES = {"damage": "урон", "tank": "выживаемость / танк", "support": "поддержка"}
_BUDGETS = {"free": "без дополнительных трат", "low": "небольшой бюджет", "flexible": "гибкий бюджет"}
_REGIONS = {
    "eu": "eu", "europe": "eu", "naw": "naw", "nae": "nae", "na": "na",
    "us": "na", "us-west": "naw", "us-east": "nae", "na-west": "naw",
    "na-east": "nae", "north america west": "naw", "north america east": "nae",
    "north-america-west": "naw", "north-america-east": "nae",
    "la": "sa", "sa": "sa", "south america": "sa", "south-america": "sa",
    "asia": "asia", "as": "asia", "jp": "jp", "japan": "jp",
}


def _text(value: Any, default: str = "", limit: int = 180) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return default
    result = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
    # Names from a public character profile are text, never Discord mentions.
    result = result.replace("@", "@\u200b").replace("`", "′")
    return result[:limit] or default


def _int(value: Any) -> int | None:
    """Only accept an actual nonnegative integral value. Unknown is never 0."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        return int(value.strip())
    return None


def _bool(value: Any) -> bool | None:
    if value is True or (type(value) is int and value == 1):
        return True
    if value is False or (type(value) is int and value == 0):
        return False
    return None


def _rows(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _name(row: Mapping, fallback: str = "без названия") -> str:
    return _text(row.get("name"), fallback)


def _key(value: Any) -> str:
    return str(value).strip().casefold() if value is not None else ""


def _region(profile: Mapping) -> str | None:
    return _REGIONS.get(_key(profile.get("region")))


def _urls(snapshot: Mapping) -> list[str]:
    url = snapshot.get("profile_url")
    if isinstance(url, str):
        try:
            parsed = urlsplit(url)
            if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                return [url]
        except ValueError:
            pass
    return [ARMORY_URL]


def _description(row: Mapping) -> str:
    desc = _text(row.get("description")) or _text(row.get("desc"))
    if desc:
        return desc
    name, value = _text(row.get("name")), _text(row.get("value"))
    return " ".join(part for part in (name, value) if part)


def _effects(node: Mapping) -> list[str]:
    value = node.get("effects", node.get("effect_list", node.get("effectList")))
    if not isinstance(value, list):
        return []
    return [part for entry in value if (part := _description(entry) if isinstance(entry, Mapping) else _text(entry))]


def _slot_key(item: Mapping) -> str:
    value = item.get("slot")
    if value is not None:
        return str(value)
    name = _key(item.get("slot_name"))
    return "name:" + name if name else ""


def _slot_label(item: Mapping) -> str:
    return _text(item.get("slot_name"), "слот " + _text(item.get("slot"), "?"))


def _item_label(item: Mapping) -> str:
    return f"{_name(item)} ({_slot_label(item)})"


class _Report:
    def __init__(self, snapshot: dict, goal: str, role: str, budget: str):
        self.snapshot, self.goal, self.role, self.budget = snapshot, goal, role, budget
        self.actions: list[dict] = []
        self.limitations: list[str] = []
        self.source_urls = _urls(snapshot)

    def limit(self, text: str) -> None:
        if text not in self.limitations:
            self.limitations.append(text)

    def add(self, id: str, priority: int, title: str, observed: str, action: str,
            reason: str, cost: str, *, confidence: str = "факт из профиля; действие условное",
            sources: list[str] | None = None) -> None:
        urls = list(dict.fromkeys(sources or self.source_urls))
        self.actions.append({
            "id": id, "priority": priority, "title": title, "observed": observed,
            "action": action, "reason": reason, "cost": cost, "confidence": confidence,
            "sources": urls,
            "evidence_at": self.snapshot.get("fetched_at"),
            "rules_checked_at": RULES_CHECKED_AT,
        })
        self.source_urls = list(dict.fromkeys(self.source_urls + urls))


def _socket_count(item: Mapping, prefix: str) -> tuple[int, int] | None:
    """Return (filled, capacity) only when both occupancy and capacity are known.

    Missing lists and malformed entries fail closed. Duplicate slot positions
    may be multiple stat lines from one stone, so count unique positions when
    every row provides one. An explicit empty list is different from None.
    """
    capacity = _int(item.get(prefix + "_slots"))
    stones = item.get(prefix + "s")
    if capacity is None or not isinstance(stones, list):
        return None
    if any(not isinstance(row, Mapping) for row in stones):
        return None
    if not stones:
        return 0, capacity
    # Only clearly populated stat/stone rows are evidence of occupancy.
    if any(not (_text(row.get("name")) or _text(row.get("value")) or _text(row.get("id"))) for row in stones):
        return None
    positions = [row.get("slot_pos", row.get("slotPos", row.get("slot"))) for row in stones]
    if any(pos is not None for pos in positions):
        parsed = [_int(pos) for pos in positions]
        if any(pos is None for pos in parsed):
            return None
        # Both 0-based and 1-based APIs can be normalized, but mixed/outside
        # ranges are not trustworthy enough to infer missing sockets.
        indexes = set(parsed)
        if not (all(0 <= pos < capacity for pos in indexes) or all(1 <= pos <= capacity for pos in indexes)):
            return None
        filled = len(indexes)
    else:
        filled = len(stones)
    return (filled, capacity) if filled <= capacity else None


def _role_focus(goal: str, role: str) -> str:
    if role == "tank":
        return "выживаемость и удержание противника в выбранном режиме"
    if role == "support":
        return "полезность лечения / усилений и собственную выживаемость"
    return "урон по монстрам" if goal == "pve" else "урон и нужную надёжность попаданий в PvP"


def _socket_actions(report: _Report, equipment: list[dict]) -> None:
    unknown: list[str] = []
    for prefix, label, rule_id, priority in (
        ("magic_stone", "камней в экипировке", "empty_magic_sockets", 10),
        ("god_stone", "божественного камня", "empty_god_sockets", 25),
    ):
        gaps: list[tuple[dict, int, int]] = []
        for item in equipment:
            result = _socket_count(item, prefix)
            if result is None:
                if _int(item.get(prefix + "_slots")) not in (None, 0):
                    unknown.append(_item_label(item))
                continue
            filled, capacity = result
            if capacity > filled:
                gaps.append((item, filled, capacity))
        if not gaps:
            continue
        count = sum(capacity - filled for _, filled, capacity in gaps)
        sample = "; ".join(f"{_item_label(item)}: занято {filled} из {capacity}" for item, filled, capacity in gaps[:4])
        if len(gaps) > 4:
            sample += f"; ещё предметов: {len(gaps) - 4}"
        spend = (
            "Сначала проверь запас собственных камней и плату в окне установки. При бюджете без трат покупку и платную установку отложи."
            if report.budget == "free" else
            "Начни с подходящих камней из своего запаса; перед покупкой посмотри стоимость установки и план замены предмета."
        )
        report.add(
            rule_id, priority, f"Проверить свободные ячейки {label}: {count}", sample + ".",
            f"Открой перечисленные предметы и раздел установки камней. {spend} Подбирай эффект под {_role_focus(report.goal, report.role)}.",
            "Источник явно передал вместимость и заполнение ячеек. Подходящий эффект можно добавить в свободную ячейку; полезность конкретного камня нужно проверить по его описанию.",
            "Нужны камни; плата и цены в оружейной отсутствуют. Сумма не рассчитана.",
        )
    if unknown:
        report.limit("У части предметов неизвестно заполнение ячеек. Такие ячейки не считаются пустыми, даже если их количество известно.")


def _enchant_actions(report: _Report, equipment: list[dict]) -> None:
    candidates: list[tuple[dict, int, int]] = []
    unknown_max = False
    for item in equipment:
        current, cap = _int(item.get("enchant_level")), _int(item.get("max_enchant_level"))
        if current is not None and cap is None:
            unknown_max = True
        if current is None or cap is None or cap <= current or item.get("enchantable") is False:
            continue
        # No universal '+10', '+11', '+15' threshold: use this item's response.
        candidates.append((item, current, cap))
    if unknown_max:
        report.limit("Для предметов без подтверждённого максимума заточки бот не задаёт целевой уровень.")
    if not candidates:
        return
    # A display order, not a DPS score. First show least-enchanted known items.
    candidates.sort(key=lambda row: (row[1], _slot_key(row[0])))
    shown = candidates[:3]
    observed = "; ".join(f"{_item_label(item)}: +{cur}, максимум в данных +{cap}" for item, cur, cap in shown) + "."
    next_steps = "; ".join(f"{_name(item)}: проверить переход +{cur} → +{cur + 1}" for item, cur, _ in shown)
    if report.budget == "free":
        action = f"{next_steps}. Пока только сравни прирост и стоимость в окне усиления; платное улучшение оставь на другой бюджет."
        priority = 70
    else:
        action = f"{next_steps}. Сравни показываемый клиентом прирост под {_role_focus(report.goal, report.role)}, цену и вероятность. Выбери один полезный шаг в пределах своего бюджета."
        priority = 40
    report.add(
        "enchant_review", priority, "Оценить следующий шаг заточки", observed, action,
        "До известного предела есть запас усиления. Сам по себе низкий плюс не доказывает выгодность вложений; максимум предмета не является обязательной целью.",
        "Расход материалов и валюты проверяется в игре. Проценты успеха, стоимость и прирост DPS не рассчитаны.",
    )


def _closed_nodes(boards: list[dict]) -> list[tuple[dict, dict, list[str]]]:
    result = []
    for board in boards:
        if _bool(board.get("open")) is not True:
            continue
        for node in _rows(board.get("nodes")):
            effects = _effects(node)
            if _bool(node.get("open")) is not False or not effects:
                continue
            if _key(node.get("type")) in {"none", "empty", "null"}:
                continue
            # Blank geometry cells are not progression opportunities.
            if not _text(node.get("name")):
                continue
            result.append((board, node, effects))
    return result


def _location(board: Mapping, node: Mapping) -> str:
    label = _name(board, "доска")
    row, col = _int(node.get("row")), _int(node.get("col"))
    if row is not None and col is not None:
        label += f", строка {row}, столбец {col}"
    return label


def _node_for_skill(effects: list[str], skill_name: str) -> bool:
    if not skill_name:
        return False
    # An exact skill-name boundary prevents 'Strike' matching 'Shield Strike'.
    pattern = re.compile(r"^" + re.escape(skill_name) + r"\s*\+\s*\d+(?:\s|$)", re.I)
    return any(pattern.search(effect) for effect in effects)


def _role_stat_matches(effects: list[str], goal: str, role: str) -> bool:
    text = " ".join(effects).casefold()
    if (goal == "pve" and re.search(r"\bpvp\b", text)) or (goal == "pvp" and re.search(r"\bpve\b", text)):
        return False
    if role == "tank":
        return any(term in text for term in ("defense", "damage tolerance", "block", "hp ", "hp +", "max hp", "защит", "блок", "здоров"))
    if role == "support":
        return any(term in text for term in ("healing", "heal boost", "cooldown", "mp cost", "лечен", "исцелен", "перезаряд"))
    return any(term in text for term in ("attack bonus", "attack +", "damage boost", "combat speed", "critical hit +", "атака +", "урон +", "скорость боя"))


def _daevanion_actions(report: _Report, boards: list[dict], skills: list[dict]) -> None:
    nodes = _closed_nodes(boards)
    active_skills = [s for s in skills if _bool(s.get("equipped")) is True and _text(s.get("name"))]
    matches: list[tuple[dict, dict, list[str], dict]] = []
    for board, node, effects in nodes:
        for skill in active_skills:
            if _node_for_skill(effects, _text(skill.get("name"))):
                matches.append((board, node, effects, skill))
                break
    if matches:
        shown = matches[:3]
        observed = "; ".join(f"{_location(board, node)}: закрыт «{_name(node)}», эффект {', '.join(effects)}" for board, node, effects, _ in shown) + "."
        report.add(
            "daevanion_equipped_skills", 20, "Проверить узлы для выбранных умений", observed,
            "Открой указанные доски Дэвианиона и посмотри путь к этим узлам. Если умение входит в твою основную ротацию, сравни полную цену пути и доступные очки до вложений.",
            "Эффект закрытого узла относится к умению, которое источник пометил выбранным. Это конкретное направление усиления текущего набора; важность умения и доступность пути ещё нужно оценить.",
            "Запас очков / ресурсов и цена пути в профиле не передаются. Бесплатность открытия не предполагается.",
        )
    stat_nodes = [(board, node, effects) for board, node, effects in nodes
                  if _key(node.get("type")) not in {"skilllevel", "skill", "skill_level"}
                  and _role_stat_matches(effects, report.goal, report.role)]
    if stat_nodes:
        shown = stat_nodes[:3]
        observed = "; ".join(f"{_location(board, node)}: закрытый узел «{_name(node)}», {', '.join(effects)}" for board, node, effects in shown) + "."
        report.add(
            "daevanion_role_stats", 30, f"Проверить узлы под роль «{_ROLES[report.role]}»", observed,
            "Сравни эти эффекты в Дэвианионе с нуждами своего билда и ценой полного пути. Выбери доступный узел только после проверки очков и условий открытия.",
            "Направление параметров соответствует выбранной роли. Порядок здесь служит списком проверок; он не доказывает максимальный прирост урона или выживаемости.",
            "Цена пути и свободные ресурсы неизвестны; точный бюджет не рассчитан.",
        )
    if boards and not matches and not stat_nodes:
        partial = [(board, _int(board.get("open_nodes")), _int(board.get("total_nodes"))) for board in boards if _bool(board.get("open")) is True]
        partial = [(b, opened, total) for b, opened, total in partial if opened is not None and total is not None and 0 <= opened < total]
        if partial:
            observed = "; ".join(f"{_name(b)}: открыто {opened} из {total} узлов" for b, opened, total in partial[:3]) + "."
            report.add(
                "daevanion_review", 60, "Выбрать следующую цель в Дэвианионе", observed,
                f"Открой эти доски и найди доступный путь к эффекту под {_role_focus(report.goal, report.role)} либо к основному умению. Сначала проверь эффект узла и его стоимость в игре.",
                "Видно состояние досок, но подтверждённого подходящего узла в ответе нет. Заполнение доски на 100% само по себе не является целью.",
                "Точный путь, стоимость и свободные очки неизвестны.", confidence="подтверждены только счётчики досок",
            )
    if boards:
        report.limit("Оружейная не подтверждает свободные очки Дэвианиона и доступность маршрута: закрытый узел может требовать промежуточных вложений.")


def _title_actions(report: _Report, titles: list[dict]) -> None:
    opposite = "pvp" if report.goal == "pve" else "pve"
    relevant = []
    for title in titles:
        if _bool(title.get("equipped")) is not True:
            continue
        raw = title.get("equip_stats", title.get("stats"))
        desc = [_description(row) for row in _rows(raw)]
        opposite_effects = [text for text in desc if re.search(r"\b" + opposite + r"\b", text, re.I)]
        if opposite_effects:
            relevant.append((title, opposite_effects))
    if relevant:
        observed = "; ".join(f"выбран «{_name(title)}»: {', '.join(effects)}" for title, effects in relevant[:3]) + "."
        report.add(
            "title_goal_review", 15, f"Сверить титулы с целью {_GOALS[report.goal]}", observed,
            "В меню титулов сравни этот эффект с уже открытыми вариантами для нужного режима. Меняй титул после сравнения всех его бонусов и возможной платы в интерфейсе.",
            f"У выбранного титула явно указан бонус для {opposite.upper()}. Это повод проверить комплект под {_GOALS[report.goal]}; остальные его эффекты также могут быть полезны.",
            "Покупка нового титула не предлагается; владение альтернативами и стоимость смены проверяются в игре.",
        )


def _reference_allowed(report: _Report, reference: Mapping) -> bool:
    own = report.snapshot.get("profile") or {}
    other = reference.get("profile") or {}
    own_region, other_region = _region(own), _region(other)
    if own_region is None or other_region is None or own_region != other_region:
        report.limit("Сравнение не выполнено: ориентир должен быть из того же региона Global, что и твой персонаж.")
        return False
    own_id, other_id = _int(own.get("class_id")), _int(other.get("class_id"))
    if own_id not in (None, 0) and other_id not in (None, 0):
        same_class = own_id == other_id
    else:
        same_class = bool(_key(own.get("class_name"))) and _key(own.get("class_name")) == _key(other.get("class_name"))
    if not same_class:
        report.limit("Сравнение не выполнено: нужен ориентир того же класса с подтверждённым названием или ID класса.")
        return False
    context = reference.get("coach_context")
    if not isinstance(context, Mapping) or context.get("goal") != report.goal or context.get("role") != report.role:
        report.limit("Сравнение не выполнено: для ориентира нужно явно выбрать тот же PvE/PvP режим и ту же роль.")
        return False
    report.limit("Ориентир выбран пользователем для этого режима и роли. Чужой профиль не доказывает оптимальность сборки; его фактическое назначение оружейная не подтверждает.")
    return True


def _reference_actions(report: _Report, reference: Mapping) -> None:
    if not _reference_allowed(report, reference):
        return
    sources = list(dict.fromkeys(_urls(report.snapshot) + _urls(reference)))
    other_profile = reference.get("profile") or {}
    other_name = _name(other_profile, "ориентир")
    own_equipment = {_slot_key(item): item for item in _rows(report.snapshot.get("equipment")) if _slot_key(item)}
    changed, same_items = [], []
    for other in _rows(reference.get("equipment")):
        own = own_equipment.get(_slot_key(other))
        if own is None:
            continue
        own_id, other_id = own.get("id"), other.get("id")
        if own_id in (None, 0, "0") or other_id in (None, 0, "0"):
            continue
        if str(own_id) != str(other_id):
            changed.append((own, other))
        else:
            cur, ref_level, cap = _int(own.get("enchant_level")), _int(other.get("enchant_level")), _int(own.get("max_enchant_level"))
            if cur is not None and ref_level is not None and cap is not None and cur < ref_level <= cap:
                same_items.append((own, cur, ref_level))
    if changed:
        observed = "; ".join(f"{_slot_label(own)}: у тебя «{_name(own)}», у {other_name} «{_name(other)}»" for own, other in changed[:3]) + "."
        report.add(
            "reference_equipment", 45, "Сравнить варианты предметов с ориентиром", observed,
            "Открой обе карточки и сравни основные / дополнительные параметры, умения, комплектные эффекты и доступность получения. Запиши замену в план только если она решает задачу твоего билда.",
            "Это реальные различия в одинаковых слотах персонажей одного класса. Более высокий ГС или другая редкость сами по себе не доказывают, что замена выгодна.",
            "Стоимость получения и переноса усилений не известна; покупка автоматически не рекомендуется.", sources=sources,
        )
    if same_items:
        observed = "; ".join(f"{_item_label(item)}: у тебя +{cur}, у {other_name} +{level}" for item, cur, level in same_items[:3]) + "."
        report.add(
            "reference_enchant", 50, "Сопоставить заточку одинаковых предметов", observed,
            "Проверь цену и эффект следующей ступени на собственном предмете. Уровень ориентира используй как вариант для оценки, а не обязательную цель.",
            "Совпадают ID предметов и известен доступный максимум. Разница в заточке подтверждена, но её эффективность на единицу ресурсов не рассчитана.",
            "Проверка в интерфейсе; расход только после оценки своего бюджета.", sources=sources,
        )
    own_skills = {str(s["id"]): s for s in _rows(report.snapshot.get("skills")) if s.get("id") is not None}
    skill_diffs = []
    for other in _rows(reference.get("skills")):
        own = own_skills.get(str(other.get("id")))
        if own is None:
            continue
        active_pair = _bool(own.get("equipped")) is True and _bool(other.get("equipped")) is True
        passive_pair = _key(own.get("type")) == "passive" and _key(other.get("type")) == "passive"
        if not (active_pair or passive_pair):
            continue
        cur, target = _int(own.get("level")), _int(other.get("level"))
        if cur is not None and target is not None and 0 < cur < target:
            skill_diffs.append((own, cur, target))
    if skill_diffs:
        observed = "; ".join(f"{_name(skill)}: у тебя {cur}, у {other_name} {target}" for skill, cur, target in skill_diffs[:3]) + "."
        report.add(
            "reference_skills", 35, "Проверить источники уровней умений", observed,
            "Сравни эти умения в двух профилях и в игре. Проверь, какие уровни дают собственные очки, экипировка и Дэвианион, затем выбери доступный источник для важного умения.",
            "Подтверждена разница одного и того же умения. Итоговый уровень не показывает, сколько свободных очков нужно, и не означает, что именно это умение обязательно повышать первым.",
            "Затраты зависят от источника уровня; свободные очки, ресурсы и цены не видны.", sources=sources,
        )
    own_boards = {str(b["id"]): b for b in _rows(report.snapshot.get("daevanion")) if b.get("id") is not None}
    board_diffs = []
    for other in _rows(reference.get("daevanion")):
        own = own_boards.get(str(other.get("id")))
        if own is None or _bool(own.get("open")) is not True or _bool(other.get("open")) is not True:
            continue
        own_nodes = {str(n.get("id", n.get("nodeId"))): n for n in _rows(own.get("nodes")) if n.get("id", n.get("nodeId")) is not None}
        for other_node in _rows(other.get("nodes")):
            own_node = own_nodes.get(str(other_node.get("id", other_node.get("nodeId"))))
            if own_node is None or _bool(own_node.get("open")) is not False or _bool(other_node.get("open")) is not True:
                continue
            effects = _effects(own_node)
            if effects and _text(own_node.get("name")) and _key(own_node.get("type")) not in {"none", "empty", "null"}:
                board_diffs.append((own, own_node, effects))
    if board_diffs:
        observed = "; ".join(f"{_location(board, node)}: у тебя закрыт, у {other_name} открыт — {', '.join(effects)}" for board, node, effects in board_diffs[:3]) + "."
        report.add(
            "reference_daevanion", 55, "Сопоставить конкретные узлы Дэвианиона", observed,
            "Сравни полные пути и роль каждого эффекта в своей сборке. Выбери только полезный тебе узел с доступной стоимостью.",
            "Сопоставлены одинаковые ID досок и узлов. Количество открытых узлов или чужой маршрут сами по себе не определяют оптимальный билд.",
            "Маршрут и запас ресурсов проверяются в игре; точная цена неизвестна.", sources=sources,
        )
    if not (changed or same_items or skill_diffs or board_diffs):
        report.limit("В доступных полях ориентира не найдено подтверждённых различий, из которых можно составить дополнительный план.")


def analyze(snapshot: dict, *, goal: str = "pve", role: str = "damage", budget: str = "low", reference: dict | None = None) -> dict:
    """Build a structured Russian report without changing either snapshot.

    Lower ``priority`` means earlier display. The UI should initially show at
    most five actions. Reference comparisons require same known class/region
    and an explicit ``reference['coach_context'] = {'goal': ..., 'role': ...}``.
    This context describes the user's choice, never an inferred game loadout.
    """
    if not isinstance(snapshot, dict):
        raise TypeError("snapshot должен быть объектом JSON")
    if goal not in _GOALS or role not in _ROLES or budget not in _BUDGETS:
        raise ValueError("Неизвестная цель, роль или бюджет")
    report = _Report(snapshot, goal, role, budget)
    profile = snapshot.get("profile") if isinstance(snapshot.get("profile"), Mapping) else {}
    name = _name(profile, "Персонаж")
    context = f"{_GOALS[goal]} · {_ROLES[role]} · {_BUDGETS[budget]}"
    known_region = _region(profile)
    if profile.get("region") and known_region is None:
        report.limit("Эта версия советника рассчитана на Global. Регион профиля не распознан как поддерживаемый; игровые рекомендации не построены.")
    else:
        equipment, skills = _rows(snapshot.get("equipment")), _rows(snapshot.get("skills"))
        _socket_actions(report, equipment)
        _enchant_actions(report, equipment)
        _daevanion_actions(report, _rows(snapshot.get("daevanion")), skills)
        _title_actions(report, _rows(snapshot.get("titles")))
        if reference is not None:
            if not isinstance(reference, dict):
                raise TypeError("reference должен быть объектом JSON")
            _reference_actions(report, reference)
        completeness = snapshot.get("completeness") or {}
        if isinstance(completeness, Mapping):
            missing = [name for key, name in (("equipment", "экипировка"), ("item_details", "детали предметов"), ("skills", "умения"), ("daevanion", "Дэвианион"), ("titles", "титулы")) if completeness.get(key) is False]
            if missing:
                report.limit("Источник передал неполные разделы: " + ", ".join(missing) + ". Отсутствие данных не считается недостатком персонажа.")
    for warning in snapshot.get("warnings", []) if isinstance(snapshot.get("warnings"), list) else []:
        if _text(warning):
            report.limit(_text(warning, limit=350))
    report.limit("Профиль не показывает инвентарь, запас валюты, выполненные задания и все варианты получения предметов. Точная смета и маршрут фарма не рассчитаны.")
    report.limit("Время загрузки профиля не доказывает свежесть данных игрового сервера. После смены экипировки обнови разбор и проверь значения.")
    report.limit("Приоритеты — порядок полезных проверок по видимым данным. Оптимальный билд, прирост DPS и актуальные цены здесь не моделируются.")
    report.actions.sort(key=lambda action: (action["priority"], action["id"]))
    if report.actions:
        summary = f"{context}. По видимым данным найдено направлений для проверки: {len(report.actions)}. Сначала посмотри первые три; перед расходом ресурсов проверь условия в игре."
    else:
        summary = f"{context}. По доступным полям конкретные улучшения не подтверждены. Это не оценка максимальной прокачки: обнови профиль или добавь ориентир своего класса через /сравнить."
    return {
        "headline": f"План развития: {name}", "summary": summary,
        "actions": report.actions, "limitations": report.limitations,
        "source_urls": report.source_urls, "ruleset_version": RULESET_VERSION,
    }
