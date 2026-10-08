"""KAZEROS: Discord UI. For hosting, start main.py."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import discord
from discord import app_commands

from kazeros.aion import AionClient, AionError
from kazeros.coach import analyze
from kazeros.config import BUDGETS, GOALS, REGIONS, ROLES, ROOT, ConfigError, default_preferences, load_config, load_token, normalize_region, data_dir
from kazeros.store import Store
from kazeros.instance import InstanceLock
from kazeros.network import proxy_for
from kazeros.views import COLOR, class_name, panel_embed, parse_character_url, profile_embed, report_embed, text

LOG = logging.getLogger("kazeros")


class UserError(ValueError):
    pass


async def reply(interaction: discord.Interaction, message=None, **kwargs):
    kwargs.setdefault("ephemeral", True)
    kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
    if interaction.response.is_done():
        return await interaction.followup.send(message, **kwargs)
    return await interaction.response.send_message(message, **kwargs)


async def error_reply(interaction, error):
    if isinstance(error, (AionError, ConfigError, UserError)):
        message = str(error)
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = "Подожди несколько секунд и повтори запрос."
    elif isinstance(error, discord.Forbidden):
        message = "Не хватает прав Discord. Для бота включи View Channels, Send Messages и Embed Links в этом канале."
    elif isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        message = "Источник отвечает дольше обычного. Повтори запрос через минуту."
    else:
        # Exception bodies from HTTP libraries may carry request context. Log only type.
        LOG.error("Operation failed: %s", type(error).__name__)
        message = "Запрос не удалось завершить. Привязка сохранена; попробуй ещё раз. Если ошибка повторяется, администратору нужно проверить логи Bothost или выполнить python check.py."
    try:
        await reply(interaction, text(message, 1850))
    except discord.HTTPException:
        LOG.warning("Could not deliver an error response")


class CoachTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        if interaction.guild_id != self.client.config["discord_guild_id"]:
            await reply(interaction, "Открой канал сервера Discord, для которого настроен этот бот.")
            return False
        return True

    async def on_error(self, interaction, error):
        await error_reply(interaction, getattr(error, "original", error))


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        await error_reply(interaction, error)


class OwnedView(SafeView):
    def __init__(self, bot, owner_id, *, timeout=600):
        super().__init__(timeout=timeout)
        self.bot, self.owner_id = bot, owner_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id or interaction.guild_id != self.bot.config["discord_guild_id"]:
            await reply(interaction, "Эта карточка относится к другому участнику. Открой /разбор для своего персонажа.")
            return False
        return True


class CharacterModal(discord.ui.Modal):
    def __init__(self, bot, *, mode="bind"):
        super().__init__(title="Твой персонаж" if mode == "bind" else "Образец для сравнения", timeout=600)
        self.bot, self.mode = bot, mode
        self.query = discord.ui.TextInput(label="Ник или ссылка на официальную оружейную", placeholder="Ник персонажа / https://aion2.plaync.com/…", max_length=700)
        self.region = discord.ui.TextInput(label="Регион: eu / nae / naw / as / la", default=bot.config["game_region"], max_length=30)
        self.server = discord.ui.TextInput(label="Игровой сервер; пусто — все серверы региона", default=bot.config["game_server_name"] if bot.config.get("game_server_id") else "", required=False, max_length=80)
        self.add_item(self.query)
        self.add_item(self.region)
        self.add_item(self.server)

    async def on_submit(self, interaction):
        await self.bot.search(interaction, str(self.query.value), str(self.region.value), str(self.server.value), mode=self.mode)

    async def on_error(self, interaction, error):
        await error_reply(interaction, error)


class CharacterSelect(discord.ui.Select):
    def __init__(self, refs):
        options = []
        for index, ref in enumerate(refs[:25]):
            name = str(ref.get("name") or "Персонаж по ссылке")
            detail = f"{ref.get('server_name') or ref.get('server_id')} · {ref.get('class_name') or 'класс'} · ур. {ref.get('level') if ref.get('level') is not None else '?'}"
            options.append(discord.SelectOption(label=name[:100], description=detail[:100], value=str(index)))
        super().__init__(placeholder="Выбери нужного персонажа", min_values=1, max_values=1, options=options)

    async def callback(self, interaction):
        view = self.view
        ref = view.refs[int(self.values[0])]
        if view.mode == "bind":
            await view.bot.bind(interaction, ref)
        else:
            await view.bot.analyze_bound(interaction, reference_ref=ref)
        # All duplicate clicks must go through the current account checks again.
        self.disabled = True


class CandidateView(OwnedView):
    def __init__(self, bot, owner_id, refs, *, mode="bind"):
        super().__init__(bot, owner_id)
        self.refs, self.mode = refs[:25], mode
        self.add_item(CharacterSelect(self.refs))


class PreferencesSelect(discord.ui.Select):
    def __init__(self, field, labels, current, row):
        self.field = field
        options = [discord.SelectOption(label=label, value=value, default=value == current) for value, label in labels.items()]
        super().__init__(placeholder={"goal": "Цель", "role": "Роль", "budget": "Бюджет"}[field], options=options, row=row)

    async def callback(self, interaction):
        self.view.preferences[self.field] = self.values[0]
        for option in self.options:
            option.default = option.value == self.values[0]
        await interaction.response.edit_message(view=self.view)


class PreferencesView(OwnedView):
    def __init__(self, bot, owner_id, preferences):
        super().__init__(bot, owner_id)
        self.preferences = dict(preferences)
        self.add_item(PreferencesSelect("goal", GOALS, preferences["goal"], 0))
        self.add_item(PreferencesSelect("role", ROLES, preferences["role"], 1))
        self.add_item(PreferencesSelect("budget", BUDGETS, preferences["budget"], 2))

    @discord.ui.button(label="Сохранить и разобрать", style=discord.ButtonStyle.primary, row=3)
    async def apply(self, interaction, button):
        # A preference view can outlive /отвязать. Recheck that a binding exists.
        if self.bot.record(interaction) is None:
            raise UserError("Сначала привяжи персонажа кнопкой «Персонаж».")
        if (interaction.guild_id, interaction.user.id) in self.bot._busy:
            raise UserError("Дождись текущего разбора и затем сохрани новую цель.")
        self.bot.store.set_preferences(interaction.guild_id, interaction.user.id, self.preferences)
        await self.bot.analyze_bound(interaction)


class ReportView(OwnedView):
    def __init__(self, bot, owner_id, snapshot, report, preferences, previous=None, *, reference_ref=None):
        super().__init__(bot, owner_id)
        self.snapshot, self.report, self.preferences = snapshot, report, preferences
        self.previous, self.reference_ref, self.page = previous, reference_ref, 0
        if len(report.get("actions") or []) <= 5:
            self.remove_item(self.more)

    @discord.ui.button(label="Обновить", style=discord.ButtonStyle.primary, row=0)
    async def refresh(self, interaction, button):
        await self.bot.analyze_bound(interaction, force=True, reference_ref=self.reference_ref)

    @discord.ui.button(label="Цель и бюджет", style=discord.ButtonStyle.secondary, row=0)
    async def preferences_button(self, interaction, button):
        await self.bot.preferences(interaction)

    @discord.ui.button(label="Экипировка", style=discord.ButtonStyle.secondary, row=0)
    async def gear(self, interaction, button):
        await reply(interaction, embed=profile_embed(self.snapshot, self.previous))

    @discord.ui.button(label="Сравнить с образцом", style=discord.ButtonStyle.secondary, row=1)
    async def compare(self, interaction, button):
        await interaction.response.send_modal(CharacterModal(self.bot, mode="compare"))

    @discord.ui.button(label="Следующие рекомендации", style=discord.ButtonStyle.secondary, row=1)
    async def more(self, interaction, button):
        count = max(1, (len(self.report.get("actions") or []) + 4) // 5)
        self.page = (self.page + 1) % count
        await interaction.response.edit_message(embed=report_embed(self.snapshot, self.report, self.preferences, self.previous, page=self.page), view=self)


class ForgetView(OwnedView):
    @discord.ui.button(label="Удалить мою привязку и историю", style=discord.ButtonStyle.danger)
    async def forget(self, interaction, button):
        async with self.bot.user_job(interaction):
            self.bot.store.forget(interaction.guild_id, interaction.user.id)
        await interaction.response.edit_message(content="Привязка, настройки и сохранённые результаты удалены. Персонажа можно привязать заново через /персонаж.", view=None)


class PanelView(SafeView):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.bot.config["discord_guild_id"]:
            await reply(interaction, "Панель работает на настроенном сервере Discord.")
            return False
        return True

    @discord.ui.button(label="Мой персонаж", emoji="👤", style=discord.ButtonStyle.primary, custom_id="kazeros:v1:bind")
    async def bind(self, interaction, button):
        await interaction.response.send_modal(CharacterModal(self.bot))

    @discord.ui.button(label="Цель и роль", emoji="🎯", style=discord.ButtonStyle.secondary, custom_id="kazeros:v1:preferences")
    async def preferences(self, interaction, button):
        await self.bot.preferences(interaction)

    @discord.ui.button(label="План усиления", emoji="📈", style=discord.ButtonStyle.success, custom_id="kazeros:v1:analyze")
    async def analyze_button(self, interaction, button):
        await self.bot.analyze_bound(interaction)

    @discord.ui.button(label="Как пользоваться", emoji="❔", style=discord.ButtonStyle.secondary, custom_id="kazeros:v1:help")
    async def help_button(self, interaction, button):
        await self.bot.help(interaction)


class CoachBot(discord.Client):
    def __init__(self, config: dict, *, store_path: Path | None = None, api=None):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none(), proxy=proxy_for("https://discord.com"))
        self.config = config
        self.tree = CoachTree(self)
        self.store = Store(store_path or data_dir() / "coach.sqlite3")
        self.api = api
        self._busy = set()
        self._capacity = asyncio.Semaphore(4)
        self._closed_storage = False
        self._commands()

    async def setup_hook(self):
        if self.api is None:
            self.api = AionClient(cache_seconds=self.config["cache_minutes"] * 60)
        self.add_view(PanelView(self))
        guild = discord.Object(id=self.config["discord_guild_id"])
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self):
        print(f"\nБот готов: {self.user}. Команды доступны на сервере Discord {self.config['discord_guild_id']}.")
        print("В нужном канале выполни /панель. На хостинге управление запуском — через панель Bothost.")
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="прокачку AION 2 Global"))

    async def close(self):
        if self.api is not None:
            await self.api.close()
        if not self._closed_storage:
            self.store.close()
            self._closed_storage = True
        await super().close()

    def record(self, interaction):
        return self.store.get(interaction.guild_id, interaction.user.id)

    @asynccontextmanager
    async def user_job(self, interaction):
        key = (interaction.guild_id, interaction.user.id)
        if key in self._busy:
            raise UserError("Твой предыдущий запрос ещё выполняется. Дождись результата.")
        self._busy.add(key)
        try:
            async with self._capacity:
                yield
        finally:
            self._busy.discard(key)

    async def lookup(self, query, region, server):
        query = query.strip()
        if not query:
            raise UserError("Введи ник персонажа или ссылку на его официальную оружейную.")
        if len(query) > 700:
            raise UserError("Слишком длинный ник или ссылка.")
        ref = parse_character_url(query)
        if ref:
            snapshot = await self.api.get_snapshot(ref)
            return [dict(snapshot["profile"])]
        if len(query) > 100:
            raise UserError("Проверь ник персонажа — введено слишком много символов.")
        region = normalize_region(region)
        server_id = None
        if str(server or "").strip() and str(server).strip().casefold() not in {"0", "all", "все", "все серверы региона"}:
            servers = await self.api.list_servers(region)
            match = next((s for s in servers if str(s["id"]) == str(server).strip() or s["name"].casefold() == str(server).strip().casefold()), None)
            if not match:
                import difflib
                near = difflib.get_close_matches(str(server), [s["name"] for s in servers], n=3, cutoff=0.35)
                suffix = " Похожие названия: " + ", ".join(near) + "." if near else ""
                raise UserError("Этот игровой сервер не найден в выбранном регионе." + suffix + " Можно оставить поле сервера пустым и выбрать персонажа из списка.")
            server_id = match["id"]
        refs = await self.api.search_characters(query, region, server_id)
        exact = [r for r in refs if str(r.get("name", "")).casefold() == query.casefold()]
        selected = exact or refs
        if len(selected) > 25:
            raise UserError("Найдено больше 25 персонажей. Укажи точный ник и игровой сервер, чтобы выбрать нужного.")
        return selected

    async def search(self, interaction, query, region, server, *, mode="bind"):
        if mode == "compare" and self.record(interaction) is None:
            raise UserError("Сначала привяжи своего персонажа через /персонаж.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.user_job(interaction):
            refs = await self.lookup(query, region, server)
        if not refs:
            raise UserError("Персонаж не найден. Проверь ник, регион и игровой сервер. Если персонаж новый, попробуй позже или вставь прямую ссылку на его оружейную.")
        intro = "Выбери своего персонажа — привязка будет сохранена." if mode == "bind" else "Выбери образец того же класса и региона. Его сборку будем сравнивать в рамках твоей выбранной цели; это ориентир, а не подтверждение оптимального билда."
        await reply(interaction, intro, view=CandidateView(self, interaction.user.id, refs, mode=mode))

    async def bind(self, interaction, ref):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.user_job(interaction):
            snapshot = await self.api.get_snapshot(ref)
            current = self.record(interaction)
            prefs = current["preferences"] if current else default_preferences(self.config)
            self.store.bind(interaction.guild_id, interaction.user.id, dict(snapshot["profile"]), prefs, snapshot)
        await reply(interaction, f"Персонаж **{text(snapshot['profile'].get('name'))}** привязан. Выбери цель, роль и бюджет и нажми «Сохранить и разобрать».", embed=profile_embed(snapshot), view=PreferencesView(self, interaction.user.id, prefs))

    async def preferences(self, interaction):
        row = self.record(interaction)
        if row is None:
            await interaction.response.send_modal(CharacterModal(self))
            return
        await reply(interaction, "Выбери цель, роль и бюджет. Затем нажми «Сохранить и разобрать». Указанный бюджет — ориентир; доступ к твоей кине и материалам не требуется.", view=PreferencesView(self, interaction.user.id, row["preferences"]))

    async def analyze_bound(self, interaction, *, force=False, reference_ref=None, preferences=None):
        row = self.record(interaction)
        if row is None:
            if interaction.response.is_done():
                raise UserError("Сначала привяжи своего персонажа через /персонаж.")
            await interaction.response.send_modal(CharacterModal(self))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.user_job(interaction):
            snapshot = await self.api.get_snapshot(row["ref"], force=force)
            prefs = dict(row["preferences"])
            if preferences:
                prefs.update({key: value for key, value in preferences.items() if value is not None})
            reference = None
            if reference_ref:
                reference = await self.api.get_snapshot(reference_ref, force=force)
                reference = dict(reference)
                reference["coach_context"] = {"goal": prefs["goal"], "role": prefs["role"]}
            report = analyze(snapshot, goal=prefs["goal"], role=prefs["role"], budget=prefs["budget"], reference=reference)
            self.store.set_preferences(interaction.guild_id, interaction.user.id, prefs)
            self.store.save_snapshot(interaction.guild_id, interaction.user.id, snapshot)
            saved = self.record(interaction)
            previous = saved.get("previous_snapshot")
        await reply(interaction, embed=report_embed(snapshot, report, prefs, previous), view=ReportView(self, interaction.user.id, snapshot, report, prefs, previous, reference_ref=reference_ref))

    async def help(self, interaction):
        embed = discord.Embed(title="Как пользоваться KAZEROS", color=COLOR)
        embed.description = (
            "**/персонаж** — привязать ник или ссылку на официальную оружейную.\n"
            "**/разбор** — получить профиль и приоритеты.\n"
            "**/сравнить** — выбрать персонажа того же класса как образец.\n"
            "**/отвязать** — удалить свою привязку и историю.\n"
            "**/панель** — разместить общую панель (для управляющих сервером).\n\n"
            "В карточке: **Цель и бюджет**, **Экипировка**, **Обновить**.\n\n"
            "Результаты видны только тебе. Правила используют доступные факты оружейной; отсутствие данных не считается отсутствием улучшения. Цены, твой инвентарь и точный прирост DPS не определяются.\n\n"
            "После изменения экипировки подожди обновления оружейной и нажми «Обновить». Время загрузки указано в отчёте. Кнопки личной карточки активны 10 минут; затем открой /разбор заново."
        )
        embed.set_footer(text="Global · Корея и Тайвань не подключены · Без платного AI-ключа")
        await reply(interaction, embed=embed)

    def _commands(self):
        @self.tree.command(name="панель", description="Разместить общую панель KAZEROS в этом канале")
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        async def panel(interaction: discord.Interaction):
            if not interaction.permissions.manage_guild:
                raise UserError("Панель размещает участник с правом «Управление сервером».")
            await interaction.response.send_message(embed=panel_embed(self.config), view=PanelView(self), allowed_mentions=discord.AllowedMentions.none())

        @self.tree.command(name="персонаж", description="Привязать персонажа AION 2 Global по нику или ссылке")
        @app_commands.guild_only()
        @app_commands.describe(имя="Ник или ссылка на официальную оружейную; без аргумента откроется форма")
        async def character(interaction: discord.Interaction, имя: str | None = None):
            if имя:
                await self.search(interaction, имя, self.config["game_region"], self.config.get("game_server_id"))
            else:
                await interaction.response.send_modal(CharacterModal(self))

        @self.tree.command(name="разбор", description="Получить персональные приоритеты усиления")
        @app_commands.guild_only()
        @app_commands.describe(цель="PvE или PvP", роль="Приоритет сборки", бюджет="Ориентир по затратам")
        @app_commands.choices(
            цель=[app_commands.Choice(name=label, value=value) for value, label in GOALS.items()],
            роль=[app_commands.Choice(name=label, value=value) for value, label in ROLES.items()],
            бюджет=[app_commands.Choice(name=label, value=value) for value, label in BUDGETS.items()],
        )
        async def review(interaction: discord.Interaction, цель: str | None = None, роль: str | None = None, бюджет: str | None = None):
            await self.analyze_bound(interaction, preferences={"goal": цель, "role": роль, "budget": бюджет})

        @self.tree.command(name="сравнить", description="Разобрать отличия от выбранного образца того же класса")
        @app_commands.guild_only()
        @app_commands.describe(образец="Ник в вашем игровом сервере или ссылка на официальную оружейную")
        async def compare(interaction: discord.Interaction, образец: str | None = None):
            if self.record(interaction) is None:
                raise UserError("Сначала привяжи своего персонажа через /персонаж.")
            if образец:
                await self.search(interaction, образец, self.config["game_region"], self.config.get("game_server_id"), mode="compare")
            else:
                await interaction.response.send_modal(CharacterModal(self, mode="compare"))

        @self.tree.command(name="помощь", description="Показать команды и объяснение разбора")
        @app_commands.guild_only()
        async def help_command(interaction: discord.Interaction):
            await self.help(interaction)

        @self.tree.command(name="отвязать", description="Удалить свою привязку персонажа и сохранённые результаты")
        @app_commands.guild_only()
        async def forget(interaction: discord.Interaction):
            await reply(interaction, "Удалить привязку, личные настройки и два сохранённых результата этого персонажа?", view=ForgetView(self, interaction.user.id))


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        config, token = load_config(), load_token()
    except ConfigError as error:
        print(error)
        return 1
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    bot = CoachBot(config)
    try:
        with InstanceLock(data_dir() / "bot.lock"):
            bot.run(token, log_handler=None)
    except RuntimeError as error:
        print(str(error))
        return 1
    except discord.LoginFailure:
        print("Токен Discord не принят. Обнови Bot Token в настройках Bothost; локально — через 02_SETTINGS.bat.")
        return 1
    except discord.Forbidden:
        print("Discord отказал в доступе. Проверь ID сервера и пригласи бота с scopes bot + applications.commands. Затем проверь логи Bothost или запусти python check.py.")
        return 1
    except (discord.HTTPException, OSError) as error:
        print(f"Соединение не установлено ({type(error).__name__}). Проверь локацию хостинга и доступ к Discord. Диагностика: python check.py.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
