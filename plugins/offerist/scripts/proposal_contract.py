"""Load and validate the fixed Offerist proposal, without network or model calls.

The template owns headings and table columns. Client data owns only content.
An empty client draft is intentionally incomplete; demo data cannot pass
``--client-ready``. This checks structure and preservation, not factual truth.
"""
from __future__ import annotations

import argparse
from datetime import date
import json
import math
from pathlib import Path
import re
import struct
from urllib.parse import urlsplit

from evidence_contract import VERSION as EVIDENCE_VERSION, validate_handoff


TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "assets" / "proposal-template.json"
SCHEMA_VERSION = "offerist-fixed-v2"
SECTION_FIELDS = {
    "id", "takeaway", "state", "opportunity", "tables", "action", "source",
    "source_url", "visual", "visual_unavailable_reason",
    "intro", "analysis_links", "details", "visual_explanation",
}


def load_template(path: str | Path | None = None) -> dict:
    template = json.loads(Path(path or TEMPLATE_PATH).read_text(encoding="utf-8-sig"))
    if not isinstance(template, dict) or template.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported proposal template")
    if not isinstance(template.get("sections"), list) or not template["sections"]:
        raise ValueError("Template must define sections")
    ids = [s.get("id") for s in template["sections"] if isinstance(s, dict)]
    if len(ids) != len(template["sections"]) or len(set(ids)) != len(ids) or not all(ids):
        raise ValueError("Template section IDs must be unique and nonempty")
    return template


def heading(definition: dict) -> str:
    """Use the template's exact Roman number and title, never client overrides."""
    return " ".join(part for part in (definition["number"], definition["title"]) if part)


def analytical_section(definition: dict) -> bool:
    """Competitor comparison and the site overview also need an evidence narrative."""
    return definition["kind"] == "analysis" or definition["id"] in {"competitors", "site_state"}


def format_cell(value) -> str:
    """Shared text representation for HTML, Word and native Google Docs."""
    if value is None:
        return ""
    if isinstance(value, dict) and value.get("value") is None:
        return "Нет данных. " + str(value.get("reason", "")).strip()
    if isinstance(value, float):
        return format(value, ".12g").replace(".", ",")
    return str(value)


def linked_cell(value, section_id: str) -> tuple[str, str | None]:
    """Keep source URLs intact while using short labels for Google artifacts."""
    shown = format_cell(value)
    if not _url(value):
        return shown, None
    parsed = urlsplit(value)
    labels = {"preparation": "Открыть анализ", "examples": "Открыть пример"}
    google_file = (
        parsed.scheme == "https"
        and parsed.hostname in {"docs.google.com", "drive.google.com"}
        and parsed.path.startswith(("/document/d/", "/spreadsheets/d/", "/file/d/", "/drive/folders/"))
    )
    return (labels[section_id] if google_file and section_id in labels else shown), value


def image_info(path: str | Path) -> tuple[str, int, int]:
    """Inspect a bounded local PNG/JPEG header; this is not a full image decoder."""
    if not isinstance(path, (str, Path)):
        raise ValueError("Image path must be an absolute local file path")
    filename = str(path)
    if "://" in filename or filename.startswith(("//", "\\\\")):
        raise ValueError("Image must be local; URLs and network paths are unsupported")
    image_path = Path(path)
    if not image_path.is_absolute():
        raise ValueError("Image path must be absolute")
    image_path = image_path.resolve()
    if str(image_path).startswith(("//", "\\\\")):
        raise ValueError("Image must not resolve to a network path")
    if not image_path.is_file():
        raise ValueError("Image file does not exist")
    suffix = image_path.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg"}:
        raise ValueError("Image file must be PNG or JPEG")
    if not 0 < image_path.stat().st_size <= 20 * 1024 * 1024:
        raise ValueError("Image must be nonempty and at most 20 MiB")
    with image_path.open("rb") as handle:
        content = handle.read(20 * 1024 * 1024 + 1)
    if len(content) > 20 * 1024 * 1024:
        raise ValueError("Image must be at most 20 MiB")
    width = height = 0
    if suffix == ".png":
        if (len(content) < 45 or content[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                or content[-12:] != b"\x00\x00\x00\x00IEND\xaeB`\x82"):
            raise ValueError("Invalid PNG header or missing end marker")
        width, height = struct.unpack(">II", content[16:24])
        mime = "image/png"
    else:
        if not content.startswith(b"\xff\xd8") or not content.endswith(b"\xff\xd9"):
            raise ValueError("Invalid JPEG header or missing end marker")
        position = 2
        while position < len(content) - 1:
            if content[position] != 0xFF:
                break
            while position < len(content) and content[position] == 0xFF:
                position += 1
            if position >= len(content):
                break
            marker = content[position]
            position += 1
            if marker in {0xDA, 0xD9}:  # Scan data follows the size-bearing frame.
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:
                continue
            if position + 2 > len(content):
                break
            length = int.from_bytes(content[position:position + 2], "big")
            if length < 2 or position + length > len(content):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                if length < 8:
                    break
                height, width = struct.unpack(">HH", content[position + 3:position + 7])
                break
            position += length
        mime = "image/jpeg"
    if not (0 < width <= 20000 and 0 < height <= 20000 and width * height <= 80000000):
        raise ValueError("Image dimensions are missing or exceed 20,000 px / 80 megapixels")
    return mime, width, height


def init_proposal(template: dict | None = None) -> dict:
    """Return a fillable client draft without demo facts, scores or money."""
    template = template or load_template()
    sections = []
    for definition in template["sections"]:
        record = {
            "id": definition["id"], "takeaway": "", "tables": [],
            "action": "", "source": "", "source_url": None, "visual": None,
            "intro": "", "analysis_links": [], "details": [], "visual_explanation": "",
        }
        if definition.get("visual_required"):
            record["visual_unavailable_reason"] = ""
        if definition["kind"] == "analysis":
            record["state"] = {"value": None, "reason": ""}
            record["opportunity"] = {"value": None, "reason": ""}
        for table in definition["tables"]:
            if table.get("binding") == "analysis_scores":
                rows = [
                    [s["title"], {"value": None, "reason": ""}, {"value": None, "reason": ""}]
                    for s in template["sections"] if s["kind"] == "analysis"
                ]
            else:
                rows = [[
                    {"value": None, "reason": ""} if kind in {"number", "integer"} else ""
                    for kind in table["column_types"]
                ]]
            record["tables"].append({"id": table["id"], "rows": rows})
        sections.append(record)
    return {
        "schema_version": SCHEMA_VERSION, "mode": "client",
        "client": {"name": "", "domain": "", "date": ""}, "sections": sections,
        "research_handoff": {"version": EVIDENCE_VERSION, "sources": [], "sections": []},
    }


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _number(value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _url(value) -> bool:
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            and not parsed.username and not parsed.password
        )
    except ValueError:
        return False


def _demo_url(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        hostname = urlsplit(value if "://" in value else "https://" + value).hostname or ""
        return any(hostname == base or hostname.endswith("." + base) for base in ("example.org", "example.com", "example.net"))
    except ValueError:
        return False


def validate(proposal, template: dict | None = None, *, client_ready: bool = False) -> list[dict]:
    """Return precise errors; an empty list means structural validity only.

    ``client_ready`` additionally rejects demo/synthetic content. The host must
    still inspect evidence and the latest rendered document before delivery.
    """
    template = template or load_template()
    errors = []

    def error(section, field, message):
        errors.append({"section": section, "field": field, "message": message})

    def unknown(value, section, field):
        if not isinstance(value, dict):
            return False
        if set(value) != {"value", "reason"} or value.get("value") is not None:
            error(section, field, "Unknown value must be {value: null, reason: nonempty text}")
        elif not _text(value.get("reason")):
            error(section, field + ".reason", "Explain why the value is unknown")
        else:
            prose(value["reason"], section, field + ".reason")
        return True

    def score(value, section, field):
        if not isinstance(value, dict) or set(value) != {"value", "reason"}:
            error(section, field, "Score must contain exactly value and reason")
            return
        number = value["value"]
        if number is not None and (not _number(number) or not 0 <= number <= 10):
            error(section, field + ".value", "Score must be a finite number from 0 to 10 or null")
        if not _text(value["reason"]):
            error(section, field + ".reason", "A score or its absence needs an explanation")
        else:
            prose(value["reason"], section, field + ".reason")

    def prose(value, section, field, *, required=True, plain=False):
        if not isinstance(value, str) or (required and not value.strip()):
            error(section, field, "Provide a substantive text explanation")
            return
        if re.search(r"\{\{.+?\}\}|\bTODO\b|\[заполнить\]", value, re.I):
            error(section, field, "Unfilled placeholder")
        if re.search(r"(?im)^\s*(?:Следующий шаг|Источники?)\s*[.:]", value):
            error(section, field, "Explain findings in the narrative; do not add legacy source/action footer labels")
        if plain and re.search(r"\*\*|__|^#{1,6}\s|<[^>]+>", value, re.M):
            error(section, field, "The introduction must be ordinary text without headings, bold markup or HTML")

    def links(value, section, field, *, required=False):
        if not isinstance(value, list):
            error(section, field, "Links must be an array of {label, url}")
            return
        if required and not value:
            error(section, field, "Include a direct link to the actual analysis or evidence")
        for index, link in enumerate(value):
            path = f"{field}[{index}]"
            if not isinstance(link, dict) or set(link) != {"label", "url"}:
                error(section, path, "A link must contain exactly label and url")
                continue
            prose(link["label"], section, path + ".label")
            if not _url(link["url"]):
                error(section, path + ".url", "Use a direct HTTP(S) URL without credentials")
            elif client_ready and _demo_url(link["url"]):
                error(section, path + ".url", "Replace the demonstration URL with real evidence")

    def details(value, section, *, required=False):
        if not isinstance(value, list):
            error(section, "details", "Details must be an ordered array")
            return
        types = [block.get("type") for block in value if isinstance(block, dict)]
        if required and ("paragraph" not in types or not any(t in types for t in ("example", "table", "image"))):
            error(section, "details", "Explain what the analysis shows in a paragraph and include a concrete example, evidence table or screenshot")
        table_ids = set()
        fields_by_type = {
            "paragraph": {"type", "text"},
            "example": {"type", "title", "observation", "implication", "action", "links"},
            "table": {"type", "id", "title", "columns", "rows", "interpretation", "links"},
            "image": {"type", "path", "alt", "caption", "interpretation", "links"},
        }
        for index, block in enumerate(value):
            path = f"details[{index}]"
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                error(section, path, "A detail block needs a supported type")
                continue
            kind = block["type"]
            if kind not in fields_by_type:
                error(section, path + ".type", "Use paragraph, example, table or image")
                continue
            if set(block) != fields_by_type[kind]:
                error(section, path, "Block must contain exactly: " + ", ".join(sorted(fields_by_type[kind])))
            if kind == "paragraph":
                prose(block.get("text"), section, path + ".text")
                continue
            links(block.get("links"), section, path + ".links", required=True)
            if kind == "example":
                for field in ("title", "observation", "implication", "action"):
                    prose(block.get(field), section, path + "." + field)
            elif kind == "table":
                for field in ("title", "interpretation"):
                    prose(block.get(field), section, path + "." + field)
                identity = block.get("id")
                if not isinstance(identity, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", identity):
                    error(section, path + ".id", "Use a stable lowercase table ID")
                elif identity in table_ids:
                    error(section, path + ".id", "Detail table IDs must be unique within the section")
                else:
                    table_ids.add(identity)
                columns, rows = block.get("columns"), block.get("rows")
                if not isinstance(columns, list) or not 2 <= len(columns) <= 7 or not all(_text(c) for c in columns):
                    error(section, path + ".columns", "An evidence table needs 2–7 nonempty column labels")
                    continue
                if not isinstance(rows, list) or not rows:
                    error(section, path + ".rows", "An evidence table needs actual findings")
                    continue
                for ri, row in enumerate(rows):
                    if not isinstance(row, list) or len(row) != len(columns):
                        error(section, f"{path}.rows[{ri}]", "Row length must match the evidence columns")
                        continue
                    for ci, cell in enumerate(row):
                        cell_path = f"{path}.rows[{ri}][{ci}]"
                        if unknown(cell, section, cell_path):
                            continue
                        if _text(cell):
                            prose(cell, section, cell_path)
                            if client_ready and _demo_url(cell):
                                error(section, cell_path, "Replace the demonstration evidence URL")
                        elif not _number(cell):
                            error(section, cell_path, "Use text, a finite number or an explicitly explained unknown")
            else:
                for field in ("alt", "caption", "interpretation"):
                    prose(block.get(field), section, path + "." + field)
                try:
                    image_info(block.get("path"))
                except (OSError, ValueError) as exception:
                    error(section, path + ".path", str(exception))

    if not isinstance(proposal, dict):
        error("$", "$", "Proposal must be an object")
        return errors
    for key in set(proposal) - {"schema_version", "mode", "client", "sections", "research_handoff"}:
        error("$", key, "Unsupported proposal field")
    if proposal.get("schema_version") != template["schema_version"]:
        error("$", "schema_version", "Schema must match the selected template")
    if proposal.get("mode") not in {"client", "demo"}:
        error("$", "mode", "Mode must be client or demo")
    if client_ready and proposal.get("mode") != "client":
        error("$", "mode", "A demonstration cannot be released as a client proposal")

    client = proposal.get("client")
    if not isinstance(client, dict):
        error("$", "client", "Client must contain name, domain and date")
    else:
        for field in ("name", "domain", "date"):
            if not _text(client.get(field)):
                error("$", "client." + field, "Fill the client field")
        if _text(client.get("date")):
            try:
                date.fromisoformat(client["date"])
            except ValueError:
                error("$", "client.date", "Use an ISO date YYYY-MM-DD")
        if _text(client.get("domain")):
            domain = client["domain"]
            if not _url(domain if "://" in domain else "https://" + domain):
                error("$", "client.domain", "Use a valid domain or HTTP(S) site URL")
        if client_ready and _demo_url(client.get("domain")):
            error("$", "client.domain", "Replace the reserved demonstration domain")

    records = proposal.get("sections")
    if not isinstance(records, list):
        error("$", "sections", "Sections must be an ordered array")
        return errors
    expected_ids = [s["id"] for s in template["sections"]]
    actual_ids = [s.get("id") if isinstance(s, dict) else None for s in records]
    if actual_ids != expected_ids:
        error("$", "sections", "All fixed sections must occur once in template order")
    by_id = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            error("$", f"sections[{index}]", "Section must be an object with a string id")
            continue
        section_id = record["id"]
        if section_id in by_id:
            error(section_id, "id", "Duplicate section")
        by_id[section_id] = record
        if section_id not in expected_ids:
            error(section_id, "id", "Unknown section")

    errors.extend(validate_handoff(
        proposal, [s["id"] for s in template["sections"] if analytical_section(s) or s["id"] == "forecast"],
        client_ready=client_ready,
    ))
    handoff = proposal.get("research_handoff")
    handoff_reviews = handoff.get("sections", []) if isinstance(handoff, dict) else []
    unavailable = {r.get("section_id") for r in handoff_reviews if isinstance(r, dict) and r.get("status") == "unavailable"} if isinstance(handoff_reviews, list) else set()

    for definition in template["sections"]:
        section_id = definition["id"]
        record = by_id.get(section_id)
        if record is None:
            error(section_id, "id", "Required section is missing")
            continue
        for field in set(record) - SECTION_FIELDS:
            error(section_id, field, "Field is fixed by the template or unsupported")
        for field in definition["required_fields"]:
            if field in {"state", "opportunity"}:
                score(record.get(field), section_id, field)
            elif field in {"analysis_links", "details"}:
                # Checked with their full schemas below, not as strings.
                continue
            elif not _text(record.get(field)):
                error(section_id, field, "Required text is missing")
        if definition["kind"] != "analysis":
            for field in ("state", "opportunity"):
                if field in record:
                    score(record[field], section_id, field)
        for field in ("takeaway", "action", "source", "intro", "visual_explanation"):
            if field in record and not isinstance(record[field], str):
                error(section_id, field, "Text must be a string")
            if isinstance(record.get(field), str) and re.search(r"\{\{.+?\}\}|\bTODO\b|\[заполнить\]", record[field], re.I):
                error(section_id, field, "Unfilled placeholder")
        is_analysis = analytical_section(definition)
        if is_analysis or section_id == "goals" or _text(record.get("intro")):
            prose(record.get("intro"), section_id, "intro", plain=True)
            if is_analysis and isinstance(record.get("intro"), str) and not re.search(r"(?<!\w)мы(?!\w)", record["intro"], re.I):
                error(section_id, "intro", "Describe the analysis from the agency's point of view: «Мы …»")
        if is_analysis or "analysis_links" in record:
            links(record.get("analysis_links"), section_id, "analysis_links", required=is_analysis and section_id not in unavailable)
        if is_analysis or "details" in record:
            details(record.get("details"), section_id, required=is_analysis)
        if _text(record.get("takeaway")):
            prose(record["takeaway"], section_id, "takeaway")
        source = record.get("source", "")
        if client_ready and definition["kind"] != "cover" and not _text(source):
            error(section_id, "source", "Keep the internal basis of the findings: analysis, brief or confirmed conditions")
        if proposal.get("mode") == "demo" and definition["kind"] != "cover":
            if not isinstance(source, str) or not re.search(r"синтет|демонстрац", source, re.I):
                error(section_id, "source", "Demo source must explicitly identify synthetic data")
        if client_ready and isinstance(source, str) and re.search(r"синтет|демонстрационн.*пример", source, re.I):
            error(section_id, "source", "Synthetic demonstration evidence cannot support a client release")
        source_url = record.get("source_url")
        if source_url is not None and not _url(source_url):
            error(section_id, "source_url", "Use an HTTP(S) source URL without credentials or null")
        elif client_ready and _demo_url(source_url):
            error(section_id, "source_url", "Replace the demonstration source link")

        tables = record.get("tables")
        if not isinstance(tables, list):
            error(section_id, "tables", "Tables must be an array, including an empty array on the cover")
            tables = []
        expected_tables = [t["id"] for t in definition["tables"]]
        actual_tables = [t.get("id") if isinstance(t, dict) else None for t in tables]
        if actual_tables != expected_tables:
            error(section_id, "tables", "Fixed tables must occur once in template order")
        table_by_id = {t["id"]: t for t in tables if isinstance(t, dict) and isinstance(t.get("id"), str)}
        for table in definition["tables"]:
            data = table_by_id.get(table["id"])
            path = "tables." + table["id"]
            if data is None:
                continue
            for key in set(data) - {"id", "rows"}:
                error(section_id, path + "." + key, "Table columns and title belong to the template")
            rows = data.get("rows")
            if not isinstance(rows, list):
                error(section_id, path + ".rows", "Rows must be an array")
                continue
            if len(rows) < table.get("min_rows", 1):
                error(section_id, path + ".rows", "Include findings or explicitly explained unknowns")
            for row_index, row in enumerate(rows):
                row_path = f"{path}.rows[{row_index}]"
                if not isinstance(row, list) or len(row) != len(table["columns"]):
                    error(section_id, row_path, "Row length must match the fixed columns")
                    continue
                for col, (value, kind) in enumerate(zip(row, table["column_types"])):
                    field = f"{row_path}[{col}]"
                    if kind == "human":
                        if value is not None and value != "":
                            error(section_id, field, "Hours, rates and amounts must remain blank for the human")
                        continue
                    if kind == "count_or_check":
                        valid_count = isinstance(value, int) and not isinstance(value, bool) and value >= 0
                        valid_check = isinstance(value, str) and value in {"", "✓", "✔"}
                        if not valid_count and not valid_check:
                            error(section_id, field, "Month cells accept a nonnegative integer, a checkmark or empty text")
                        continue
                    if unknown(value, section_id, field):
                        continue
                    if kind in {"number", "integer"}:
                        if not _number(value) or value < 0 or (kind == "integer" and int(value) != value):
                            error(section_id, field, "Use a nonnegative number or an explicit unknown with reason")
                    elif kind == "url":
                        if not _url(value):
                            error(section_id, field, "Use a direct HTTP(S) link or an explained unknown")
                        elif client_ready and _demo_url(value):
                            error(section_id, field, "Replace the demonstration link with real evidence")
                    elif kind == "optional_text":
                        if not isinstance(value, str):
                            error(section_id, field, "Use text, leaving an unknown responsibility empty")
                    elif not _text(value):
                        error(section_id, field, "Text cell must be filled or explicitly unknown")
                    else:
                        prose(value, section_id, field)
                        if client_ready and _demo_url(value):
                            error(section_id, field, "Replace the demonstration evidence URL")
            if table.get("required_first_column"):
                labels = [r[0] for r in rows if isinstance(r, list) and r]
                if any(label not in labels for label in table["required_first_column"]):
                    error(section_id, path, "Every required assessment group must remain visible; explain empty groups explicitly")
            if table.get("binding") == "analysis_scores":
                analyses = [s for s in template["sections"] if s["kind"] == "analysis"]
                if [r[0] if isinstance(r, list) and r else None for r in rows] != [s["title"] for s in analyses]:
                    error(section_id, path, "Summary must contain every analysis in template order")
                for row, analysis in zip(rows, analyses):
                    if not isinstance(row, list) or len(row) != 3:
                        continue
                    source_record = by_id.get(analysis["id"], {})
                    for index, field in ((1, "state"), (2, "opportunity")):
                        source_score = source_record.get(field)
                        if not isinstance(source_score, dict):
                            continue
                        actual = row[index].get("value") if isinstance(row[index], dict) else row[index]
                        if actual != source_score.get("value"):
                            error(section_id, path, f"{analysis['id']} {field} differs from its analytical card")

        visual = record.get("visual")
        visual_reason = record.get("visual_unavailable_reason")
        if visual_reason is not None and not isinstance(visual_reason, str):
            error(section_id, "visual_unavailable_reason", "Explain the unavailable chart in plain text")
        if visual is None:
            if definition.get("visual_required") and not _text(visual_reason):
                error(section_id, "visual", "Required chart is missing; provide data or an explicit visual_unavailable_reason")
            continue
        if _text(visual_reason):
            error(section_id, "visual_unavailable_reason", "A present chart cannot also be marked unavailable")
        if not isinstance(visual, dict):
            error(section_id, "visual", "Visual must be an object or null")
            continue
        prose(record.get("visual_explanation"), section_id, "visual_explanation")
        visual_type = visual.get("type")
        if visual_type == "image":
            if set(visual) != {"type", "path", "alt"}:
                error(section_id, "visual", "Image visual must contain exactly type, path and alt")
            if not _text(visual.get("alt")):
                error(section_id, "visual.alt", "Image needs a meaningful text description")
            try:
                image_info(visual.get("path"))
            except (OSError, ValueError) as exception:
                error(section_id, "visual.path", str(exception))
            continue
        if set(visual) != {"type", "labels", "values", "unit"}:
            error(section_id, "visual", "Visual must contain type, labels, values and unit")
        if visual_type not in {"bars", "line", "steps", "matrix"}:
            error(section_id, "visual.type", "Supported types are bars, line, steps, matrix and image")
        labels, values = visual.get("labels"), visual.get("values")
        if not isinstance(labels, list) or not labels or not all(_text(label) for label in labels):
            error(section_id, "visual.labels", "Visual needs nonempty text labels")
        if not isinstance(values, list):
            error(section_id, "visual.values", "Values must be an array")
        elif visual_type == "steps":
            if values:
                error(section_id, "visual.values", "A process diagram has labels only, without numeric values")
            if isinstance(labels, list) and len(labels) < 2:
                error(section_id, "visual.labels", "A process diagram needs at least two steps")
        else:
            if isinstance(labels, list) and len(values) != len(labels):
                error(section_id, "visual.values", "Provide one numeric value per label")
            if not all(_number(value) and value >= 0 for value in values):
                error(section_id, "visual.values", "Chart values must be finite nonnegative numbers")
            if not _text(visual.get("unit")):
                error(section_id, "visual.unit", "Quantitative visuals need a unit")
            if visual_type == "line" and isinstance(labels, list) and len(labels) < 2:
                error(section_id, "visual.labels", "A line chart needs at least two points")
        if not isinstance(visual.get("unit"), str):
            error(section_id, "visual.unit", "Unit must be text")
    # Compare quantities where the same named work appears in both fixed tables.
    roadmap_tables = by_id.get("roadmap", {}).get("tables", [])
    budget_tables = by_id.get("budget", {}).get("tables", [])
    if isinstance(roadmap_tables, list) and isinstance(budget_tables, list):
        roadmap_rows = next((t.get("rows", []) for t in roadmap_tables if isinstance(t, dict) and t.get("id") == "roadmap"), [])
        budget_rows = next((t.get("rows", []) for t in budget_tables if isinstance(t, dict) and t.get("id") == "estimate"), [])
        if isinstance(roadmap_rows, list) and isinstance(budget_rows, list):
            quantities = {r[0]: r[2] for r in budget_rows if isinstance(r, list) and len(r) == 6 and isinstance(r[0], str)}
            for row in roadmap_rows:
                if not isinstance(row, list) or len(row) != 4 or not isinstance(row[0], str):
                    continue
                numeric = [v for v in row[1:] if isinstance(v, int) and not isinstance(v, bool)]
                if not numeric or any(v not in ("",) and (not isinstance(v, int) or isinstance(v, bool)) for v in row[1:]):
                    continue
                quantity = quantities.get(row[0])
                if _number(quantity) and sum(numeric) != quantity:
                    error("roadmap", "tables.roadmap", f"{row[0]} monthly counts differ from the estimate quantity")
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initial = commands.add_parser("init", help="Write a new empty client draft")
    initial.add_argument("--output", required=True, type=Path)
    initial.add_argument("--template", type=Path)
    checking = commands.add_parser("validate", help="Validate fixed structure and values")
    checking.add_argument("input", type=Path)
    checking.add_argument("--template", type=Path)
    checking.add_argument("--client-ready", action="store_true")
    args = parser.parse_args(argv)
    try:
        template = load_template(args.template)
        if args.command == "init":
            with args.output.open("x", encoding="utf-8") as output:
                output.write(json.dumps(init_proposal(template), ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            print(json.dumps({"path": str(args.output.resolve()), "status": "needs_content"}, ensure_ascii=False))
            return 0
        proposal = json.loads(args.input.read_text(encoding="utf-8-sig"))
        errors = validate(proposal, template, client_ready=args.client_ready)
        print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2, allow_nan=False))
        return int(bool(errors))
    except (OSError, ValueError) as exception:
        print(json.dumps({"valid": False, "errors": [{"section": "$", "field": "$", "message": str(exception)}]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
