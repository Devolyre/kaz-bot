"""Regression tests for false recommendations that can waste player resources."""

from copy import deepcopy
import unittest

from kazeros.coach import analyze


def snapshot(**overrides):
    result = {
        "profile_url": "https://aion2.plaync.com/en-us/characters/index?character=demo",
        "fetched_at": "2026-10-08T04:00:00+00:00",
        "profile": {"name": "Demo", "region": "eu", "class_id": 3, "class_name": "Templar"},
        "equipment": [], "skills": [], "daevanion": [], "titles": [],
        "warnings": [], "completeness": {},
    }
    result.update(overrides)
    return result


def ids(report):
    return {action["id"] for action in report["actions"]}


class CoachTests(unittest.TestCase):
    def test_missing_fields_do_not_become_missing_gear_or_empty_stones(self):
        item = {"id": 10, "name": "Weapon", "slot": 1, "magic_stone_slots": 4,
                "god_stone_slots": 1, "detail_loaded": True, "enchant_level": 0}
        report = analyze(snapshot(equipment=[item]))
        self.assertFalse({"empty_magic_sockets", "empty_god_sockets", "enchant_review"} & ids(report))
        self.assertTrue(any("неизвестно заполнение" in text for text in report["limitations"]))

    def test_explicit_empty_list_is_a_real_gap_but_null_is_not(self):
        item = {"id": 10, "name": "Weapon", "slot": 1, "magic_stone_slots": 3,
                "magic_stones": [], "god_stone_slots": 1, "god_stones": None}
        report = analyze(snapshot(equipment=[item]), budget="free")
        self.assertIn("empty_magic_sockets", ids(report))
        self.assertNotIn("empty_god_sockets", ids(report))
        action = next(a for a in report["actions"] if a["id"] == "empty_magic_sockets")
        self.assertIn("занято 0 из 3", action["observed"])
        self.assertIn("платную установку отложи", action["action"])

    def test_malformed_socket_data_does_not_create_a_purchase_recommendation(self):
        for stones in ([{}], [None], [{"name": "A", "slotPos": 9}], [{"name": "A", "slotPos": 0}, {"name": "B"}], [{"name": "A"}] * 5):
            with self.subTest(stones=stones):
                item = {"id": 10, "name": "Weapon", "slot": 1,
                        "magic_stone_slots": 4, "magic_stones": stones}
                self.assertNotIn("empty_magic_sockets", ids(analyze(snapshot(equipment=[item]))))

    def test_known_item_cap_is_not_replaced_with_a_global_constant(self):
        equipment = [
            {"slot": 1, "id": 1, "name": "At cap", "enchant_level": 7, "max_enchant_level": 7},
            {"slot": 2, "id": 2, "name": "Unknown", "enchant_level": 0},
            {"slot": 3, "id": 3, "name": "Can check", "enchant_level": 4, "max_enchant_level": 6},
        ]
        report = analyze(snapshot(equipment=equipment), budget="free")
        action = next(a for a in report["actions"] if a["id"] == "enchant_review")
        self.assertIn("максимум в данных +6", action["observed"])
        self.assertIn("+4 → +5", action["action"])
        self.assertNotIn("Unknown", action["observed"])
        self.assertNotIn("At cap", action["observed"])
        self.assertIn("платное улучшение оставь", action["action"])

    def test_board_geometry_is_not_progress_and_unknown_open_is_not_closed(self):
        nodes = [
            {"nodeId": 1, "name": "", "type": "None", "open": 0, "effectList": []},
            {"nodeId": 2, "name": "Unknown state", "type": "Stat", "effectList": [{"desc": "Defense Bonus +30"}]},
            {"nodeId": 3, "name": "Empty geometry", "type": "None", "open": 0, "effectList": [{"desc": "Defense Bonus +30"}]},
        ]
        report = analyze(snapshot(daevanion=[{"id": 21, "name": "Nezekan", "open": True, "nodes": nodes}]), role="tank")
        self.assertNotIn("daevanion_role_stats", ids(report))

    def test_skill_node_must_match_exact_equipped_skill(self):
        nodes = [
            {"nodeId": 1, "name": "Skill A", "type": "SkillLevel", "open": 0,
             "row": 3, "col": 2, "effectList": [{"desc": "Shield Strike +1"}]},
        ]
        board = {"id": 21, "name": "Nezekan", "open": True, "nodes": nodes}
        for skill in ({"name": "Strike", "equipped": True}, {"name": "Shield Strike", "equipped": None}, {"name": "Shield Strike", "equipped": False}):
            self.assertNotIn("daevanion_equipped_skills", ids(analyze(snapshot(skills=[skill], daevanion=[board]))))
        report = analyze(snapshot(skills=[{"name": "Shield Strike", "equipped": True}], daevanion=[board]))
        action = next(a for a in report["actions"] if a["id"] == "daevanion_equipped_skills")
        self.assertIn("Shield Strike +1", action["observed"])
        self.assertIn("строка 3, столбец 2", action["observed"])

    def test_role_and_mode_filter_real_node_effects(self):
        nodes = [
            {"id": 1, "name": "Block", "type": "Stat", "open": False, "effects": [{"desc": "Block +10"}]},
            {"id": 2, "name": "PvP damage", "type": "Stat", "open": False, "effects": [{"desc": "PvP Damage Boost +3%"}]},
        ]
        data = snapshot(daevanion=[{"id": 21, "name": "Board", "open": True, "nodes": nodes}])
        tank = analyze(data, role="tank", goal="pve")
        action = next(a for a in tank["actions"] if a["id"] == "daevanion_role_stats")
        self.assertIn("Block +10", action["observed"])
        self.assertNotIn("PvP Damage", action["observed"])
        self.assertNotIn("daevanion_role_stats", ids(analyze(data, role="damage", goal="pve")))
        self.assertIn("daevanion_role_stats", ids(analyze(data, role="damage", goal="pvp")))

    def test_normalized_api_descriptions_drive_real_board_and_title_advice(self):
        # The live adapter keeps NC's effectList.desc as 'description', with
        # name/value absent. Do not lose this evidence after normalization.
        data = snapshot(
            skills=[{"id": 1, "name": "Pummel", "equipped": True, "level": 14}],
            daevanion=[{"id": 21, "name": "Nezekan", "open": True, "nodes": [
                {"id": 210073, "name": "Skill Level Up - Pummel", "type": "SkillLevel", "open": False,
                 "effects": [{"id": None, "name": None, "value": None, "description": "Pummel +1"}]},
            ]}],
            titles=[{"id": 5, "name": "Test title", "equipped": True,
                     "equip_stats": [{"name": None, "value": None, "description": "PvP Damage Boost +3%"}]}],
        )
        report = analyze(data)
        self.assertIn("daevanion_equipped_skills", ids(report))
        self.assertIn("title_goal_review", ids(report))

    def test_title_display_id_zero_does_not_mean_no_stat_titles(self):
        data = snapshot(titles=[{"id": 4, "name": "Title", "equipped": None,
                                 "equip_stats": [{"desc": "PvP Damage Boost +3%"}]}])
        data["profile"]["title_id"] = 0
        self.assertNotIn("title_goal_review", ids(analyze(data)))
        data["titles"][0]["equipped"] = True
        self.assertIn("title_goal_review", ids(analyze(data)))

    def test_reference_must_match_class_region_mode_and_role(self):
        own = snapshot(skills=[{"id": 1, "name": "Strike", "equipped": True, "level": 2}])
        ref = snapshot(skills=[{"id": 1, "name": "Strike", "equipped": True, "level": 8}],
                       coach_context={"goal": "pve", "role": "damage"})
        self.assertIn("reference_skills", ids(analyze(own, reference=ref)))
        for path, value in (("region", "kr"), ("region", "naw"), ("class_id", 2)):
            bad = deepcopy(ref)
            bad["profile"][path] = value
            self.assertNotIn("reference_skills", ids(analyze(own, reference=bad)))
        for context in ({}, {"goal": "pvp", "role": "damage"}, {"goal": "pve", "role": "tank"}):
            bad = deepcopy(ref)
            bad["coach_context"] = context
            self.assertNotIn("reference_skills", ids(analyze(own, reference=bad)))

    def test_reference_does_not_treat_higher_gs_as_an_upgrade_rule(self):
        own, ref = snapshot(), snapshot(coach_context={"goal": "pve", "role": "damage"})
        own["profile"]["item_level"] = 1000
        ref["profile"]["item_level"] = 3000
        report = analyze(own, reference=ref)
        self.assertFalse(report["actions"])
        self.assertTrue(any("не найдено подтверждённых различий" in text for text in report["limitations"]))

    def test_all_five_live_global_region_codes_accept_matching_references(self):
        for region in ("eu", "nae", "naw", "as", "la"):
            with self.subTest(region=region):
                own = snapshot(skills=[{"id": 1, "name": "Strike", "equipped": True, "level": 2}])
                ref = snapshot(skills=[{"id": 1, "name": "Strike", "equipped": True, "level": 8}],
                               coach_context={"goal": "pve", "role": "damage"})
                own["profile"]["region"] = ref["profile"]["region"] = region
                self.assertIn("reference_skills", ids(analyze(own, reference=ref)))

    def test_report_is_read_only_and_every_action_has_sources_and_date(self):
        data = snapshot(equipment=[{"slot": 1, "id": 2, "name": "Blade", "enchant_level": 1, "max_enchant_level": 3}])
        original = deepcopy(data)
        report = analyze(data)
        self.assertEqual(original, data)
        self.assertTrue(report["actions"])
        for action in report["actions"]:
            self.assertEqual(action["sources"], [data["profile_url"]])
            self.assertEqual(action["evidence_at"], data["fetched_at"])
            self.assertTrue(action["rules_checked_at"])

    def test_unsupported_non_global_region_gets_no_game_recommendations(self):
        data = snapshot(equipment=[{"slot": 1, "id": 2, "name": "Blade", "enchant_level": 1, "max_enchant_level": 3}])
        data["profile"]["region"] = "tw"
        self.assertFalse(analyze(data)["actions"])


if __name__ == "__main__":
    unittest.main()
