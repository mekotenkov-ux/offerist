"""Offline checks for author handoff and client evidence, never a truth certificate.

The manifest stays internal. Renderers consume only proposal sections, not notes,
file paths or author checks. A successful check cannot prove that a person/model
read a file or that a sample supports a conclusion; independent review must do so.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

VERSION = "offerist-evidence-v1"
KINDS = {"original", "input", "derived", "internal_memo"}
STATUSES = {"adequate", "limited", "unavailable"}


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def web_url(value):
    if not nonempty(value) or any(c.isspace() for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def source_identity(url):
    """Recognize file aliases for classification; never change the published URL.

    A Drive preview, Docs editor and open?id link can name the same file. This
    only compares identity, and does not claim that sharing/access is identical.
    """
    if not web_url(url):
        return url
    parsed = urlsplit(url)
    if parsed.hostname in {"drive.google.com", "docs.google.com"}:
        match = re.search(r"/(?:file|document|spreadsheets|presentation)/(?:u/\d+/)?d/([A-Za-z0-9_-]+)(?:/|$)|/folders/([A-Za-z0-9_-]+)(?:/|$)", parsed.path)
        file_id = next((p for p in match.groups() if p), None) if match else None
        if not file_id and parsed.path.rstrip("/") in {"/open", "/uc", "/thumbnail"}:
            file_ids = parse_qs(parsed.query).get("id", [])
            if len(file_ids) == 1 and re.fullmatch(r"[A-Za-z0-9_-]+", file_ids[0]):
                file_id = file_ids[0]
        if file_id:
            return "gdrive:" + file_id
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def urls(value):
    """Include URLs in prose, table cells, image evidence and explicit links."""
    if isinstance(value, dict):
        for item in value.values():
            yield from urls(item)
    elif isinstance(value, list):
        for item in value:
            yield from urls(item)
    elif isinstance(value, str):
        if web_url(value):
            yield value
        else:
            yield from re.findall(r'https?://[^\s<>"\]]+', value)


def validate_handoff(proposal, required_sections, *, client_ready=False):
    """Validate evidence identity, lineage, review coverage and publication links.

    Legacy drafts may omit the manifest; client releases may not. The scope is
    the exact exported corpus (region, engine, dates, pagination, filters, limits),
    never an assertion that a bounded API export represents an entire service.
    """
    errors = []

    def fail(section, field, message):
        errors.append({"section": section, "field": field, "message": message})

    handoff = proposal.get("research_handoff")
    if handoff is None:
        if client_ready:
            fail("$", "research_handoff", "Client release requires original reports, internal memos and author source checks")
        return errors
    if not isinstance(handoff, dict) or handoff.get("version") != VERSION:
        fail("$", "research_handoff", "Use research_handoff version " + VERSION)
        return errors
    sources = handoff.get("sources")
    reviews = handoff.get("sections")
    if not isinstance(sources, list) or not isinstance(reviews, list):
        fail("$", "research_handoff", "sources and sections must be arrays")
        return errors
    by_id, by_url = {}, {}
    for index, source in enumerate(sources):
        field = f"research_handoff.sources[{index}]"
        if not isinstance(source, dict) or not nonempty(source.get("id")):
            fail("$", field, "Source needs a stable nonempty id")
            continue
        sid = source["id"]
        if sid in by_id:
            fail("$", field, "Duplicate source id")
        by_id[sid] = source
        if source.get("kind") not in KINDS:
            fail("$", field + ".kind", "Use original, input, derived or internal_memo")
        if not isinstance(source.get("client_safe"), bool):
            fail("$", field + ".client_safe", "Explicitly classify client publication safety")
        if not nonempty(source.get("scope")):
            fail("$", field + ".scope", "Describe the exact corpus, dates, filters and export limits")
        if source.get("completeness") not in {"full", "bounded"}:
            fail("$", field + ".completeness", "Declare full or bounded export; explain bounds in scope")
        local_path, url = source.get("local_path"), source.get("url")
        if not nonempty(local_path) and not web_url(url):
            fail("$", field, "Provide a local report path or direct source URL")
        if nonempty(local_path):
            path = Path(local_path)
            if not path.is_absolute() or not path.is_file():
                fail("$", field + ".local_path", "Local source must be an existing absolute file")
            else:
                digest = source.get("sha256")
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    fail("$", field + ".sha256", "Record the SHA-256 of the source actually reviewed")
                elif file_hash(path) != digest:
                    fail("$", field + ".sha256", "Source changed after author review; inspect it again")
        if url is not None:
            if not web_url(url):
                fail("$", field + ".url", "Use a direct HTTP(S) URL without credentials or null")
            else:
                identity = source_identity(url)
                if identity in by_url:
                    fail("$", field + ".url", "One URL cannot masquerade as two evidence identities")
                by_url[identity] = source
        if source.get("kind") == "internal_memo" and source.get("client_safe") is not False:
            fail("$", field + ".client_safe", "Internal memos are never client materials")

    def primary_ancestors(sid, visiting=None):
        visiting = set() if visiting is None else visiting
        if sid not in by_id or sid in visiting:
            return set()
        source = by_id[sid]
        if source.get("kind") in {"original", "input"}:
            return {sid}
        if source.get("kind") != "derived":
            return set()
        parents = source.get("parent_ids", [])
        if not isinstance(parents, list) or not all(isinstance(p, str) for p in parents):
            return set()
        return set().union(*(primary_ancestors(p, visiting | {sid}) for p in parents))

    for sid, source in by_id.items():
        if source.get("kind") != "derived":
            continue
        parents = source.get("parent_ids")
        if (not isinstance(parents, list) or not parents
                or any(not isinstance(p, str) or p not in by_id or by_id[p].get("kind") == "internal_memo" for p in parents)
                or not primary_ancestors(sid)):
            fail("$", "research_handoff.sources." + sid, "Derived data needs traceable original/input parent_ids without cycles or missing parents")

    def has_cycle(sid, visiting):
        if sid in visiting:
            return True
        source = by_id.get(sid, {})
        if source.get("kind") != "derived":
            return False
        parents = source.get("parent_ids", [])
        return isinstance(parents, list) and any(isinstance(p, str) and has_cycle(p, visiting | {sid}) for p in parents)

    for sid in by_id:
        if has_cycle(sid, set()):
            fail("$", "research_handoff.sources." + sid, "Evidence lineage contains a cycle")

    section_records = {s.get("id"): s for s in proposal.get("sections", []) if isinstance(s, dict)}
    reviewed_sections = set()
    for index, review in enumerate(reviews):
        field = f"research_handoff.sections[{index}]"
        if not isinstance(review, dict) or not isinstance(review.get("section_id"), str) or review.get("section_id") not in section_records:
            fail("$", field, "Review must identify an existing proposal section")
            continue
        section = review["section_id"]
        if section in reviewed_sections:
            fail(section, field, "Duplicate section handoff")
        reviewed_sections.add(section)
        if review.get("status") not in STATUSES or not nonempty(review.get("reason")):
            fail(section, field, "Record adequate, limited or unavailable and a substantive sufficiency reason")
        source_ids, memo_ids, checks = review.get("source_ids"), review.get("memo_ids"), review.get("author_checks")
        if not isinstance(source_ids, list) or not all(isinstance(s, str) for s in source_ids):
            fail(section, field + ".source_ids", "source_ids must list original/input/derived evidence")
            source_ids = []
        if not isinstance(memo_ids, list) or not memo_ids or any(not isinstance(s, str) or by_id.get(s, {}).get("kind") != "internal_memo" for s in memo_ids):
            fail(section, field + ".memo_ids", "Supply the internal specialist memo separately from source reports")
        required_checks = set()
        for sid in source_ids:
            if sid not in by_id or by_id[sid].get("kind") == "internal_memo":
                fail(section, field + ".source_ids", "Evidence cannot be missing or replaced by an internal memo")
            required_checks.add(sid)
            required_checks.update(primary_ancestors(sid))
        if review.get("status") != "unavailable" and not any(primary_ancestors(s) for s in source_ids):
            fail(section, field + ".source_ids", "Adequate/limited research requires primary evidence, not a memo alone")
        checked = set()
        if not isinstance(checks, list):
            fail(section, field + ".author_checks", "Author must record source inspection, not only receipt of a memo")
            checks = []
        for check in checks:
            if not isinstance(check, dict) or check.get("source_id") not in by_id or any(not nonempty(check.get(k)) for k in ("locator", "observation", "decision")):
                fail(section, field + ".author_checks", "Each check needs source_id, exact locator, observation and author decision")
            else:
                checked.add(check["source_id"])
        if not required_checks <= checked:
            fail(section, field + ".author_checks", "Author checks must cover every used report and its primary parents")
        record = section_records[section]
        if client_ready:
            public_urls = {source_identity(url) for url in urls(record.get("analysis_links", []))}
            for url in public_urls:
                source = by_url.get(url, {})
                if source.get("kind") not in {"original", "input", "derived"} or source.get("client_safe") is not True:
                    fail(section, "analysis_links", "Every analysis link must resolve to a registered client-safe full report, not a memo or unclassified link")
            for sid in required_checks if review.get("status") != "unavailable" else []:
                source = by_id.get(sid, {})
                if source.get("kind") in {"original", "input"}:
                    public_source = source
                    if source.get("client_safe") is not True:
                        public_source = by_id.get(source.get("client_copy_id"), {})
                        if (public_source.get("kind") != "derived" or sid not in primary_ancestors(public_source.get("id"))
                                or not nonempty(public_source.get("redaction_note")) or public_source.get("id") not in checked):
                            fail(section, "analysis_links", "Private primary evidence needs a reviewed full client copy with explicit redactions and lineage")
                    if public_source.get("client_safe") is not True or source_identity(public_source.get("url")) not in public_urls:
                        fail(section, "analysis_links", "Link each complete, client-safe primary report directly; an archive or specialist memo is insufficient")
        if section == "forecast" and review.get("status") == "unavailable":
            baseline_rows = review.get("baseline_rows", {})
            if not isinstance(baseline_rows, dict):
                fail(section, "baseline_rows", "Map table IDs to zero-based measured baseline row indices")
                baseline_rows = {}
            tables = {t.get("id"): t for t in record.get("tables", []) if isinstance(t, dict)}
            for table_id, indices in baseline_rows.items():
                if (table_id not in tables or not isinstance(indices, list)
                        or any(not isinstance(i, int) or isinstance(i, bool) or i < 0 or i >= len(tables[table_id].get("rows", [])) for i in indices)):
                    fail(section, "baseline_rows", "Measured baseline indices must identify existing table rows")
            visual_role = review.get("visual_role")
            if visual_role not in {None, "measured_baseline"}:
                fail(section, "visual_role", "Only measured_baseline can accompany an unavailable forecast")
            if (baseline_rows or visual_role == "measured_baseline") and (not any(primary_ancestors(s) for s in source_ids) or not required_checks <= checked):
                fail(section, "baseline_rows", "Measured baseline needs primary evidence and direct author checks; its label alone is insufficient")
            visual = record.get("visual")
            if isinstance(visual, dict) and visual.get("type") != "steps" and visual_role != "measured_baseline":
                fail(section, "visual", "Unavailable forecast cannot contain a quantitative forecast chart")
            for table in record.get("tables", []):
                marked = baseline_rows.get(table.get("id"), [])
                for row_index, row in enumerate(table.get("rows", [])):
                    if row_index not in (marked if isinstance(marked, list) else []) and any(isinstance(cell, (int, float)) and not isinstance(cell, bool) for cell in row):
                        fail(section, "tables", "Unavailable forecast must use explained unknowns, not arbitrary scenario numbers")

    if client_ready:
        for section in set(required_sections) - reviewed_sections:
            fail(section, "research_handoff", "Missing source handoff and author sufficiency assessment")
        for section, record in section_records.items():
            # These two legacy provenance fields are internal and deliberately
            # absent from both renderers; all rendered fields are still scanned.
            rendered = {key: value for key, value in record.items() if key not in {"source", "source_url"}}
            for url in urls(rendered):
                source = by_url.get(source_identity(url))
                if source is None:
                    # Prose punctuation may follow an otherwise registered URL.
                    # Compare only a second candidate; preserve the actual URL,
                    # including any meaningful terminal path characters.
                    source = by_url.get(source_identity(url.rstrip(".,;:!?)}")))
                if source and (source.get("kind") == "internal_memo" or source.get("client_safe") is not True):
                    fail(section, "client_source", "Client content must not expose an internal memo or unsafe source")
    return errors
