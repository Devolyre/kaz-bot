"""User-flow, isolation and Discord rendering regression checks."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from bot import CoachBot, OwnedView, PanelView, ReportView, UserError
from kazeros.config import DEFAULTS, ConfigError, load_token, validate_config, validate_token
from kazeros.instance import InstanceLock
from kazeros.store import Store, changes
from kazeros.views import parse_character_url, profile_embed, report_embed


REF = {"region": "eu", "server_id": 1312, "character_id": "abcdefghijklmnop=", "name": "Test", "server_name": "Luteros", "class_name": "Templar", "class_id": 3, "level": 45}


def snapshot():
    return {
        "schema_version": 1, "source": "ncsoft",
        "profile": {**REF, "item_level": 1000, "combat_power": 12345},
        "profile_url": "https://aion2.plaync.com/en-us/characters/1312/abcdefghijklmnop%3D?region=eu",
        "fetched_at": "2026-10-08T04:00:00+00:00",
        "equipment": [], "skills": [], "daevanion": [], "titles": [], "stats": [],
        "warnings": [], "completeness": {},
    }


class Response:
    def __init__(self):
        self.done = False
        self.calls = []

    def is_done(self):
        return self.done

    async def defer(self, **kwargs):
        self.done = True
        self.calls.append(("defer", kwargs))

    async def send_message(self, content=None, **kwargs):
        self.done = True
        self.calls.append(("message", {"content": content, **kwargs}))

    async def send_modal(self, modal):
        self.done = True
        self.calls.append(("modal", modal))

    async def edit_message(self, **kwargs):
        self.done = True
        self.calls.append(("edit", kwargs))


class Followup:
    def __init__(self):
        self.calls = []

    async def send(self, content=None, **kwargs):
        self.calls.append({"content": content, **kwargs})


def interaction(user=10, guild=100):
    return SimpleNamespace(user=SimpleNamespace(id=user), guild_id=guild, response=Response(), followup=Followup(), permissions=SimpleNamespace(manage_guild=True))


class FakeAPI:
    def __init__(self):
        self.refs = [dict(REF)]
        self.snapshot = snapshot()
        self.calls = []

    async def close(self):
        pass

    async def list_servers(self, region):
        return [{"id": 1312, "name": "Luteros"}]

    async def search_characters(self, nickname, region, server_id=None):
        self.calls.append((nickname, region, server_id))
        return deepcopy(self.refs)

    async def get_snapshot(self, ref, *, force=False):
        self.calls.append(("snapshot", force))
        return deepcopy(self.snapshot)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "store.db")
        self.prefs = {"goal": "pve", "role": "tank", "budget": "low"}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_members_and_guilds_are_isolated_and_delete_is_scoped(self):
        for gid, uid in [(100, 10), (100, 11), (200, 10)]:
            self.store.bind(gid, uid, REF, self.prefs, snapshot())
        self.store.forget(100, 10)
        self.assertIsNone(self.store.get(100, 10))
        self.assertIsNotNone(self.store.get(100, 11))
        self.assertIsNotNone(self.store.get(200, 10))

    def test_cached_repeat_does_not_erase_history_and_wrong_identity_is_rejected(self):
        before = snapshot()
        self.store.bind(100, 10, REF, self.prefs, before)
        after = deepcopy(before)
        after["fetched_at"] = "2026-10-08T05:00:00+00:00"
        after["profile"]["item_level"] = 1100
        self.store.save_snapshot(100, 10, after)
        self.store.save_snapshot(100, 10, after)
        self.assertEqual(self.store.get(100, 10)["previous_snapshot"]["profile"]["item_level"], 1000)
        wrong = deepcopy(after)
        wrong["profile"]["character_id"] = "someoneelse="
        with self.assertRaises(ValueError):
            self.store.save_snapshot(100, 10, wrong)

    def test_rebinding_different_character_clears_the_old_history(self):
        self.store.bind(100, 10, REF, self.prefs, snapshot())
        other = snapshot()
        other["profile"]["character_id"] = "othercharacter="
        self.store.bind(100, 10, other["profile"], self.prefs, other)
        self.assertIsNone(self.store.get(100, 10)["previous_snapshot"])

    def test_missing_stat_is_not_a_negative_progress_delta(self):
        before = snapshot()
        after = snapshot()
        after["profile"]["item_level"] = None
        self.assertEqual(changes(before, after), [])


class InputTests(unittest.TestCase):
    def test_official_encoded_url_is_parsed_once_and_external_urls_are_rejected(self):
        result = parse_character_url(snapshot()["profile_url"])
        self.assertEqual(result["character_id"], REF["character_id"])
        for value in ["http://aion2.plaync.com/en-us/characters/1312/abcdefgh?region=eu", "https://aion2.plaync.com.evil.example/en-us/characters/1312/abcdefgh?region=eu", "https://localhost/", "https://aion2.plaync.com/en-us/characters/1312/abcdefgh?region=kr", "https://user:secret@aion2.plaync.com/en-us/characters/1312/abcdefgh?region=eu"]:
            with self.assertRaises(ConfigError):
                parse_character_url(value)

    def test_new_install_requires_real_discord_id_and_local_token(self):
        with self.assertRaises(ConfigError):
            validate_config(DEFAULTS)
        with TemporaryDirectory() as temp:
            from unittest.mock import patch
            with patch.dict("os.environ", {}, clear=True), self.assertRaises(ConfigError):
                load_token(Path(temp))
        for token in ["", "test", "x" * 40 + "\nDISCORD_TOKEN=second"]:
            with self.assertRaises(ConfigError):
                validate_token(token)

    def test_second_process_lock_fails_then_recovers_after_exit(self):
        with TemporaryDirectory() as temp:
            lock = Path(temp) / "bot.lock"
            with InstanceLock(lock):
                with self.assertRaises(RuntimeError):
                    with InstanceLock(lock):
                        pass
            with InstanceLock(lock):
                pass

    def test_large_and_untrusted_source_values_fit_discord_limits(self):
        s = snapshot()
        s["profile"]["name"] = "@everyone **url** " * 30
        s["equipment"] = [{"slot": i, "name": "@everyone " + "x" * 1000, "enchant_level": 5} for i in range(30)]
        report = {"summary": "x" * 15000, "actions": [{"title": "x" * 1000, "observed": "@everyone " * 2000, "action": "x" * 10000, "reason": "x" * 10000, "cost": "x" * 10000} for _ in range(15)], "limitations": ["x" * 10000] * 3}
        for embed in (profile_embed(s), report_embed(s, report, {"goal": "pve", "role": "tank", "budget": "low"})):
            self.assertLessEqual(len(embed), 6000)
            self.assertLessEqual(len(embed.title), 256)
            for field in embed.fields:
                self.assertLessEqual(len(field.value), 1024)
            self.assertNotIn("@everyone", json.dumps(embed.to_dict()))


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.api = FakeAPI()
        self.bot = CoachBot({**DEFAULTS, "discord_guild_id": 100}, store_path=Path(self.temp.name) / "db", api=self.api)

    async def asyncTearDown(self):
        await self.bot.close()
        self.temp.cleanup()

    async def test_command_contract_persistent_panel_and_minimal_intents(self):
        names = {cmd.name for cmd in self.bot.tree.get_commands()}
        self.assertEqual(names, {"панель", "персонаж", "разбор", "сравнить", "помощь", "отвязать"})
        self.assertTrue(PanelView(self.bot).is_persistent())
        self.assertFalse(self.bot.intents.message_content)
        self.assertFalse(self.bot.intents.members)

    async def test_search_does_not_bind_before_selection_and_is_private(self):
        i = interaction()
        await self.bot.search(i, "Test", "eu", "Luteros")
        self.assertIsNone(self.bot.record(i))
        self.assertEqual(self.api.calls[0], ("Test", "eu", 1312))
        self.assertTrue(i.followup.calls[-1]["ephemeral"])
        self.assertEqual(len(i.followup.calls[-1]["view"].refs), 1)

    async def test_candidate_overflow_does_not_hide_a_same_named_character(self):
        self.api.refs = [{**REF, "server_id": i} for i in range(1, 30)]
        with self.assertRaisesRegex(UserError, "больше 25"):
            await self.bot.lookup("Test", "eu", "")

    async def test_bind_analyze_preferences_history_flow(self):
        await self.bot.bind(interaction(), REF)
        row = self.bot.store.get(100, 10)
        self.assertEqual(row["ref"]["character_id"], REF["character_id"])
        i = interaction()
        await self.bot.analyze_bound(i, preferences={"role": "tank"})
        self.assertTrue(i.followup.calls[-1]["ephemeral"])
        self.assertIsInstance(i.followup.calls[-1]["view"], ReportView)
        self.assertEqual(self.bot.store.get(100, 10)["preferences"]["role"], "tank")

    async def test_unbound_analysis_opens_a_form_and_private_views_check_owner(self):
        i = interaction()
        await self.bot.analyze_bound(i)
        self.assertEqual(i.response.calls[0][0], "modal")
        view = OwnedView(self.bot, 10)
        other = interaction(user=11)
        self.assertFalse(await view.interaction_check(other))
        self.assertTrue(other.response.calls[-1][1]["ephemeral"])

    async def test_two_simultaneous_requests_from_same_member_do_not_mutate_state(self):
        i = interaction()
        async with self.bot.user_job(i):
            with self.assertRaises(UserError):
                async with self.bot.user_job(i):
                    pass
        self.assertEqual(self.bot._busy, set())


if __name__ == "__main__":
    unittest.main()
