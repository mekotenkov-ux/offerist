#!/usr/bin/env python3
"""Compile a validated proposal into staged Google Docs requests, without credentials.

Run stages in order: skeleton, tables, fill, images (optional), style, verify.
Each stage consumes the actual native readback from the preceding mutation.
The compiler never deletes or replaces a pre-existing document body.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
from typing import Any

import proposal_contract as contract


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def unwrap(value: dict) -> dict:
    return value.get("structuredContent", value)


def tab_body(readback: dict, tab_id: str | None = None) -> tuple[dict, str, list]:
    doc = unwrap(readback)
    tabs = doc.get("tabs") or []
    found = []

    def visit(items):
        for item in items:
            props = item.get("tabProperties", item)
            native = item.get("documentTab", item)
            if native.get("body"):
                found.append((native, props.get("tabId", native.get("tabId"))))
            visit(item.get("childTabs", []))

    visit(tabs)
    if not found and doc.get("body"):
        found.append((doc, tab_id))
    matches = [(t, tid) for t, tid in found if tab_id is None or tid == tab_id]
    if len(matches) != 1:
        raise ValueError("Select exactly one native tab using --tab-id")
    tab, tid = matches[0]
    if not tid:
        raise ValueError("Native readback must include tabId")
    return doc, tid, tab["body"].get("content", [])


def paragraph_text(element: dict) -> str:
    return "".join(x.get("textRun", {}).get("content", "")
                   for x in element.get("paragraph", {}).get("elements", []))


def table_marker(key: str) -> str:
    return f"[[OFFERIST_TABLE:{key}]]"


def image_marker(section_id: str) -> str:
    return f"[[OFFERIST_IMAGE:{section_id}]]"


def cell_text(value: Any) -> str:
    return contract.format_cell(value)


def linked_cell(value: Any, section_id: str) -> tuple[str, str | None]:
    """Shared presentation helper, retained here for native-client compatibility."""
    return contract.linked_cell(value, section_id)


def score_text(label: str, rating: dict) -> str:
    value = rating.get("value")
    shown = "Не оценено" if value is None else f"{contract.format_cell(value)}/10"
    return f"{label}\n{shown}\n{rating['reason']}"


def check_data(data: dict, template: dict) -> None:
    errors = contract.validate(data, template, client_ready=data.get("mode") == "client")
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))


def specification(data: dict, template: dict) -> dict:
    """One ordered content plan shared by every native compilation stage.

    Provenance fields are deliberately absent: author-facing evidence metadata
    must never turn into automatic client footers.
    """
    check_data(data, template)
    by_id = {s["id"]: s for s in data["sections"]}
    sections, tables, image_specs = [], [], []

    def make_table(section_id, definition, values, *, detail=False):
        rows, cell_links, notes = [definition["columns"]], {}, []
        for ri, values_row in enumerate(values["rows"], 1):
            row = []
            for ci, value in enumerate(values_row):
                if isinstance(value, dict) and value.get("value") is None:
                    label, link = "Н/Д", None
                    note = " ".join(value["reason"].split())
                    if note not in notes:
                        notes.append(note)
                else:
                    label, link = linked_cell(value, section_id)
                row.append(label)
                if link:
                    cell_links[f"{ri}:{ci}"] = link
            rows.append(row)
        key = f"{section_id}:{'detail:' if detail else ''}{definition['id']}"
        return {"key": key, "kind": "data", "section_id": section_id,
                "title": definition.get("title", ""),
                "page_break_before": key == "competitors:link_comparison",
                "rows": rows, "cell_links": cell_links, "notes": notes}

    for definition in template["sections"]:
        section = by_id[definition["id"]]
        sid = definition["id"]
        entry = {"id": definition["id"], "heading": contract.heading(definition),
                 "level": definition.get("level", 1), "definition": definition,
                 "data": section, "tables": [], "blocks": []}

        def paragraph(text, role="body"):
            if text:
                for line in text.split("\n"):
                    if line.strip():
                        entry["blocks"].append({"type": "paragraph", "role": role, "text": line})

        def links(items):
            # Each label has its own paragraph and URL, avoiding substring
            # guessing when a label is repeated in other evidence blocks.
            for item in items or []:
                for line in item["label"].split("\n"):
                    if line.strip():
                        entry["blocks"].append({"type": "paragraph", "role": "link",
                                                "text": line, "url": item["url"]})

        def append_table(item):
            first_title = len(entry["blocks"])
            paragraph(item["title"], "table_title")
            if len(entry["blocks"]) > first_title:
                entry["blocks"][first_title]["page_break_before"] = item.get("page_break_before", False)
            entry["tables"].append(item)
            tables.append(item)
            entry["blocks"].append({"type": "table", "table": item})
            paragraph(table_note(item), "note")

        def append_image(key, visual, role):
            item = {"type": "image", "key": key, "section_id": sid,
                    "visual": visual, "role": role}
            entry["blocks"].append(item)
            image_specs.append(item)

        paragraph(section.get("intro"), "intro")
        links(section.get("analysis_links"))
        paragraph(section["takeaway"], "takeaway")
        data_tables = {t["id"]: t for t in section.get("tables", [])}
        fixed_tables = [make_table(sid, t, data_tables[t["id"]])
                        for t in definition.get("tables", [])]
        # The first canonical table is the actionable opportunity/task overview.
        if fixed_tables:
            append_table(fixed_tables[0])
        if definition["kind"] == "analysis":
            score = {"key": f"{sid}:scores", "kind": "scores",
                     "section_id": sid, "title": "", "page_break_before": False,
                     "rows": [[score_text("Состояние", section["state"]),
                               score_text("Потенциал работ", section["opportunity"])]]}
            append_table(score)
        for item in fixed_tables[1:]:
            append_table(item)
        for index, detail in enumerate(section.get("details", [])):
            kind = detail["type"]
            if kind == "paragraph":
                paragraph(detail["text"])
            elif kind == "example":
                paragraph(detail["title"], "detail_title")
                for field in ("observation", "implication", "action"):
                    paragraph(detail[field])
                links(detail.get("links"))
            elif kind == "table":
                append_table(make_table(sid, detail, detail, detail=True))
                paragraph(detail["interpretation"], "interpretation")
                links(detail.get("links"))
            elif kind == "image":
                append_image(f"{sid}:detail:{index}", detail, "evidence")
                paragraph(detail["caption"], "caption")
                paragraph(detail["interpretation"], "interpretation")
                links(detail.get("links"))
        if section.get("visual"):
            append_image(sid, section["visual"], "visual")
            paragraph(section["visual_explanation"], "interpretation")
        elif section.get("visual_unavailable_reason"):
            paragraph(section["visual_unavailable_reason"], "note")
        sections.append(entry)
    return {"sections": sections, "tables": tables, "images": image_specs}


def range_at(start: int, end: int, tid: str) -> dict:
    if end <= start:
        raise ValueError("Empty or reversed native range")
    return {"startIndex": start, "endIndex": end, "tabId": tid}


def table_note(table: dict) -> str:
    return "Н/Д — " + "; ".join(table["notes"]) if table.get("notes") else ""


def point(index: int, tid: str) -> dict:
    return {"index": index, "tabId": tid}


def dim(value: float) -> dict:
    return {"magnitude": value, "unit": "PT"}


def color(hex_value: str) -> dict:
    h = hex_value.lstrip("#")
    return {"color": {"rgbColor": {
        name: int(h[offset:offset + 2], 16) / 255
        for name, offset in (("red", 0), ("green", 2), ("blue", 4))
    }}}


def request_style(kind: str, target: dict, style: dict) -> dict:
    style_key = {"updateTextStyle": "textStyle",
                 "updateParagraphStyle": "paragraphStyle",
                 "updateTableCellStyle": "tableCellStyle"}[kind]
    return {kind: {**target, style_key: style, "fields": ",".join(style)}}


def skeleton(data: dict, template: dict, readback: dict, tab_id=None) -> list[dict]:
    _, tid, body = tab_body(readback, tab_id)
    if any("table" in e or paragraph_text(e).strip() or any(
            "inlineObjectElement" in x for x in e.get("paragraph", {}).get("elements", []))
           for e in body):
        raise ValueError("skeleton only accepts an empty new document")
    lines = []
    for section in specification(data, template)["sections"]:
        s = section["data"]
        lines.append(section["heading"])
        if data["mode"] == "demo":
            lines.append("ДЕМОНСТРАЦИОННЫЙ ПРИМЕР · СИНТЕТИЧЕСКИЕ ДАННЫЕ")
        if section["id"] == "cover":
            client = data["client"]
            lines.extend([f"{client['name']} · {client['domain']}", client["date"]])
        for block in section["blocks"]:
            if block["type"] == "paragraph":
                lines.append(block["text"])
            elif block["type"] == "table":
                lines.append(table_marker(block["table"]["key"]))
            elif block["type"] == "image":
                lines.append(image_marker(block["key"]))
        lines.append("")
    text = "\n".join(lines) + "\n"
    return [{"insertText": {"location": point(1, tid), "text": text}}]


def marker_locations(body: list, markers: set[str]) -> dict[str, dict]:
    found = {}
    for element in body:
        text = paragraph_text(element).rstrip("\n")
        if text in markers:
            if text in found:
                raise ValueError(f"Duplicate marker: {text}")
            found[text] = element
    missing = markers - found.keys()
    if missing:
        raise ValueError(f"Missing native markers: {sorted(missing)}")
    return found


def tables(data: dict, template: dict, readback: dict, tab_id=None) -> list[dict]:
    _, tid, body = tab_body(readback, tab_id)
    if any("table" in e for e in body):
        raise ValueError("tables stage requires a skeleton without existing tables")
    specs = specification(data, template)["tables"]
    found = marker_locations(body, {table_marker(t["key"]) for t in specs})
    ordered = sorted(specs, key=lambda t: found[table_marker(t["key"])]["startIndex"], reverse=True)
    return [{"insertTable": {"rows": len(t["rows"]), "columns": len(t["rows"][0]),
                             "location": point(found[table_marker(t["key"])]["startIndex"], tid)}}
            for t in ordered]


def resolve_tables(body: list, specs: list) -> list[tuple[dict, dict]]:
    actual = [e for e in body if "table" in e]
    if len(actual) != len(specs):
        raise ValueError(f"Native table count {len(actual)} != expected {len(specs)}")
    for element, spec in zip(actual, specs):
        rows = element["table"]["tableRows"]
        if len(rows) != len(spec["rows"]) or any(len(r["tableCells"]) != len(spec["rows"][i])
                                               for i, r in enumerate(rows)):
            raise ValueError(f"Native table shape differs: {spec['key']}")
    return list(zip(actual, specs))


def resolve_blocks(body: list, spec: dict, data: dict) -> list[tuple[dict, dict, dict]]:
    """Bind every authored block to its exact native position, in order.

    Empty paragraphs left by table insertion are harmless; extra visible text,
    moved evidence, and missing images are not. Images may still be markers
    while preparing the images stage.
    """
    headings = {s["heading"]: s for s in spec["sections"]}
    table_keys = {actual["startIndex"]: table["key"]
                  for actual, table in resolve_tables(body, spec["tables"])}
    observed = {s["id"]: [] for s in spec["sections"]}
    current = None
    for element in body:
        value = paragraph_text(element).rstrip("\n")
        if value in headings:
            current = headings[value]
            continue
        if current is None:
            continue
        if "table" in element:
            observed[current["id"]].append(("table", table_keys[element["startIndex"]], element))
            continue
        if not value.strip() and not any("inlineObjectElement" in x
                                        for x in element.get("paragraph", {}).get("elements", [])):
            continue
        if value == "ДЕМОНСТРАЦИОННЫЙ ПРИМЕР · СИНТЕТИЧЕСКИЕ ДАННЫЕ":
            continue
        if current["id"] == "cover" and value in {
            f"{data['client']['name']} · {data['client']['domain']}", data["client"]["date"]
        }:
            continue
        inline_images = [x for x in element.get("paragraph", {}).get("elements", [])
                         if "inlineObjectElement" in x]
        if value.startswith("[[OFFERIST_IMAGE:") and value.endswith("]]"):
            observed[current["id"]].append(("image", value, element))
        elif inline_images:
            if len(inline_images) != 1 or value.strip():
                raise ValueError(f"Evidence image must occupy its own paragraph: {current['id']}")
            observed[current["id"]].append(("image", None, element))
        else:
            observed[current["id"]].append(("paragraph", value, element))
    result = []
    for section in spec["sections"]:
        actual = observed[section["id"]]
        if len(actual) != len(section["blocks"]):
            raise ValueError(f"Native content block count differs in {section['id']}: "
                             f"{len(actual)} != {len(section['blocks'])}")
        for block, (kind, value, element) in zip(section["blocks"], actual):
            expected = (block.get("text") if block["type"] == "paragraph"
                        else block["table"]["key"] if block["type"] == "table"
                        else image_marker(block["key"]))
            if kind != block["type"] or (value is not None and value != expected):
                raise ValueError(f"Native content order/text differs in {section['id']}: "
                                 f"expected {block['type']} {expected!r}, found {kind} {value!r}")
            result.append((section, block, element))
    return result


def fill(data: dict, template: dict, readback: dict, tab_id=None) -> list[dict]:
    _, tid, body = tab_body(readback, tab_id)
    specs = specification(data, template)["tables"]
    markers = marker_locations(body, {table_marker(t["key"]) for t in specs})
    operations = []
    for actual, spec in resolve_tables(body, specs):
        marker = markers[table_marker(spec["key"])]
        # The marker immediately follows its inserted table; reject shifted mappings.
        if marker["startIndex"] < actual["endIndex"]:
            raise ValueError(f"Table marker precedes its table: {spec['key']}")
        for row, values in zip(actual["table"]["tableRows"], spec["rows"]):
            for cell, value in zip(row["tableCells"], values):
                paragraphs = [p for p in cell["content"] if "paragraph" in p]
                if len(paragraphs) != 1 or paragraph_text(paragraphs[0]).strip():
                    raise ValueError("fill only accepts empty native cells")
                if value:
                    index = paragraphs[0]["startIndex"]
                    operations.append((index, {"insertText": {
                        "location": point(index, tid), "text": str(value)}}))
        start = marker["startIndex"]
        operations.append((start, {"deleteContentRange": {
            "range": range_at(start, start + utf16_length(table_marker(spec["key"])), tid)}}))
    return [request for _, request in sorted(operations, key=lambda x: x[0], reverse=True)]


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Not a PNG image: {path}")
    return struct.unpack(">II", header[16:24])


def images(data: dict, template: dict, readback: dict, visual_dir: Path | None, tab_id=None) -> list[dict]:
    _, tid, body = tab_body(readback, tab_id)
    spec = specification(data, template)
    found = {}
    for _, block, element in resolve_blocks(body, spec, data):
        if block["type"] != "image":
            continue
        value = paragraph_text(element).rstrip("\n")
        if value == image_marker(block["key"]):
            found[block["key"]] = (element["startIndex"], utf16_length(value))
        else:
            inline = next(x for x in element["paragraph"]["elements"] if "inlineObjectElement" in x)
            found[block["key"]] = (inline["startIndex"], 1)
    batches = []
    width = template["style"]["content_width_pt"]
    for item in sorted(spec["images"], key=lambda s: found[s["key"]][0], reverse=True):
        visual = item["visual"]
        if visual["type"] == "image":
            path = Path(visual["path"]).resolve()
        elif visual_dir is not None:
            path = (visual_dir / f"visual-{item['section_id']}.png").resolve()
        else:
            raise ValueError("Chart images require --visual-dir; evidence images use their local paths")
        if not path.is_file():
            raise ValueError(f"Missing visual image: {path}")
        _, px, py = contract.image_info(path)
        max_height = template["style"]["page_height_pt"] - 2 * template["style"]["margin_pt"] - 80
        actual_width = min(width, max_height * px / py) if visual["type"] == "image" else min(width, 180 * px / py)
        index, length = found[item["key"]]
        batches.append({"image_uris": str(path), "requests": [
            {"deleteContentRange": {"range": range_at(
                index, index + length, tid)}},
            {"insertInlineImage": {"location": point(index, tid), "uri": str(path),
                                   "objectSize": {"width": dim(actual_width),
                                                  "height": dim(actual_width * py / px)}}}
        ]})
    return batches


def table_column_proportions(key: str, columns: int) -> list[float]:
    """Readable fixed widths keyed to canonical table identities."""
    widths = {
        "roadmap:roadmap": [46, 18, 18, 18],
        "semantic:page_scope": [26, 14, 12, 48],
        "budget:estimate": [38, 13, 15, 10, 10, 14],
        "budget:external": [40, 20, 20, 20],
        "preparation:analysis_scope": [20, 27, 37, 16],
        "examples:examples": [30, 48, 22],
    }.get(key)
    if widths is None:
        return [1 / columns] * columns
    if len(widths) != columns:
        raise ValueError(f"Column layout differs from canonical table {key}")
    return [value / sum(widths) for value in widths]


def style(data: dict, template: dict, readback: dict, tab_id=None) -> list[dict]:
    _, tid, body = tab_body(readback, tab_id)
    spec = specification(data, template)
    block_positions = {element["startIndex"]: block
                       for _, block, element in resolve_blocks(body, spec, data)}
    config = template["style"]
    end = body[-1]["endIndex"] - 1
    all_range = range_at(1, end, tid)
    document_style = {f"margin{side}": dim(config["margin_pt"])
                      for side in ("Top", "Bottom", "Left", "Right")}
    document_style.update(pageSize={"width": dim(config["page_width_pt"]),
                                    "height": dim(config["page_height_pt"])},
                          documentFormat={"documentMode": "PAGES"})
    requests = [{"updateDocumentStyle": {"documentStyle": document_style,
                                         "fields": ",".join(document_style)}}]
    # pageBreakBefore must never target a range spanning table paragraphs.
    requests.append(request_style("updateParagraphStyle", {"range": all_range}, {
        "namedStyleType": "NORMAL_TEXT", "lineSpacing": 110,
        "spaceAbove": dim(0), "spaceBelow": dim(5), "keepWithNext": False,
        "keepLinesTogether": True}))
    requests.append(request_style("updateTextStyle", {"range": all_range}, {
        "fontSize": dim(config["body_pt"]), "weightedFontFamily": {"fontFamily": config["font_family"]},
        "foregroundColor": color("#17212B"), "bold": False, "italic": False}))
    headings = {s["heading"]: s for s in spec["sections"]}
    current = None
    for element in body:
        if "paragraph" not in element:
            continue
        value = paragraph_text(element).rstrip("\n")
        a, b = element["startIndex"], element["endIndex"]
        block = block_positions.get(a, {})
        target = {"range": range_at(a, b, tid)}
        text_target = {"range": range_at(a, max(a + 1, b - 1), tid)}
        if value in headings:
            current = headings[value]
            level = current["level"]
            named = "TITLE" if current["id"] == "cover" else f"HEADING_{min(max(level, 1), 3)}"
            requests.append(request_style("updateParagraphStyle", target, {
                "namedStyleType": named, "pageBreakBefore": current["id"] != "cover",
                "keepWithNext": True, "spaceAbove": dim(0), "spaceBelow": dim(12)}))
            size = config["heading_sizes_pt"][min(max(level, 1), 3) - 1]
            requests.append(request_style("updateTextStyle", text_target, {
                "fontSize": dim(size), "bold": True, "foregroundColor": color("#17212B")}))
        elif value.startswith("ДЕМОНСТРАЦИОННЫЙ"):
            requests.append(request_style("updateTextStyle", text_target, {
                "fontSize": dim(8.5), "foregroundColor": color("#52606D")}))
            requests.append(request_style("updateParagraphStyle", target, {"keepWithNext": True}))
        elif block.get("role") == "takeaway":
            requests.append(request_style("updateParagraphStyle", target, {
                "shading": {"backgroundColor": color("#D9EAD3")},
                **{f"border{side}": {"color": color("#6DA855"), "width": dim(0.75),
                                    "padding": dim(6), "dashStyle": "SOLID"}
                   for side in ("Top", "Bottom", "Left", "Right")},
                "spaceAbove": dim(8), "spaceBelow": dim(10), "keepWithNext": True}))
            requests.append(request_style("updateTextStyle", text_target, {"bold": True}))
        elif block.get("role") in {"table_title", "detail_title"}:
            requests.append(request_style("updateParagraphStyle", target, {
                "keepWithNext": True, "spaceAbove": dim(10), "spaceBelow": dim(4),
                "pageBreakBefore": bool(block.get("page_break_before"))}))
            requests.append(request_style("updateTextStyle", text_target, {"bold": True, "fontSize": dim(11)}))
        elif block.get("role") == "link":
            requests.append(request_style("updateTextStyle", text_target, {
                "link": {"url": block["url"]}, "bold": False}))
            requests.append(request_style("updateParagraphStyle", target, {
                "spaceAbove": dim(0), "spaceBelow": dim(6), "keepWithNext": True}))
        elif block.get("role") in {"caption", "note"}:
            requests.append(request_style("updateTextStyle", text_target, {
                "fontSize": dim(config["caption_pt"]), "foregroundColor": color("#52606D")}))
            requests.append(request_style("updateParagraphStyle", target, {
                "spaceAbove": dim(3), "spaceBelow": dim(6),
                "keepWithNext": block.get("role") == "caption"}))
        elif block.get("role") == "intro":
            requests.append(request_style("updateTextStyle", text_target, {"bold": False, "italic": False}))
            requests.append(request_style("updateParagraphStyle", target, {
                "keepWithNext": True, "spaceAbove": dim(0), "spaceBelow": dim(6)}))
        elif not value:
            has_image = any("inlineObjectElement" in x for x in element["paragraph"].get("elements", []))
            requests.append(request_style("updateParagraphStyle", target, {
                "spaceAbove": dim(0), "spaceBelow": dim(4 if has_image else 1),
                "pageBreakBefore": bool(has_image and block.get("role") == "visual" and
                                       block.get("visual", {}).get("type") == "image"),
                "keepWithNext": has_image,
                "lineSpacing": 100, "alignment": "CENTER" if has_image else "START"}))
            if not has_image:
                requests.append(request_style("updateTextStyle", text_target, {"fontSize": dim(1)}))
    for actual, table in resolve_tables(body, spec["tables"]):
        index = actual["startIndex"]
        rows = actual["table"]["tableRows"]
        cols = len(rows[0]["tableCells"])

        def table_range(row=0, column=0, row_count=None, column_count=None):
            return {"tableCellLocation": {"tableStartLocation": point(index, tid),
                                         "rowIndex": row, "columnIndex": column},
                    "rowSpan": len(rows) if row_count is None else row_count,
                    "columnSpan": cols if column_count is None else column_count}

        proportions = table_column_proportions(table["key"], cols)
        for ci, proportion in enumerate(proportions):
            requests.append({"updateTableColumnProperties": {
                "tableStartLocation": point(index, tid), "columnIndices": [ci],
                "tableColumnProperties": {"widthType": "FIXED_WIDTH",
                                          "width": dim(config["content_width_pt"] * proportion)},
                "fields": "widthType,width"}})
        requests.append({"updateTableRowStyle": {
            "tableStartLocation": point(index, tid),
            "tableRowStyle": {"preventOverflow": True},
            "fields": "preventOverflow"}})
        if table["kind"] == "data":
            requests.append({"pinTableHeaderRows": {
                "tableStartLocation": point(index, tid), "pinnedHeaderRowsCount": 1}})
        borders = {"color": color("#D8E1E8"), "width": dim(0.5), "dashStyle": "SOLID"}
        cell_style = {f"padding{side}": dim(5) for side in ("Top", "Bottom", "Left", "Right")}
        if table["section_id"] == "budget":
            cell_style.update(paddingTop=dim(4), paddingBottom=dim(4))
        cell_style.update({f"border{side}": borders for side in ("Top", "Bottom", "Left", "Right")})
        cell_style["contentAlignment"] = "TOP"
        requests.append(request_style("updateTableCellStyle", {"tableRange": table_range()}, cell_style))
        shade = "#FFF2CC" if table["kind"] == "scores" else "#D9EAF7"
        requests.append(request_style("updateTableCellStyle", {
            "tableRange": table_range(row_count=1)}, {"backgroundColor": color(shade)}))
        for ri, row in enumerate(rows):
            for ci, cell in enumerate(row["tableCells"]):
                for pi, paragraph in enumerate(p for p in cell["content"] if "paragraph" in p):
                    a, b = paragraph["startIndex"], paragraph["endIndex"]
                    requests.append(request_style("updateParagraphStyle", {
                        "range": range_at(a, b, tid)}, {
                        "lineSpacing": 105, "spaceAbove": dim(0), "spaceBelow": dim(1),
                        "alignment": "CENTER" if table["key"] == "roadmap:roadmap" and ci > 0 else "START",
                        "keepWithNext": table["kind"] == "scores" and pi < 2,
                        "keepLinesTogether": True}))
                    size = 20 if table["kind"] == "scores" and pi == 1 else config["table_pt"]
                    requests.append(request_style("updateTextStyle", {
                        "range": range_at(a, max(a + 1, b - 1), tid)}, {
                        "fontSize": dim(size), "bold": (pi < 2 if table["kind"] == "scores" else ri == 0)}))
                    value = paragraph_text(paragraph).strip()
                    link = table.get("cell_links", {}).get(f"{ri}:{ci}")
                    if link and value:
                        requests.append(request_style("updateTextStyle", {
                            "range": range_at(a, a + utf16_length(value), tid)},
                            {"link": {"url": link}}))
    # Named styles reset font family; setting a family alone resets bold to weight 400.
    # Send family and the intended weight together on each explicitly sized run.
    for request in requests:
        text_request = request.get("updateTextStyle")
        if text_request and "fontSize" in text_request["textStyle"]:
            text_request["textStyle"]["weightedFontFamily"] = {"fontFamily": config["font_family"]}
            text_request["fields"] = ",".join(text_request["textStyle"])
    return requests


def verify(data: dict, template: dict, readback: dict, tab_id=None) -> dict:
    doc, tid, body = tab_body(readback, tab_id)
    spec = specification(data, template)
    issues = []
    expected_headings = [s["heading"] for s in spec["sections"]]
    actual_headings = [paragraph_text(e).strip() for e in body
                       if e.get("paragraph", {}).get("paragraphStyle", {}).get("namedStyleType", "")
                       in {"TITLE", "HEADING_1", "HEADING_2", "HEADING_3"}]
    if actual_headings != expected_headings:
        issues.append({"field": "headings", "message": "Native heading tree/order differs from canonical template"})
    try:
        resolved = resolve_blocks(body, spec, data)
    except ValueError as exc:
        resolved = []
        issues.append({"field": "content.order", "message": str(exc)})
    for section, block, element in resolved:
        if block.get("role") == "link":
            runs = [item["textRun"] for item in element["paragraph"].get("elements", [])
                    if item.get("textRun", {}).get("content", "").strip()]
            if not runs or any(run.get("textStyle", {}).get("link", {}).get("url") != block["url"]
                               for run in runs):
                issues.append({"section": section["id"], "field": "content.link",
                               "message": "Analysis/evidence link target differs from authored URL"})
    for actual, table in resolve_tables(body, spec["tables"]):
        if table_note(table):
            following = next((paragraph_text(e).rstrip("\n") for e in body
                              if e.get("startIndex", 0) >= actual.get("endIndex", 0)
                              and paragraph_text(e).strip()), None)
            if following != table_note(table):
                issues.append({"section": table["section_id"], "field": table["key"] + ".notes",
                               "message": "Unknown-data explanation missing or moved from its table"})
        for ri, (row, expected) in enumerate(zip(actual["table"]["tableRows"], table["rows"])):
            for ci, (cell, value) in enumerate(zip(row["tableCells"], expected)):
                actual_value = "".join(paragraph_text(p) for p in cell["content"]).rstrip("\n")
                if actual_value != str(value):
                    issues.append({"section": table["section_id"], "field": f"{table['key']}[{ri}][{ci}]",
                                   "message": "Native editable cell differs from validated data"})
                link = table.get("cell_links", {}).get(f"{ri}:{ci}")
                if link:
                    visible_runs = [run["textRun"] for p in cell["content"]
                                    for run in p.get("paragraph", {}).get("elements", [])
                                    if run.get("textRun", {}).get("content", "").strip()]
                    if not visible_runs or any(run.get("textStyle", {}).get("link", {}).get("url") != link
                                               for run in visible_runs):
                        issues.append({"section": table["section_id"], "field": f"{table['key']}[{ri}][{ci}].link",
                                       "message": "Native hyperlink target differs from validated URL"})
    whole_text = "\n".join(paragraph_text(e) for e in body)
    if "[[OFFERIST_" in whole_text:
        issues.append({"field": "markers", "message": "Unresolved table/image markers remain"})
    if data["mode"] == "demo" and whole_text.count("ДЕМОНСТРАЦИОННЫЙ ПРИМЕР") != len(spec["sections"]):
        issues.append({"field": "demo", "message": "Every section must display synthetic-data label"})
    expected_images = len(spec["images"])
    actual_images = sum("inlineObjectElement" in p for e in body
                        for p in e.get("paragraph", {}).get("elements", []))
    if actual_images != expected_images:
        issues.append({"field": "images", "message": f"Expected {expected_images}, found {actual_images}"})
    expected_by_heading = {s["heading"]: s for s in spec["sections"]}
    tab = next((t.get("documentTab", t) for t in doc.get("tabs", [])
                if t.get("tabId", t.get("tabProperties", {}).get("tabId")) == tid), doc)
    document_style = tab.get("documentStyle") or doc.get("documentStyle", {})
    for side in ("Top", "Bottom", "Left", "Right"):
        magnitude = document_style.get("margin" + side, {}).get("magnitude")
        if magnitude is None or abs(magnitude - template["style"]["margin_pt"]) > .1:
            issues.append({"field": "margin" + side, "message": "Native margin differs from template"})
    page = document_style.get("pageSize", document_style.get("documentSize", {}))
    for axis, key in (("width", "page_width_pt"), ("height", "page_height_pt")):
        magnitude = page.get(axis, {}).get("magnitude")
        if magnitude is None or abs(magnitude - template["style"][key]) > .1:
            issues.append({"field": "page." + axis, "message": "Native page size differs from template"})
    named_styles = {s["namedStyleType"]: s.get("textStyle", {})
                    for s in (tab.get("namedStyles") or doc.get("namedStyles", {})).get("styles", [])}

    def check_font(paragraph, size, bold, field):
        inherited = named_styles.get(paragraph.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT"), {})
        for run in paragraph.get("elements", []):
            text = run.get("textRun", {})
            if not text.get("content", "").strip():
                continue
            effective = {**inherited, **text.get("textStyle", {})}
            magnitude = effective.get("fontSize", {}).get("magnitude")
            family = effective.get("weightedFontFamily", {}).get("fontFamily")
            if magnitude is None or abs(magnitude - size) > .1 or effective.get("bold", False) != bold:
                issues.append({"field": field, "message": "Native font size/weight differs from template"})
            if family != template["style"]["font_family"]:
                issues.append({"field": field, "message": "Native font family differs from template"})

    for section, block, element in resolved:
        if block.get("role") == "intro":
            check_font(element["paragraph"], template["style"]["body_pt"], False,
                       section["id"] + ".intro.font")
            style = element["paragraph"].get("paragraphStyle", {})
            background = style.get("shading", {}).get("backgroundColor", {})
            while isinstance(background, dict) and "color" in background:
                background = background["color"]
            rgb_background = background.get("rgbColor") if isinstance(background, dict) else None
            if rgb_background is not None and any(rgb_background.get(k, 0) < .99 for k in ("red", "green", "blue")):
                issues.append({"section": section["id"], "field": "intro.style",
                               "message": "Analysis introduction must be plain, without a shaded callout"})
            if any(run.get("textRun", {}).get("textStyle", {}).get("italic")
                   for run in element["paragraph"].get("elements", [])):
                issues.append({"section": section["id"], "field": "intro.style",
                               "message": "Analysis introduction must not use italic emphasis"})

    current, placements = None, {}
    for element in body:
        value = paragraph_text(element).strip()
        if value in expected_by_heading:
            section = expected_by_heading[value]
            current = section["id"]
            level = min(max(section["level"], 1), 3)
            expected_named = "TITLE" if current == "cover" else f"HEADING_{level}"
            if element["paragraph"].get("paragraphStyle", {}).get("namedStyleType") != expected_named:
                issues.append({"section": current, "field": "heading.style", "message": "Wrong native heading level"})
            check_font(element["paragraph"], template["style"]["heading_sizes_pt"][level - 1],
                       True, current + ".heading.font")
        placements[current] = placements.get(current, 0) + sum(
            "inlineObjectElement" in x for x in element.get("paragraph", {}).get("elements", []))
    for section in spec["sections"]:
        section_image_count = sum(b["type"] == "image" for b in section["blocks"])
        if placements.get(section["id"], 0) != section_image_count:
            issues.append({"section": section["id"], "field": "visual.position",
                           "message": "Image is missing, duplicated, or placed in the wrong section"})

    def rgb(value):
        while isinstance(value, dict) and "color" in value:
            value = value["color"]
        return value.get("rgbColor", {}) if isinstance(value, dict) else {}

    for section, block, element in resolved:
        if block.get("role") != "takeaway":
            continue
        paragraph_style = element["paragraph"].get("paragraphStyle", {})
        expected_color = rgb(color("#D9EAD3"))
        actual_color = rgb(paragraph_style.get("shading", {}).get("backgroundColor", {}))
        has_frame = all(paragraph_style.get("border" + side, {}).get("width", {}).get("magnitude", 0) > 0
                        for side in ("Top", "Bottom", "Left", "Right"))
        if not has_frame or any(abs(actual_color.get(k, 0) - v) > .01 for k, v in expected_color.items()):
            issues.append({"section": section["id"], "field": "takeaway.style",
                           "message": "Native takeaway must retain the green framed conclusion"})

    for actual, table in resolve_tables(body, spec["tables"]):
        expected_color = rgb(color("#FFF2CC" if table["kind"] == "scores" else "#D9EAF7"))
        first_row = actual["table"]["tableRows"][0]["tableCells"]
        proportions = table_column_proportions(table["key"], len(first_row))
        if len(set(proportions)) > 1:
            properties = actual["table"].get("tableStyle", {}).get("tableColumnProperties", [])
            if len(properties) != len(proportions) or any(
                abs(prop.get("width", {}).get("magnitude", 0) -
                    template["style"]["content_width_pt"] * fraction) > .1
                for prop, fraction in zip(properties, proportions)
            ):
                issues.append({"section": table["section_id"], "field": table["key"] + ".columns",
                               "message": "Native column widths differ from the keyed layout"})
        if table["key"] == "roadmap:roadmap":
            for row in actual["table"]["tableRows"]:
                for cell in row["tableCells"][1:]:
                    if any(p["paragraph"].get("paragraphStyle", {}).get("alignment") != "CENTER"
                           for p in cell["content"] if "paragraph" in p):
                        issues.append({"section": table["section_id"], "field": table["key"] + ".alignment",
                                       "message": "Month cells must be centered"})
                        break
        for cell in first_row:
            actual_color = rgb(cell.get("tableCellStyle", {}).get("backgroundColor", {}))
            if any(abs(actual_color.get(k, 0) - v) > .01 for k, v in expected_color.items()):
                issues.append({"section": table["section_id"], "field": table["key"] + ".header",
                               "message": "Native table header/card fill differs from template"})
            if table["kind"] == "scores":
                paragraphs = [p for p in cell["content"] if "paragraph" in p]
                if len(paragraphs) >= 2:
                    check_font(paragraphs[1]["paragraph"], 20, True, table["key"] + ".score")
    return {"ok": not issues, "structural_ok": not issues, "mode": data["mode"],
            "client_data_validated": data["mode"] == "client",
            "visual_verification": "requires_export_and_render_inspection",
            "issues": issues, "document_id": doc.get("documentId"),
            "revision": doc.get("revisionId"), "tab_id": tid,
            "headings": len(actual_headings), "editable_tables": len(spec["tables"]),
            "score_cards": 2 * sum(t["kind"] == "scores" for t in spec["tables"]),
            "images": actual_images}


def compile_stage(stage: str, data: dict, template: dict, readback: dict,
                  visual_dir: Path | None = None, tab_id: str | None = None) -> dict:
    check_data(data, template)
    doc = unwrap(readback)
    if not doc.get("documentId") or not doc.get("revisionId"):
        raise ValueError("Readback requires documentId and revisionId")
    if stage == "verify":
        return verify(data, template, readback, tab_id)
    if stage == "images":
        batches = images(data, template, readback, visual_dir, tab_id)
    else:
        requests = globals()[stage](data, template, readback, tab_id)
        batches = [{"requests": requests[i:i + 180]} for i in range(0, len(requests), 180)]
    if batches:
        batches[0]["write_control"] = {"requiredRevisionId": doc["revisionId"]}
    fingerprint = hashlib.sha256(json.dumps([template, data], sort_keys=True,
                                            ensure_ascii=False).encode()).hexdigest()
    return {"stage": stage, "document_id": doc["documentId"], "input_sha256": fingerprint,
            "source_revision": doc["revisionId"], "batches": batches,
            "revision_rule": "Use each batch response writeControl.requiredRevisionId for the next batch; never retry an uncertain write blindly."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["skeleton", "tables", "fill", "images", "style", "verify"])
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--readback", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--tab-id")
    parser.add_argument("--visual-dir", type=Path)
    args = parser.parse_args()
    data = json.loads(args.data.read_text(encoding="utf-8-sig"))
    readback = json.loads(args.readback.read_text(encoding="utf-8-sig"))
    result = compile_stage(args.stage, data, contract.load_template(args.template), readback,
                           args.visual_dir, args.tab_id)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "batches"}, ensure_ascii=False))
    if args.stage == "verify" and not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
