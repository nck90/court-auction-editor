#!/usr/bin/env python3
"""Cell-level diff between pipeline output and human PDF for 의정부3계."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path("/Users/bagjun-won/t")
sys.path.insert(0, str(ROOT / "app"))

from render_final_notice import format_entry, load_entries  # noqa: E402


# Expected cells extracted by hand from the human PDF layout text.
# Each row: (case_num, item, expected_locations_list, expected_usages_list, expected_note)
EXPECTED = {
    ("2024타경3865", "1"): {
        "locations": [
            "양주시 남면 한산리 578 908㎡",
            "양주시 남면 운하로99번길 345-24 [1동] 단층 농기계수리점195㎡",
            "동소 345-24 [2동] 단층 농기계수리점190.19㎡",
            "동소 345-24 [3동] 단층 농기계수리점59.58㎡ 제시외 다용도실등31㎡ 소관정1식",
            "양주시 남면 한산리 578-13 30㎡",
            "양주시 남면 신산리 320 1315㎡",
        ],
        "usages": ["잡종지", "기타", "기타", "기타", "도로", "전"],
        "note": "일괄매각.제시외건물포함.농지취득자격증명요",
    },
    ("2024타경4073", "1"): {
        "locations": [
            "의정부시 배꽃길105,10층1014호[민락동,의정부더리브센텀스퀘어1 지식산업센터1동] 57.27㎡",
        ],
        "usages": ["근린시설"],
        "note": "",
    },
    ("2024타경4073", "2"): {
        "locations": [
            "의정부시 배꽃길7,8층818호[민락동,의정부더리브센텀스퀘어2 지식산업센터2동] 57.27㎡",
        ],
        "usages": ["근린시설"],
        "note": "",
    },
    ("2024타경4448", "1"): {
        "locations": ["포천시 신읍동 369-64 1179㎡"],
        "usages": ["임야"],
        "note": "",
    },
    ("2024타경5113", "1"): {
        "locations": ["양주시 고암길275,308동 6층603호[고암동,동안마을] 83.44㎡"],
        "usages": ["아파트"],
        "note": "",
    },
    ("2024타경5373", "1"): {
        "locations": ["양주시 화합로1710번길12,지2층비210호[옥정동,양주옥정듀클래스1] 공장 44.88㎡"],
        "usages": ["상가,오피스텔등"],
        "note": "",
    },
    ("2024타경5472", "1"): {
        "locations": ["의정부시 송양로46,707동 13층1304호[낙양동,의정부민락푸르지오] 84.937㎡"],
        "usages": ["아파트"],
        "note": "",
    },
    ("2024타경5601", "1"): {
        "locations": ["의정부시 용민로55,16층1601호[용현동,애디안주상복합] 69.04㎡"],
        "usages": ["아파트"],
        "note": "",
    },
    ("2024타경5700", "1"): {
        "locations": ["양주시 덕계로138-32[덕계동],1층103호[덕계동,센트럴휴티스근린생활시설] 29.2342㎡"],
        "usages": ["근린시설"],
        "note": "",
    },
    ("2024타경70100", "1"): {
        "locations": [
            "포천시 창수면 추동리 255 4958㎡ 제시외 작업장등1,147.4㎡ 수변전설비 255kw㎡",
            "동소 256-2 611㎡ 제시외 보일러실12㎡",
            "포천시 창수면 포천로 2575 1층116.2㎡ 2층126.73㎡ 각사무소",
            "동소 2575 에이동호 1층827.2㎡ 2층163.2㎡",
            "동소 2575 비동호 단층325.8㎡",
            "동소 2575 씨동호 단층330㎡",
        ],
        "usages": ["공장용지", "대", "근린시설", "공장", "공장", "공장"],
        "note": "일괄매각.제시외건물포함[기호ㄴ제외]",
    },
    ("2024타경80909", "1"): {
        "locations": [
            "철원군 서면 와수리 1206-7 603㎡",
            "동소 1206-43 48㎡",
            "동소 1206-45 221㎡",
            "철원군 서면 와수로181번길 15 지하1층유흥주점214.2㎡ 계단실18㎡ 기계실92.7㎡ 1층주차장36㎡ 휴게음식점361.92㎡ 2∼4층여관각463.81㎡ 5층일반음식점370.21㎡ 6층세탁실23.15㎡ 제시외 계단실등254.4㎡",
        ],
        "usages": ["대", "대", "대", "위락시설,\n근린시설"],
        "note": "일괄매각.제시외건물포함",
    },
    ("2024타경82134", "1"): {
        "locations": ["의정부시 시민로158번길53-7,1동 3층 301호[의정부동,한자연다세대주택주건축물] 29.952㎡"],
        "usages": ["다세대"],
        "note": "",
    },
    ("2024타경82974", "1"): {
        "locations": ["포천시 호병골길41-5,101동 13층 1310호[신읍동,일신아파트] 39.95㎡"],
        "usages": ["아파트"],
        "note": "",
    },
    ("2024타경83311", "1"): {
        "locations": ["포천시 소흘읍 고모리 842 1475㎡[갑구2번강용자1/24지분전부.농지취득자격증명요,제시외 물건매각제외]"],
        "usages": ["답"],
        "note": "지분매각.공유자우선매수신고1회제한",
    },
    ("2024타경83830", "1"): {
        "locations": ["양주시 백석읍 호명로77,201동 1층 105호[가야아파트] 59.97㎡"],
        "usages": ["아파트"],
        "note": "",
    },
    ("2024타경83977", "1"): {
        "locations": ["양주시 고읍남로20-15,5층 506호[광사동,스카이캐슬] 26.675㎡"],
        "usages": ["오피스텔"],
        "note": "",
    },
    ("2024타경84130", "1"): {
        "locations": ["의정부시 시민로245번길10,104동 20층 2003호[신곡동,신곡신일1차아파트] 84.955㎡"],
        "usages": ["아파트"],
        "note": "",
    },
    ("2024타경84369", "1"): {
        "locations": ["동두천시 평화로2316-6,201동 17층 1703호[지행동,지행현대아파트] 70.62㎡"],
        "usages": ["아파트"],
        "note": "",
    },
    ("2024타경85546", "1"): {
        "locations": ["의정부시 평화로272번길23,105동 4층 402호[호원동,일진해피빌] 39.56㎡"],
        "usages": ["다세대"],
        "note": "",
    },
    ("2024타경85690", "1"): {
        "locations": ["양주시 평화로1476-24,202동 8층 803호[덕계동,범양아파트 2단지] 84.7524㎡"],
        "usages": ["아파트"],
        "note": "",
    },
}


def main() -> int:
    doc = load_entries(
        Path(
            "output/batch_test/0320_의정부지방법원_경매3계-완료/CS_20250403_20250318110713.normalized.json"
        )
    )
    total = 0
    mismatches = 0
    for entry in doc.get("entries", []):
        if entry.get("usage") in {"자동차", "선박", "건설기계", "항공기"}:
            continue
        case_nums = entry.get("case_numbers") or []
        item = entry.get("item_number") or ""
        if not case_nums:
            continue
        key = (case_nums[0], item)
        expected = EXPECTED.get(key)
        if not expected:
            continue
        total += 1
        f = format_entry(entry)
        # Compare.
        me_loc = f.get("locations") or []
        me_usg = f.get("usages") or []
        me_note = f.get("note") or ""
        diffs: list[str] = []
        if len(me_loc) != len(expected["locations"]):
            diffs.append(
                f"N rows: me={len(me_loc)} expected={len(expected['locations'])}"
            )
        for i, (m, e) in enumerate(zip(me_loc, expected["locations"])):
            if m != e:
                diffs.append(f"  loc[{i}]:\n    me: {m!r}\n    ex: {e!r}")
        for i, (m, e) in enumerate(zip(me_usg, expected["usages"])):
            if m != e:
                diffs.append(f"  usg[{i}]:\n    me: {m!r}\n    ex: {e!r}")
        if me_note != expected["note"]:
            diffs.append(f"  note:\n    me: {me_note!r}\n    ex: {expected['note']!r}")
        if diffs:
            mismatches += 1
            print(f"== {key} ==")
            for d in diffs:
                print(d)
            print()
    print(f"\n Total compared: {total}, mismatches: {mismatches}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
