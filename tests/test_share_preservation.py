import unittest

from qwen_pipeline import (
    _choose_note_text,
    _enforce_location_invariants,
    _prepare_locations_for_llm,
)
from rules import compact_keun_rin, convert_share_notation, strip_building_materials


class SharePreservationTests(unittest.TestCase):
    def test_missing_share_is_restored_from_source_location(self):
        source_locs = [
            {
                "address_raw": "대구광역시 달성군 현풍읍 하리 27",
                "use_raw": "대 750㎡ (전 소유권 중 갑구 7번 윤석훈 80분의 5 지분전부)",
            }
        ]
        out_locs = [{"address": "동소 27 750㎡", "use": "대"}]

        fixed = _enforce_location_invariants(source_locs, out_locs)

        self.assertEqual(len(fixed), 1)
        self.assertIn("동소 27 750㎡", fixed[0]["address"])
        self.assertIn("갑구7번윤석훈5/80지분전부", fixed[0]["address"])

    def test_split_lots_keep_share_and_same_address_collapses_to_dongso(self):
        share = (
            "(전 소유권 중 갑구 4번 이강규 1745분의 785.25 지분전부 "
            "갑구 6번 김정숙 1745분의 785.25 지분전부)"
        )
        source_locs = [
            {
                "address_raw": "대구광역시 달성군 옥포읍 본리리 417",
                "use_raw": f"답 1651㎡ {share}",
            },
            {
                "address_raw": "대구광역시 달성군 옥포읍 본리리 417-1",
                "use_raw": f"답 124㎡ {share}",
            },
            {
                "address_raw": "대구광역시 달성군 옥포읍 본리리 417-2",
                "use_raw": f"답 48㎡ {share}",
            },
        ]
        out_locs = [
            {"address": "달성군 옥포읍 본리리 417 1651㎡", "use": "답"},
            {"address": "달성군 옥포읍 본리리 417-1 124㎡", "use": "답"},
            {"address": "달성군 옥포읍 본리리 417-2 48㎡", "use": "답"},
        ]

        fixed = _enforce_location_invariants(source_locs, out_locs)

        self.assertEqual(len(fixed), 3)
        self.assertTrue(fixed[1]["address"].startswith("동소 본리리 417-1"))
        self.assertTrue(fixed[2]["address"].startswith("동소 본리리 417-2"))
        for loc in fixed:
            self.assertIn("갑구4번이강규785.25/1745지분전부", loc["address"])
            self.assertIn("갑구6번김정숙785.25/1745지분전부", loc["address"])

    def test_share_text_is_removed_before_llm_and_reassembled_afterward(self):
        source_locs = [
            {
                "address_raw": "대구광역시 달성군 현풍읍 하리 27",
                "use_raw": "대 750㎡ (전 소유권 중 갑구 7번 윤석훈 80분의 5 지분전부)",
            }
        ]

        prepared = _prepare_locations_for_llm(source_locs)

        self.assertNotIn("지분", prepared[0]["use_raw"])
        self.assertNotIn("갑구", prepared[0]["use_raw"])
        self.assertEqual(prepared[0]["use_raw"], "대 750㎡")

    def test_collapsed_share_rows_fail_closed_to_source_row_count(self):
        share = "(전 소유권 중 갑구 7번 윤석훈 80분의 5 지분전부)"
        source_locs = [
            {
                "address_raw": "대구광역시 달성군 현풍읍 하리 23",
                "use_raw": "대 81㎡",
            },
            {
                "address_raw": "대구광역시 달성군 현풍읍 하리 27",
                "use_raw": f"대 750㎡ {share}",
            },
            {
                "address_raw": "대구광역시 달성군 현풍읍 하리 28",
                "use_raw": f"대 200㎡ {share}",
            },
        ]
        out_locs = [
            {"address": "달성군 현풍읍 하리 23 81㎡ 동소 27 750㎡", "use": "대"},
        ]

        fixed = _enforce_location_invariants(source_locs, out_locs)

        self.assertEqual(len(fixed), 3)
        self.assertIn("갑구7번윤석훈5/80지분전부", fixed[1]["address"])
        self.assertIn("갑구7번윤석훈5/80지분전부", fixed[2]["address"])

    def test_non_share_rows_also_preserve_source_row_count(self):
        source_locs = [
            {
                "address_raw": "대구광역시 달성군 현풍읍 하리 23",
                "use_raw": "대 81㎡",
            },
            {
                "address_raw": "대구광역시 달성군 현풍읍 하리 22",
                "use_raw": "대 157㎡",
            },
            {
                "address_raw": "대구광역시 달성군 현풍읍 하리 25-1",
                "use_raw": "대 99㎡",
            },
        ]
        out_locs = [
            {"address": "달성군 현풍읍 하리 23 81㎡ 동소 22 157㎡ 동소 25-1 99㎡", "use": "대"},
        ]

        fixed = _enforce_location_invariants(source_locs, out_locs)

        self.assertEqual(len(fixed), 3)
        self.assertEqual(fixed[0]["address"], "달성군 현풍읍 하리 23 대 81㎡")
        self.assertTrue(fixed[1]["address"].startswith("동소 하리 22"))
        self.assertTrue(fixed[2]["address"].startswith("동소 하리 25-1"))

    def test_share_conversion_keeps_gyouja_marker_for_gapgu_clause(self):
        converted = convert_share_notation("(갑구 53번 공유자 권금자 지분 24157분의 363 전부)")
        self.assertEqual(converted, "갑구53번공유자권금자363/24157지분전부")

    def test_building_material_and_geunrin_are_normalized(self):
        self.assertEqual(strip_building_materials("[철근]콘크리트 3층단독주택"), "3층단독주택")
        self.assertEqual(compact_keun_rin("1,2종근린생활시설"), "근린시설")
        self.assertEqual(compact_keun_rin("제2종근린생활시설"), "근린시설")

    def test_lossy_note_edit_falls_back_to_raw_note(self):
        raw_note = (
            "일괄매각.제시외건물포함.목록1은공부상근린공공시설및2종근린생활시설이나"
            "지1층은주차장,승강로등으로,1층~3층은창고,내부계단,작업장등으로이용중임."
            "주식회사파인트리코리아가2024.12.2.자유치권신고(공사대금230,000,000원)를하였으나그성립여부는불분명함"
        )
        edited_note = "일괄매각.제시외건물포함.목록1은공부상근린공공시설및2종근린생활시설이나그성립여부는불분명함"

        chosen = _choose_note_text(raw_note, edited_note)

        self.assertEqual(chosen, raw_note)
        self.assertIn("유치권신고", chosen)
        self.assertIn("230,000,000원", chosen)


if __name__ == "__main__":
    unittest.main()
