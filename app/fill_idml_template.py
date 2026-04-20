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

STORY_GROUP_MAP = {
    "u4638": "아파트",
    "u4772": "연립주택/다세대/빌라",
    "ue5": "단독주택,다가구주택",
    "u4795": "대지/임야/전답",
    "u47e6": "상가/오피스텔,근린시설",
    "u47b5": "기타",
}

DONOR_STORY_MAP = {
    "u4638": "u4638",
    "u4772": "u4772",
    "ue5": "u4772",
    "u4795": "u4795",
    "u47e6": "u47e6",
    "u47b5": "u47b5",
}


def make_content_nodes(text: str) -> list[ET.Element]:
    nodes: list[ET.Element] = []
    for idx, piece in enumerate(text.split("\n")):
        content = ET.Element("Content")
        content.text = piece
        nodes.append(content)
        if idx < len(text.split("\n")) - 1:
            nodes.append(ET.Element("Br"))
    return nodes


def update_cell(cell: ET.Element, text: str) -> None:
    paragraph = cell.find("ParagraphStyleRange")
    if paragraph is None:
        paragraph = ET.SubElement(cell, "ParagraphStyleRange", {"AppliedParagraphStyle": "ParagraphStyle/표-내용"})
    character = paragraph.find("CharacterStyleRange")
    if character is None:
        character = ET.SubElement(
            paragraph,
            "CharacterStyleRange",
            {"AppliedCharacterStyle": "CharacterStyle/$ID/[No character style]"},
        )
    for child in list(character):
        character.remove(child)
    for node in make_content_nodes(text):
        character.append(node)


def formatted_rows(entries: list[dict]) -> list[list[str]]:
    rows: list[list[str]] = []
    for entry in entries:
        normalized = normalize_entry_for_overlay(format_entry(entry))
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


def rebuild_story_table(story_xml: bytes, story_id: str, rows: list[list[str]]) -> bytes:
    root = ET.fromstring(story_xml)
    story = root.find("Story")
    if story is None:
        raise ValueError(f"Story element missing for {story_id}")
    story.attrib["Self"] = story_id

    paragraph = story.find("ParagraphStyleRange")
    if paragraph is None:
        paragraph = ET.SubElement(story, "ParagraphStyleRange", {"AppliedParagraphStyle": "ParagraphStyle/표-내용"})
    character = paragraph.find("CharacterStyleRange")
    if character is None:
        character = ET.SubElement(
            paragraph,
            "CharacterStyleRange",
            {"AppliedCharacterStyle": "CharacterStyle/$ID/[No character style]"},
        )
    table = character.find("Table")
    if table is None:
        raise ValueError(f"Table missing for story {story_id}")

    row_templates = table.findall("Row")
    cell_templates = table.findall("Cell")[:6]
    columns = table.findall("Column")
    for elem in row_templates + table.findall("Cell"):
        table.remove(elem)

    table.attrib["Self"] = f"{story_id}Table"
    table.attrib["BodyRowCount"] = str(max(1, len(rows)))

    for row_idx in range(max(1, len(rows))):
        template = copy.deepcopy(row_templates[min(row_idx, len(row_templates) - 1)])
        template.attrib["Self"] = f"{story_id}Row{row_idx}"
        template.attrib["Name"] = str(row_idx)
        table.append(template)

    insert_at = len(table.findall("Row")) + len(columns)
    for row_idx, values in enumerate(rows):
        for col_idx, value in enumerate(values):
            cell = copy.deepcopy(cell_templates[col_idx])
            cell.attrib["Self"] = f"{story_id}Cell{col_idx}_{row_idx}"
            cell.attrib["Name"] = f"{col_idx}:{row_idx}"
            update_cell(cell, value)
            table.insert(insert_at, cell)
            insert_at += 1

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def replace_header_story(data: bytes, text: str) -> bytes:
    root = ET.fromstring(data)
    story = root.find("Story")
    if story is None:
        return data
    paragraph = story.find("ParagraphStyleRange")
    if paragraph is None:
        return data
    character = paragraph.find("CharacterStyleRange")
    if character is None:
        return data
    for child in list(character):
        character.remove(child)
    content = ET.Element("Content")
    content.text = text
    character.append(content)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def fill_template(template_path: Path, data_path: Path, output_path: Path) -> None:
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    groups = {group: [] for group in GROUP_ORDER}
    for entry in payload["entries"]:
        groups[format_entry(entry)["group"]].append(entry)

    with zipfile.ZipFile(template_path) as zin:
        source_files = {info.filename: zin.read(info.filename) for info in zin.infolist()}
        source_infos = {info.filename: info for info in zin.infolist()}

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for filename, data in source_files.items():
            info = source_infos[filename]
            if filename.startswith("Stories/Story_") and filename.endswith(".xml"):
                story_id = filename.split("Story_")[1].split(".xml")[0]
                if story_id in STORY_GROUP_MAP:
                    donor_id = DONOR_STORY_MAP[story_id]
                    donor_xml = source_files[f"Stories/Story_{donor_id}.xml"]
                    data = rebuild_story_table(donor_xml, story_id, formatted_rows(groups[STORY_GROUP_MAP[story_id]]))
                elif story_id == "u96f":
                    data = replace_header_story(data, "법원 경매부동산의 매각 공고")
            zout.writestr(info, data)


def main() -> int:
    parser = argparse.ArgumentParser(description="IDML 템플릿의 표 스토리에 현재 경매 내용을 주입합니다.")
    parser.add_argument("template_idml", type=Path)
    parser.add_argument("data_json", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()
    fill_template(args.template_idml, args.data_json, args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
