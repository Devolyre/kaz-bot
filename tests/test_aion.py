"""Offline regression tests for identity, partial data, and HTTP failures.

Fixtures are synthetic; their field shapes were checked against real Global
responses on 2026-10-08. Passing these tests is not a claim of live access.
"""

import asyncio
from copy import deepcopy
import unittest
from urllib.parse import urlsplit

from aiohttp import web
from aiohttp.test_utils import TestServer

from kazeros.aion import AionClient, AionError, normalize_region, profile_url


REF = {"region": "eu", "server_id": 1312, "character_id": "test-character="}


def source_data():
    return {
        "servers": {"serverList": [
            {"serverId": 1312, "serverName": "Luteros", "raceId": 1},
            {"serverId": 1301, "serverName": "Siel", "raceId": 1},
        ]},
        "classes": {"classList": [{"id": 3, "name": "Templar", "text": "Templar"}]},
        "pcdata": {"pcDataList": [{"id": 9, "className": "TEMPLAR", "classText": "Templar"}]},
        "search": {
            "list": [{"characterId": "test-character%3D", "name": "<strong>Tester</strong>", "serverId": 1312,
                      "serverName": "Luteros", "region": "eu", "pcId": 9, "level": 45, "race": 1}],
            "pagination": {"page": 1, "size": 200, "total": 1, "endPage": 1},
        },
        "info": {
            "profile": {"characterId": "test-character=", "serverId": 1312, "serverName": "Luteros",
                        "characterName": "Tester", "characterLevel": 45, "className": "Templar",
                        "pcId": 9, "combatPower": 68078, "raceId": 1, "raceName": "Elyos"},
            "stat": {"statList": [{"type": "ItemLevel", "name": "아이템레벨", "value": 1400},
                                  {"type": "DEX", "name": "Dexterity", "value": 0}]},
            "title": {"titleList": [{"id": 1, "name": "Test title", "equipCategory": "Attack",
                                      "statList": [], "equipStatList": [{"desc": "PvE Damage Boost +3%"}]}]},
            "daevanion": {"boardList": [{"id": 21, "name": "Nezekan", "open": 1,
                                          "openNodeCount": 0, "totalNodeCount": 88}]},
        },
        "equipment": {
            "equipment": {"equipmentList": [{"id": 123, "slotPos": 1, "slotPosName": "MainHand",
                                             "name": "Test sword", "grade": "Unique", "enchantLevel": 0, "exceedLevel": 0}],
                          "skinList": []},
            "skill": {"skillList": [{"id": 1, "name": "Test skill", "category": "Active", "skillLevel": 1,
                                    "acquired": 1, "equip": 1, "needLevel": 1}]},
            "petwing": {"pet": None, "wing": None},
        },
        "item": {"id": 123, "name": "Test sword", "grade": "Unique", "categoryName": "Longsword",
                 "level": 45, "enchantLevel": 0, "maxEnchantLevel": 15, "magicStoneSlotCount": 4,
                 "magicStoneStat": [], "godStoneSlotCount": 1, "mainStats": [], "subStats": []},
        "board": {"nodeList": [{"nodeId": 210001, "name": "", "type": "None", "open": 0,
                                 "row": 1, "col": 1, "effectList": []},
                                {"nodeId": 210034, "name": "Attack", "type": "Stat", "open": 0,
                                 "effectList": [{"desc": "Attack Bonus +3"}]}],
                  "openSkillEffectList": [], "openStatEffectList": []},
    }


class StubClient(AionClient):
    def __init__(self):
        super().__init__()
        self.data = source_data()
        self.calls = []

    async def _request(self, url, params, *, cache_seconds=0):
        self.calls.append((url, deepcopy(params)))
        await asyncio.sleep(0)  # Exercise concurrent snapshot request coalescing.
        path = urlsplit(url).path
        name = path.rsplit("/", 1)[1]
        name = {"character": "search", "detail": "board"}.get(name, name)
        result = self.data[name]
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = StubClient()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_real_schema_keeps_unknown_distinct_from_known_zero(self):
        snapshot = await self.client.get_snapshot(REF)
        self.assertEqual(snapshot["profile"]["item_level"], 1400)
        self.assertEqual(snapshot["profile"]["combat_power"], 68078)
        self.assertEqual(snapshot["profile"]["class_id"], 3)
        item = snapshot["equipment"][0]
        self.assertEqual(item["enchant_level"], 0)
        self.assertEqual(item["max_enchant_level"], 15)
        self.assertEqual(item["magic_stones"], [])
        self.assertIsNone(item["god_stones"])
        self.assertEqual(item["god_stone_slots"], 1)
        self.assertIsNone(item["max_exceed_level"])
        self.assertEqual(snapshot["daevanion"][0]["open_nodes"], 0)
        self.assertEqual(snapshot["daevanion"][0]["nodes"][0]["type"], "None")
        self.assertTrue(snapshot["skills"][0]["equipped"])
        self.assertIsNone(snapshot["skills"][0]["max_level"])
        self.assertIsNone(snapshot["titles"][0]["equipped"])
        self.assertTrue(snapshot["completeness"]["item_details"])
        self.assertIn("+00:00", snapshot["fetched_at"])

    async def test_no_gear_response_is_not_an_empty_inventory(self):
        self.client.data["equipment"]["equipment"]["equipmentList"] = None
        with self.assertRaises(AionError) as caught:
            await self.client.get_snapshot(REF)
        self.assertEqual(caught.exception.code, "incomplete")

    async def test_missing_optional_sections_are_unknown_and_marked(self):
        self.client.data["equipment"].pop("skill")
        for key in ("stat", "daevanion", "title"):
            self.client.data["info"].pop(key)
        snapshot = await self.client.get_snapshot(REF)
        for key in ("stats", "skills", "daevanion", "titles"):
            self.assertIsNone(snapshot[key])
            self.assertFalse(snapshot["completeness"][key])
        self.assertIsNone(snapshot["profile"]["item_level"])
        self.assertEqual(snapshot["profile"]["combat_power"], 68078)

    async def test_partial_item_failure_does_not_create_zero_slots_or_caps(self):
        self.client.data["item"] = AionError("Источник временно недоступен.")
        snapshot = await self.client.get_snapshot(REF)
        item = snapshot["equipment"][0]
        self.assertEqual(item["id"], 123)
        self.assertEqual(item["enchant_level"], 0)
        self.assertIsNone(item["max_enchant_level"])
        self.assertIsNone(item["magic_stones"])
        self.assertIsNone(item["magic_stone_slots"])
        self.assertFalse(item["detail_loaded"])
        self.assertFalse(snapshot["completeness"]["item_details"])
        self.assertTrue(snapshot["warnings"])

    async def test_changed_item_is_not_combined_with_old_equipment(self):
        self.client.data["item"]["id"] = 999
        snapshot = await self.client.get_snapshot(REF)
        self.assertEqual(snapshot["equipment"][0]["id"], 123)
        self.assertFalse(snapshot["equipment"][0]["detail_loaded"])

    async def test_missing_enchant_does_not_request_a_fabricated_plus_zero_item(self):
        self.client.data["equipment"]["equipment"]["equipmentList"][0].pop("enchantLevel")
        snapshot = await self.client.get_snapshot(REF)
        self.assertIsNone(snapshot["equipment"][0]["enchant_level"])
        self.assertFalse(any(url.endswith("/item") for url, _ in self.client.calls))

    async def test_identity_mismatch_cancels_analysis(self):
        self.client.data["info"]["profile"]["characterId"] = "someone-else="
        with self.assertRaises(AionError):
            await self.client.get_snapshot(REF)
        self.assertFalse(any(url.endswith("/item") for url, _ in self.client.calls))

    async def test_url_encoding_is_done_exactly_once(self):
        encoded_ref = {**REF, "character_id": "test-character%3D"}
        await self.client.get_snapshot(encoded_ref)
        queries = [params for _, params in self.client.calls if "characterId" in params]
        self.assertTrue(queries)
        self.assertTrue(all(q["characterId"] == "test-character=" for q in queries))
        url = profile_url(encoded_ref)
        self.assertIn("test-character%3D?region=eu", url)
        self.assertNotIn("%253D", url)

    async def test_search_class_mapping_uses_class_table_not_pc_id(self):
        hits = await self.client.search_characters("Tester", "eu", 1312)
        self.assertEqual(hits[0]["name"], "Tester")
        self.assertEqual(hits[0]["class_name"], "Templar")
        self.assertEqual(hits[0]["class_id"], 3)
        self.assertEqual(hits[0]["character_id"], "test-character=")

    async def test_search_does_not_silently_choose_a_same_named_character(self):
        self.client.data["search"]["list"].append({
            **self.client.data["search"]["list"][0], "characterId": "second=", "serverId": 1301, "serverName": "Siel",
        })
        self.client.data["search"]["pagination"]["total"] = 2
        hits = await self.client.search_characters("Tester", "eu")
        self.assertEqual(len(hits), 2)
        self.assertEqual({h["server_id"] for h in hits}, {1301, 1312})

    async def test_cross_region_search_reply_is_rejected(self):
        self.client.data["search"]["list"][0]["region"] = "naw"
        with self.assertRaises(AionError):
            await self.client.search_characters("Tester", "eu")

    async def test_overflow_is_reported_instead_of_hiding_candidates(self):
        self.client.data["search"]["pagination"].update(total=201, endPage=2)
        with self.assertRaises(AionError) as caught:
            await self.client.search_characters("T", "eu")
        self.assertEqual(caught.exception.code, "too_many_results")

    async def test_cache_preserves_timestamp_isolation_and_force_refresh(self):
        first = await self.client.get_snapshot(REF)
        initial_calls = len(self.client.calls)
        first["profile"]["name"] = "modified by caller"
        second = await self.client.get_snapshot(REF)
        self.assertEqual(second["profile"]["name"], "Tester")
        self.assertEqual(first["fetched_at"], second["fetched_at"])
        self.assertEqual(len(self.client.calls), initial_calls)
        await self.client.get_snapshot(REF, force=True)
        self.assertGreater(len(self.client.calls), initial_calls)

    async def test_concurrent_requests_share_one_source_read(self):
        one, two = await asyncio.gather(self.client.get_snapshot(REF), self.client.get_snapshot(REF))
        self.assertEqual(sum(url.endswith("/info") for url, _ in self.client.calls), 1)
        one["profile"]["name"] = "changed"
        self.assertEqual(two["profile"]["name"], "Tester")

    async def test_missing_board_detail_stays_unknown(self):
        self.client.data["board"] = {"nodeList": None}
        snapshot = await self.client.get_snapshot(REF)
        self.assertEqual(snapshot["daevanion"][0]["total_nodes"], 88)
        self.assertIsNone(snapshot["daevanion"][0]["nodes"])
        self.assertFalse(snapshot["completeness"]["daevanion"])

    def test_only_global_regions_and_valid_refs_are_accepted(self):
        self.assertEqual(normalize_region(" EU "), "eu")
        self.assertEqual(normalize_region("sa"), "la")
        with self.assertRaises(AionError):
            normalize_region("kr")
        with self.assertRaises(AionError):
            profile_url({**REF, "server_id": 0})


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AionClient()
        self.client._interval = 0
        self.servers = []

    async def asyncTearDown(self):
        await self.client.close()
        for server in self.servers:
            await server.close()

    async def serve(self, handler):
        app = web.Application()
        app.router.add_get("/test", handler)
        server = TestServer(app)
        await server.start_server()
        self.servers.append(server)
        return str(server.make_url("/test"))

    async def test_rate_limit_blocks_following_calls_without_retrying(self):
        calls = []
        async def handler(request):
            calls.append(request)
            return web.Response(status=429, headers={"Retry-After": "30"})
        url = await self.serve(handler)
        for _ in range(2):
            with self.assertRaises(AionError) as caught:
                await self.client._request(url, {})
            self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(len(calls), 1)

    async def test_html_success_is_rejected_as_non_data(self):
        async def handler(_):
            return web.Response(text="<html>Sign in</html>")
        url = await self.serve(handler)
        with self.assertRaises(AionError) as caught:
            await self.client._request(url, {})
        self.assertEqual(caught.exception.code, "schema_changed")

    async def test_access_denied_is_not_retried(self):
        calls = []
        async def handler(request):
            calls.append(request)
            return web.Response(status=403)
        url = await self.serve(handler)
        with self.assertRaises(AionError) as caught:
            await self.client._request(url, {})
        self.assertEqual(caught.exception.code, "access_denied")
        self.assertEqual(len(calls), 1)

    async def test_transient_server_failure_recovers_once(self):
        calls = []
        async def handler(request):
            calls.append(request)
            return web.Response(status=503) if len(calls) == 1 else web.json_response({"ok": True})
        url = await self.serve(handler)
        self.assertEqual(await self.client._request(url, {}), {"ok": True})
        self.assertEqual(len(calls), 2)

    async def test_static_json_cache_does_not_leak_mutations(self):
        calls = []
        async def handler(request):
            calls.append(request)
            return web.json_response({"items": [1]})
        url = await self.serve(handler)
        body = await self.client._request(url, {}, cache_seconds=60)
        body["items"].append(2)
        self.assertEqual(await self.client._request(url, {}, cache_seconds=60), {"items": [1]})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
