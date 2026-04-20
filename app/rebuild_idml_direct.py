#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from overlay_template_pdf import normalize_entry_for_overlay
from render_final_notice import GROUP_ORDER, format_entry


IDML_NS = "http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging"
ET.register_namespace("idPkg", IDML_NS)

LABEL_TEXT_BY_GROUP = {
    "기타": "[기타]",
    "아파트": "[아파트]",
    "대지/임야/전답": "[대지/임야/전답]",
    "상가/오피스텔,근린시설": "[상가/오피스텔,근린시설]",
    "연립주택/다세대/빌라": "[연립주택/다세대/빌라]",
    "단독주택,다가구주택": "[단독주택,다가구주택]",
}

SLOTS = [
    ("u3f71", "u47e6"),
    ("u3f2c", "u4638"),
    ("u3f88", "u4795"),
    ("u3f9f", "u47b5"),
    ("u3f43", "u4772"),
    ("u3f5a", "ue5"),
]
SLOT_FILL_ORDER = [
    "기타",
    "대지/임야/전답",
    "상가/오피스텔,근린시설",
    "아파트",
    "연립주택/다세대/빌라",
    "단독주택,다가구주택",
]

DONOR_BY_STORY = {
    "u47e6": "u47e6",
    "u47b5": "u47b5",
    "u4638": "u4638",
    "u4795": "u4795",
    "u4772": "u4772",
    "ue5": "u4772",
}

NEW_LABEL_BOUNDS = (357.73, 79.50, 692.22, 93.67)
NEW_BODY_BOUNDS = (356.92, 93.67, 691.90, 131.17)
HIDDEN_BOUNDS = (-1000.0, -1000.0, -995.0, -995.0)


def extract_meta(payload_mode: str, payload: object) -> dict[str, str]:
    if payload_mode == "slots":
        meta = payload.get("meta", {})
        return {
            "court_line": meta.get("court_line", ""),
            "auction_datetime": meta.get("auction_datetime", ""),
            "decision_datetime": meta.get("decision_datetime", ""),
            "officer_line": meta.get("officer_line", ""),
        }
    return {
        "court_line": "",
        "auction_datetime": "",
        "decision_datetime": "",
        "officer_line": "",
    }


def extract_court_and_division(court_line: str) -> tuple[str, str]:
    match = re.match(r"^(.*)\s+(경매\d+계)$", court_line.strip())
    if match:
        return match.group(1), re.sub(r"경매(\d+계)", r"경매 \1", match.group(2))
    return court_line.strip(), ""


def normalize_datetime_line(text: str, preferred_label: str) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    clean = re.sub(r"^[^:]+:\s*", "", clean)
    clean = re.sub(r"①.*$", "", clean).strip()
    return f"{preferred_label} : {clean}" if clean else ""


def normalize_datetime_value(text: str) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    clean = re.sub(r"^[^:]+:\s*", "", clean)
    if "①" in clean or "②" in clean:
        date_match = re.search(r"^\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.", clean)
        time_match = re.search(r"①\s*(\d{1,2}:\d{2})", clean) or re.search(r"(\d{1,2}:\d{2})", clean)
        if date_match and time_match:
            return f"{date_match.group(0).strip()} {time_match.group(1)}"
    return clean


def infer_sale_place(court_name: str) -> str:
    parts = court_name.split()
    if len(parts) >= 2:
        return f"{parts[-1]} 1층 입찰법정"
    return ""


def update_meta_story(story_xml: bytes, meta: dict[str, str]) -> bytes:
    root = ET.fromstring(story_xml)
    court_name, division = extract_court_and_division(meta.get("court_line", ""))
    auction_value = normalize_datetime_value(meta.get("auction_datetime", ""))
    decision_value = normalize_datetime_value(meta.get("decision_datetime", ""))
    sale_place = infer_sale_place(court_name)
    officer_name = meta.get("officer_line", "").replace("보좌관", "").replace(":", "").strip()
    officer_line = f"{court_name} 사법보좌관 {officer_name}".strip() if court_name and officer_name else ""

    replacements = {
        "<경매 3계>": f"<{division}>" if division else "<경매 3계>",
        "2025. 4. 3.[목] 10:30": auction_value or "2025. 4. 3.[목] 10:30",
        "2025. 4. 10.[목] 15:30": decision_value or "2025. 4. 10.[목] 15:30",
        "의정부지방법원 제6호법정(2별관101호) ": sale_place or "의정부지방법원 제6호법정(2별관101호) ",
        "의정부지방법원  사법보좌관 장용석": officer_line or "의정부지방법원  사법보좌관 장용석",
    }
    for node in root.iter("Content"):
        if node.text in replacements:
            node.text = replacements[node.text]
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def make_content_nodes(text: str) -> list[ET.Element]:
    parts = text.split("\n")
    out: list[ET.Element] = []
    for idx, piece in enumerate(parts):
        node = ET.Element("Content")
        node.text = piece
        out.append(node)
        if idx < len(parts) - 1:
            out.append(ET.Element("Br"))
    return out


def set_story_text(story_xml: bytes, text: str) -> bytes:
    root = ET.fromstring(story_xml)
    story = root.find("Story")
    paragraph = story.find("ParagraphStyleRange") if story is not None else None
    if paragraph is None:
        paragraph = ET.SubElement(story, "ParagraphStyleRange", {"AppliedParagraphStyle": "ParagraphStyle/본문제목"})
    character = paragraph.find("CharacterStyleRange")
    if character is None:
        character = ET.SubElement(paragraph, "CharacterStyleRange", {"AppliedCharacterStyle": "CharacterStyle/$ID/[No character style]"})
    for child in list(character):
        character.remove(child)
    for node in make_content_nodes(text):
        character.append(node)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def set_or_update_properties(parent: ET.Element, values: dict[str, str]) -> None:
    props = parent.find("Properties")
    if props is None:
        props = ET.SubElement(parent, "Properties")
    for key, value in values.items():
        node = props.find(key)
        if node is None:
            node = ET.SubElement(props, key, {"type": "unit"})
        node.text = value


def update_cell(cell: ET.Element, text: str, col_idx: int, dense: bool, ultra_dense: bool = False) -> None:
    cell.attrib["VerticalJustification"] = "TopAlign"
    for k in ["TopInset", "BottomInset", "LeftInset", "RightInset", "TextTopInset", "TextBottomInset", "TextLeftInset", "TextRightInset"]:
        if k in cell.attrib:
            cell.attrib[k] = "0.4" if "Top" in k or "Bottom" in k else "0.8"
    paragraph = cell.find("ParagraphStyleRange")
    if paragraph is None:
        paragraph = ET.SubElement(cell, "ParagraphStyleRange", {"AppliedParagraphStyle": "ParagraphStyle/표-내용"})
    character = paragraph.find("CharacterStyleRange")
    if character is None:
        character = ET.SubElement(paragraph, "CharacterStyleRange", {"AppliedCharacterStyle": "CharacterStyle/$ID/[No character style]"})
    if col_idx == 2:
        character.attrib["AppliedCharacterStyle"] = "CharacterStyle/표 내용"
        if ultra_dense:
            character.attrib["PointSize"] = "5.1"
            character.attrib["HorizontalScale"] = "74"
            character.attrib["Tracking"] = "-75"
        else:
            character.attrib["PointSize"] = "5.6" if dense else "6.0"
            character.attrib["HorizontalScale"] = "76" if dense else "82"
            character.attrib["Tracking"] = "-60" if dense else "-45"
    elif col_idx in (0, 4, 5):
        if ultra_dense:
            character.attrib["PointSize"] = "5.0"
            character.attrib["HorizontalScale"] = "82"
            character.attrib["Tracking"] = "-40"
        else:
            character.attrib["PointSize"] = "5.4" if dense else "5.8"
            character.attrib["HorizontalScale"] = "84"
            character.attrib["Tracking"] = "-30"
    else:
        if ultra_dense:
            character.attrib["PointSize"] = "5.1"
            character.attrib["HorizontalScale"] = "82"
            character.attrib["Tracking"] = "-35"
        else:
            character.attrib["PointSize"] = "5.5" if dense else "5.9"
            character.attrib["HorizontalScale"] = "84"
            character.attrib["Tracking"] = "-25"
    set_or_update_properties(paragraph, {"Leading": "5.7" if ultra_dense else ("6.2" if dense else "6.8")})
    for child in list(character):
        character.remove(child)
    for node in make_content_nodes(text):
        character.append(node)


def formatted_rows(entries: list[dict]) -> list[list[str]]:
    rows = []
    for entry in entries:
        base = format_entry(entry)
        normalized = normalize_entry_for_overlay(base)
        rows.append(
            [
                normalized["case"],
                normalized["item"],
                normalized["location"],
                normalized["usage"],
                normalized["price"],
                normalized["note"],
            ]
        )
    return rows or [["", "", "", "", "", ""]]


def compress_for_idml(group: str, row: list[str]) -> list[str]:
    case, item, location, usage, price, note = row
    if group in {"기타", "대지/임야/전답"}:
        location = location.replace("\n", " ")
        usage = usage.replace("\n", "/")
        note = note.replace(".", "·")
    elif group in {"상가/오피스텔,근린시설", "단독주택,다가구주택"}:
        location = location.replace("\n", " ")
        usage = usage.replace("\n", "/")
    else:
        usage = usage.replace("\n", "/")
    return [case, item, location, usage, price, note]


def rebuild_story_table(story_xml: bytes, story_id: str, rows: list[list[str]], group: str) -> bytes:
    root = ET.fromstring(story_xml)
    story = root.find("Story")
    if story is None:
        raise ValueError(f"Story missing: {story_id}")
    story.attrib["Self"] = story_id

    paragraph = story.find("ParagraphStyleRange")
    if paragraph is None:
        paragraph = ET.SubElement(story, "ParagraphStyleRange", {"AppliedParagraphStyle": "ParagraphStyle/표-내용"})
    character = paragraph.find("CharacterStyleRange")
    if character is None:
        character = ET.SubElement(paragraph, "CharacterStyleRange", {"AppliedCharacterStyle": "CharacterStyle/$ID/[No character style]"})
    table = character.find("Table")
    if table is None:
        raise ValueError(f"Table missing in story {story_id}")

    row_templates = table.findall("Row")
    col_templates = table.findall("Column")
    cell_templates = table.findall("Cell")[:6]
    for child in list(table):
        if child.tag in {"Row", "Column", "Cell"}:
            table.remove(child)

    table.attrib["Self"] = f"{story_id}Table"
    table.attrib["BodyRowCount"] = str(max(1, len(rows)))
    dense = group in {"기타", "대지/임야/전답"}
    ultra_dense = group == "기타" and len(rows) >= 3

    for row_idx in range(max(1, len(rows))):
        tmpl = copy.deepcopy(row_templates[min(row_idx, len(row_templates) - 1)])
        tmpl.attrib["Self"] = f"{story_id}Row{row_idx}"
        tmpl.attrib["Name"] = str(row_idx)
        if ultra_dense:
            tmpl.attrib["SingleRowHeight"] = "9.6"
            tmpl.attrib["MinimumHeight"] = "5.2"
        else:
            tmpl.attrib["SingleRowHeight"] = "11.2" if dense else "13.0"
            tmpl.attrib["MinimumHeight"] = "5.8"
        for k in ["TextTopInset", "TextBottomInset", "TextLeftInset", "TextRightInset"]:
            if k in tmpl.attrib:
                tmpl.attrib[k] = "0.4" if "Top" in k or "Bottom" in k else "0.8"
        table.append(tmpl)
    for col_idx, col in enumerate(col_templates):
        tmpl = copy.deepcopy(col)
        tmpl.attrib["Self"] = f"{story_id}Col{col_idx}"
        tmpl.attrib["Name"] = str(col_idx)
        if ultra_dense:
            widths = ["28", "11", "170", "22", "38", "51"]
        elif dense:
            widths = ["30", "12", "164", "24", "40", "50"]
        elif group == "연립주택/다세대/빌라":
            widths = ["32", "12", "158", "28", "42", "48"]
        else:
            widths = ["32", "12", "156", "28", "42", "52"]
        tmpl.attrib["SingleColumnWidth"] = widths[col_idx]
        table.append(tmpl)
    for row_idx, values in enumerate(rows):
        values = compress_for_idml(group, values)
        for col_idx, value in enumerate(values):
            cell = copy.deepcopy(cell_templates[col_idx])
            cell.attrib["Self"] = f"{story_id}Cell{col_idx}_{row_idx}"
            cell.attrib["Name"] = f"{col_idx}:{row_idx}"
            update_cell(cell, value, col_idx, dense, ultra_dense)
            table.append(cell)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def set_frame_bounds(frame: ET.Element, bounds: tuple[float, float, float, float]) -> None:
    x0, y0, x1, y1 = bounds
    frame.attrib["ItemTransform"] = "1 0 0 1 0 0"
    path_points = frame.find(".//PathPointArray")
    if path_points is None:
        return
    points = [
        (x0, y0),
        (x0, y1),
        (x1, y1),
        (x1, y0),
    ]
    for elem, (x, y) in zip(list(path_points), points):
        value = f"{x} {y}"
        elem.attrib["Anchor"] = value
        elem.attrib["LeftDirection"] = value
        elem.attrib["RightDirection"] = value
    pref = frame.find("TextFramePreference")
    if pref is not None:
        pref.attrib["TextColumnFixedWidth"] = str(x1 - x0)


def active_groups(groups: dict[str, list[dict]]) -> list[str]:
    return [group for group in SLOT_FILL_ORDER if groups.get(group)]


def load_payload(data_json: Path) -> tuple[str, object]:
    payload = json.loads(data_json.read_text(encoding="utf-8"))
    if "slots" in payload:
        return "slots", payload
    groups = {group: [] for group in GROUP_ORDER}
    for entry in payload["entries"]:
        groups[format_entry(entry)["group"]].append(entry)
    return "groups", groups


def rebuild(template_idml: Path, data_json: Path, output_idml: Path) -> None:
    payload_mode, payload_data = load_payload(data_json)
    meta = extract_meta(payload_mode, payload_data)

    with zipfile.ZipFile(template_idml) as zin:
        files = {info.filename: zin.read(info.filename) for info in zin.infolist()}
        infos = {info.filename: info for info in zin.infolist()}

    spread_root = ET.fromstring(files["Spreads/Spread_ud6.xml"])
    spread = spread_root.find("Spread")
    if spread is None:
        raise ValueError("Spread element missing")

    frame_by_story: dict[str, ET.Element] = {}
    for tf in spread.findall("TextFrame"):
        story = tf.attrib.get("ParentStory")
        if story:
            frame_by_story[story] = tf

    if "ue5" not in frame_by_story:
        donor = copy.deepcopy(frame_by_story["u4772"])
        donor.attrib["Self"] = "u9000"
        donor.attrib["ParentStory"] = "ue5"
        donor.attrib["PreviousTextFrame"] = "n"
        donor.attrib["NextTextFrame"] = "n"
        set_frame_bounds(donor, NEW_BODY_BOUNDS)
        spread.append(donor)
        frame_by_story["ue5"] = donor

    set_frame_bounds(frame_by_story["u3f5a"], NEW_LABEL_BOUNDS)

    updated_files = dict(files)
    if payload_mode == "slots":
        raw_slots = list(payload_data["slots"])
        slot_payloads = raw_slots + [{"group": None, "entries": []}] * max(0, len(SLOTS) - len(raw_slots))
    else:
        groups = payload_data
        visible_groups = active_groups(groups)
        slot_payloads = []
        for idx in range(len(SLOTS)):
            if idx < len(visible_groups):
                group = visible_groups[idx]
                slot_payloads.append({"group": group, "entries": groups[group]})
            else:
                slot_payloads.append({"group": None, "entries": []})

    for (label_story, body_story), slot_payload in zip(SLOTS, slot_payloads):
        assigned_group = slot_payload["group"]
        label_path = f"Stories/Story_{label_story}.xml"
        body_path = f"Stories/Story_{body_story}.xml"

        if assigned_group is None:
            updated_files[label_path] = set_story_text(files[label_path], "")
            donor_id = DONOR_BY_STORY[body_story]
            donor_path = f"Stories/Story_{donor_id}.xml"
            updated_files[body_path] = rebuild_story_table(files[donor_path], body_story, [["", "", "", "", "", ""]], "아파트")
            if label_story in frame_by_story:
                set_frame_bounds(frame_by_story[label_story], HIDDEN_BOUNDS)
            if body_story in frame_by_story:
                set_frame_bounds(frame_by_story[body_story], HIDDEN_BOUNDS)
            continue

        updated_files[label_path] = set_story_text(files[label_path], LABEL_TEXT_BY_GROUP[assigned_group])
        donor_id = DONOR_BY_STORY[body_story]
        donor_path = f"Stories/Story_{donor_id}.xml"
        updated_files[body_path] = rebuild_story_table(
            files[donor_path],
            body_story,
            formatted_rows(slot_payload["entries"]),
            assigned_group,
        )

    for story_id in ("ua16", "u483c"):
        story_path = f"Stories/Story_{story_id}.xml"
        updated_files[story_path] = update_meta_story(files[story_path], meta)

    updated_files["Spreads/Spread_ud6.xml"] = ET.tostring(spread_root, encoding="utf-8", xml_declaration=True)

    output_idml.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_idml, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for filename, data in updated_files.items():
            zout.writestr(infos[filename], data)


def main() -> int:
    parser = argparse.ArgumentParser(description="IDML 내부의 프레임/표를 직접 재구성합니다.")
    parser.add_argument("template_idml", type=Path)
    parser.add_argument("data_json", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()
    rebuild(args.template_idml, args.data_json, args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
