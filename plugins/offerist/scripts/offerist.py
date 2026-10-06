"""Offerist: local memory, arithmetic and review receipts. No network or LLM calls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import shutil
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
        raise ValueError("Use a lower-case ID with letters, digits, hyphens or underscores (1–64).")
    if value in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}:
        raise ValueError("Reserved filesystem ID")
    return value


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, data, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if exclusive:
        with path.open("x", encoding="utf-8") as out:
            out.write(body)
    else:
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(path)
    return data


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inside(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Path must be inside the selected folder")
    return path


def agency(store, key):
    return inside(Path(store) / "agencies", identifier(key))


def runpath(store, key, run):
    return inside(agency(store, key) / "runs", identifier(run))


def init_profile(store, key, name):
    path = agency(store, key) / "profile.json"
    return write(path, {"schema_version": 1, "agency_id": key, "name": name,
        "created_at": now(), "criteria": [{"id": "action-plan", "text": "Понятно, что подрядчик улучшит, какие изменения сделает, как они связаны с показателями, объёмом, сроком и проверкой. Цели и обязанности клиента не выдуманы."}], "kpi_policy": [], "guarantees": [],
        "pricing": [], "case_studies": [], "example_cards": [], "workflow": {}, "tools": [],
        "format": {"default": "text", "template": "standard-b2b-v4", "custom_format_training": False, "presentation_on_request": True},
        "references": [], "training_status": "needs_agency_facts; standard_format_ready"}, exclusive=True)


def init_run(store, key, run, site, geo, project=None):
    profile = read(agency(store, key) / "profile.json")
    if geo not in {"yes", "no", "undecided"}:
        raise ValueError("geo must be yes, no or undecided")
    if urlsplit(site).scheme not in {"http", "https"} or not urlsplit(site).hostname:
        raise ValueError("site must be an absolute HTTP(S) URL")
    root = runpath(store, key, run)
    root.mkdir(parents=True, exist_ok=False)
    write(root / "run.json", {"schema_version": 2, "agency_id": key, "run_id": run,
        "project_id": project or urlsplit(site).hostname.lower(), "site": site,
        "geo": geo, "geo_user_quote": "", "created_at": now(), "seller_id": "",
        "criteria": profile.get("criteria", []), "quality_checks": {}, "exceptions": [],
        "commercial_status": "estimate_pending_human", "status": "draft"})
    write(root / "evidence.json", [])
    write(root / "scope.json", {"agency_id": key, "run_id": run, "site": site,
        "geo": geo, "geo_user_quote": "", "exceptions": []})
    (root / "brief.md").write_text("", encoding="utf-8")
    (root / "proposal.md").write_text("", encoding="utf-8")
    return {"run_path": str(root.resolve()), "status": "draft"}


def events(root):
    return [read(p) for p in sorted((root / "feedback").glob("*.json"))]


def feedback(store, key, item):
    root = agency(store, key)
    read(root / "profile.json")
    for field in ("user_quote", "rule", "scope", "status", "check"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            raise ValueError(f"Feedback requires nonempty {field}")
    if item["scope"] not in {"agency", "project", "run"}:
        raise ValueError("scope must be agency, project or run")
    if item["status"] not in {"active", "proposed"}:
        raise ValueError("status must be active or proposed")
    if item["scope"] == "project" and not item.get("project_id"):
        raise ValueError("project rule needs project_id")
    if item["scope"] == "run":
        read(runpath(store, key, item.get("run_id", "")) / "run.json")
    event = {k: item[k] for k in ("user_quote", "rule", "scope", "status", "check", "project_id", "run_id", "source", "before", "after", "proposal_sha256", "proposal_version", "author", "location", "resolution", "recheck_result") if k in item}
    event.update(id="rule-" + uuid.uuid4().hex[:12], agency_id=key, recorded_at=now(), type="rule")
    path = root / "feedback" / (event["id"] + ".json")
    write(path, event, exclusive=True)
    return {"saved": read(path), "path": str(path.resolve()), "sha256": digest(path)}


def retire_rule(store, key, rule_id, reason):
    root = agency(store, key)
    if not reason.strip():
        raise ValueError("Retirement needs a reason")
    if rule_id not in {e["id"] for e in events(root) if e["type"] == "rule"}:
        raise ValueError("Unknown rule in this agency")
    event = {"id": "retire-" + uuid.uuid4().hex[:12], "type": "retire", "rule_id": rule_id, "reason": reason, "recorded_at": now()}
    return write(root / "feedback" / (event["id"] + ".json"), event, exclusive=True)


def context(store, key, run=None):
    root = agency(store, key)
    profile = read(root / "profile.json")
    current = read(runpath(store, key, run) / "run.json") if run else {}
    history = events(root)
    retired = {e["rule_id"] for e in history if e["type"] == "retire"}
    def applies(e):
        return (e["scope"] == "agency" or
            (run and e["scope"] == "run" and e.get("run_id") == run) or
            (run and e["scope"] == "project" and e.get("project_id") == current.get("project_id")))
    rules = [e for e in history if e["type"] == "rule" and e["id"] not in retired and applies(e)]
    # Feedback files are the audit record; context carries only reusable rule data.
    rule_fields = ("id", "agency_id", "project_id", "run_id", "scope", "status", "rule", "check", "recorded_at")
    visible_rules = [{field: e[field] for field in rule_fields if field in e} for e in rules]
    # The profile only lists card paths. Expose a small, approved index here,
    # never the card body, private source reference, or asset content.
    example_index = []
    cards_root = root / "examples"
    for relative in profile.get("example_cards", []):
        card_path = inside(cards_root, relative)
        card = read(card_path)
        if not (card.get("permission_to_show") is True and card.get("client_visible") is True
                and card.get("status") == "approved"
                and card.get("claim_type") == "illustrative_deliverable"
                and card.get("redaction") in {"done", "not_needed"}):
            continue
        scope = card.get("scope")
        if scope == "agency":
            pass
        elif scope == "project" and run and card.get("project_id") == current.get("project_id"):
            pass
        else:
            continue
        example_index.append({field: card[field] for field in
            ("id", "type", "title", "goal_relevance", "scope", "project_id") if field in card}
            | {"card_path": str(card_path.resolve())})
    visible_profile = {k: v for k, v in profile.items() if k != "example_cards"}
    return {"profile": visible_profile, "run": current, "examples_index": example_index,
            "active_rules": [e for e in visible_rules if e["status"] == "active"],
            "proposed_rules": [e for e in visible_rules if e["status"] == "proposed"]}


def number(value, field, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{field} must be a finite number >= {minimum}")
    return value


def count(value, field):
    number(value, field)
    if int(value) != value:
        raise ValueError(f"{field} must be an integer")
    return int(value)


def calculate(data):
    result = {}
    if "positions" in data:
        rows = data["positions"]
        denom = sum(number(r["impressions"], "impressions") for r in rows)
        total = sum(number(r["position"], "position", 1) * r["impressions"] for r in rows)
        result["weighted_position"] = total / denom if denom else None
        result["position_impressions"] = denom
    if "pages" in data:
        counts = dict.fromkeys(("keep", "optimize", "rework", "unknown"), 0)
        seen = set()
        for row in data["pages"]:
            u = urlsplit(row["url"])
            if u.scheme not in {"http", "https"} or not u.hostname:
                raise ValueError("Page URL must be absolute HTTP(S)")
            canonical = urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path or "/", u.query, ""))
            if canonical in seen:
                raise ValueError("Duplicate landing URL; resolve mapping before calculating")
            seen.add(canonical)
            if row["action"] not in counts:
                raise ValueError("Existing page action must be keep/optimize/rework/unknown")
            counts[row["action"]] += 1
        n = len(seen)
        result["existing_pages"] = {"denominator": n, "counts": counts,
            "shares": {k: v / n if n else None for k, v in counts.items()}}
    if "missing_clusters" in data:
        ids = [r["id"] for r in data["missing_clusters"]]
        if any(not isinstance(x, str) or not x.strip() for x in ids) or len(set(ids)) != len(ids):
            raise ValueError("Missing clusters need distinct nonempty IDs")
        result["create_candidates"] = len(ids)
    if "page_counts" in data:
        rows = data["page_counts"]
        fields = ("definition", "source", "period", "engine", "region", "device", "segment")
        keys = [tuple(r.get(f) for f in fields) for r in rows]
        if any(any(not isinstance(x, str) or not x.strip() for x in k) for k in keys) or len(set(keys)) > 1:
            raise ValueError("Page counts are not comparable across definition/source/period/engine/region/device/segment")
        domains = [r["domain"] for r in rows]
        if len(set(domains)) != len(domains):
            raise ValueError("Duplicate domains in page-count cohort")
        base = count(rows[0]["count"], "page count") if rows else None
        result["page_deltas"] = [{"domain": r["domain"], "count": count(r["count"], "page count"),
            "absolute": r["count"] - base, "relative": (r["count"] - base) / base if base else None} for r in rows]
    if "continuation" in data:
        c = data["continuation"]
        if c.get("same_cohort") is not True or not c.get("definition", "").strip():
            raise ValueError("Continuation requires an explicit definition and the same cohort")
        all_visits = count(c["eligible_visits"], "eligible_visits")
        continued = count(c["continued_visits"], "continued_visits")
        if continued > all_visits:
            raise ValueError("Continued visits exceed eligible visits")
        result["continuation_rate"] = continued / all_visits if all_visits else None
        result["continuation_definition"] = c["definition"]
    return result


def screenshot_source(root, evidence):
    """Check format signature and receipt; authenticity still needs visual review."""
    source = inside(root, evidence.get("path") or "")
    if not source.is_file():
        raise ValueError("Screenshot file missing or changed")
    with source.open("rb") as image:
        header = image.read(12)
    suffix = source.suffix.lower()
    matches = {
        ".png": header.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": header.startswith(b"\xff\xd8\xff"),
        ".jpeg": header.startswith(b"\xff\xd8\xff"),
        ".webp": header[:4] == b"RIFF" and header[8:12] == b"WEBP",
    }
    if not matches.get(suffix, False):
        raise ValueError("Screenshot format does not match file extension")
    if evidence.get("sha256") != digest(source):
        raise ValueError("Screenshot file missing or changed")
    return source


def chart_source(root, evidence):
    for field in ("source", "captured_at", "caption"):
        value = evidence.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Chart requires {field}")
    source = inside(root, evidence.get("path") or "")
    if source.suffix.lower() != ".png":
        raise ValueError("Unsupported chart file")
    if not source.is_file() or source.stat().st_size <= 8:
        raise ValueError("Chart file missing or changed")
    with source.open("rb") as image:
        png_signature = image.read(8)
    if png_signature != b"\x89PNG\r\n\x1a\n" or evidence.get("sha256") != digest(source):
        raise ValueError("Chart file missing or changed")
    return source


def packet(store, key, run):
    root = runpath(store, key, run)
    out = root / "client-packet"
    # A unique revision prevents stale attachments from surviving a newer packet.
    revision = out / uuid.uuid4().hex[:12]
    revision.mkdir(parents=True)
    for name in ("brief.md", "proposal.md"):
        if not (root / name).read_text(encoding="utf-8").strip():
            raise ValueError(f"Empty {name}")
        shutil.copy2(root / name, revision / name)
    # Client-readable maps and their source-bound index travel with this exact
    # revision. Legacy runs may omit them; a complete workflow proposal cannot.
    for name in ("visible-appendix.md", "client-handoff.json"):
        source = root / name
        if source.is_file():
            if not source.read_text(encoding="utf-8-sig").strip():
                raise ValueError(f"Empty {name}")
            shutil.copy2(source, revision / name)
    handoff_path = root / "client-handoff.json"
    if handoff_path.is_file():
        handoff = read(handoff_path)
        supporting = handoff.get("evidence_files", {})
        if not isinstance(supporting, dict):
            raise ValueError("Client evidence_files must map paths to SHA-256")
        for ref, expected in supporting.items():
            if (not isinstance(ref, str) or not ref.startswith("client-evidence/")
                    or ".." in Path(ref).parts or not isinstance(expected, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", expected)):
                raise ValueError("Client supporting evidence needs a client-evidence path and SHA-256")
            source = inside(root, ref)
            if not source.is_file() or digest(source) != expected:
                raise ValueError("Client supporting evidence is missing or changed")
        refs = {row.get("source_table_ref") for row in handoff.get("material_claims", []) if row.get("source_table_ref")}
        for table_ref in list(refs):
            if not isinstance(table_ref, str) or not table_ref.startswith("client-evidence/") or ".." in Path(table_ref).parts:
                raise ValueError("Client numeric table path must stay under client-evidence/")
            table_path = inside(root, table_ref)
            if table_path.is_file():
                table = read(table_path)
                refs.update(row.get("origin", {}).get("packet_ref") for row in table.get("rows", [])
                            if isinstance(row, dict) and isinstance(row.get("origin"), dict)
                            and row["origin"].get("status") == "available" and row["origin"].get("packet_ref"))
        refs.update(row.get("chart_ref") for row in handoff.get("visuals", []) if row.get("chart_ref"))
        refs.update(row.get("report_ref") or row.get("evidence_ref") for row in handoff.get("bar_summary", [])
                    if row.get("report_ref") or row.get("evidence_ref"))
        refs.update(row.get("passage_ref") for row in handoff.get("bar_summary", []) if row.get("passage_ref"))
        refs.update(row.get("packet_ref") for row in handoff.get("material_proofs", [])
                    if row.get("status") == "shown" and row.get("packet_ref"))
        refs.update(row.get("packet_ref") for row in handoff.get("source_rows", []) if row.get("packet_ref"))
        refs.update(supporting)
        for ref in sorted(refs):
            if not isinstance(ref, str) or not ref.startswith("client-evidence/") or ".." in Path(ref).parts:
                raise ValueError("Client evidence path must stay under client-evidence/")
            source = inside(root, ref)
            if not source.is_file() or not source.read_bytes().strip():
                raise ValueError("Client evidence file is missing or empty")
            target = inside(revision, ref)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if ref in supporting and digest(target) != supporting[ref]:
                raise ValueError("Client supporting evidence changed during copy")
    public = []
    for index, e in enumerate(read(root / "evidence.json")):
        if e.get("client_visible") is not True:
            continue
        if e.get("kind") == "example":
            if e.get("status") != "observed":
                continue
            source = inside(root, e.get("path", ""))
            suffix = source.suffix.lower()
            if suffix not in {".md", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".pdf"}:
                raise ValueError("Unsupported example file")
            if not source.is_file() or e.get("sha256") != digest(source):
                raise ValueError("Example file missing or changed")
            target = revision / "attachments" / (str(index) + suffix)
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(source, target)
            # Do not send the source, card reference, or other private provenance.
            public.append({k: e[k] for k in ("id", "kind", "observation", "caption", "status") if k in e}
                          | {"path": target.relative_to(revision).as_posix(), "sha256": digest(target)})
            continue
        if e.get("kind") == "chart" and e.get("status") == "observed":
            source = chart_source(root, e)
            target = revision / "attachments" / (str(index) + ".png")
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(source, target)
            public.append({k: e[k] for k in ("id", "kind", "source", "captured_at", "scope", "observation", "caption", "status") if k in e}
                          | {"path": target.relative_to(revision).as_posix(), "sha256": digest(target)})
            continue
        item = {k: e[k] for k in ("id", "kind", "source", "captured_at", "scope", "observation", "limitation", "status", "surface") if k in e}
        if e.get("kind") == "screenshot" and e.get("status") == "observed":
            source = screenshot_source(root, e)
            suffix = source.suffix.lower()
            target = revision / "attachments" / (str(index) + suffix)
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(source, target)
            item["path"] = target.relative_to(revision).as_posix()
        public.append(item)
    write(revision / "attachments.json", public)
    manifest = {"files": {p.relative_to(revision).as_posix(): digest(p) for p in sorted(revision.rglob("*")) if p.is_file()},
                "proposal_sha256": digest(root / "proposal.md")}
    write(revision / "manifest.json", manifest)
    receipt = {"path": revision.relative_to(root).as_posix(), "packet_sha256": digest(revision / "manifest.json"), "created_at": now()}
    write(root / "packet-receipt.json", receipt)
    return {**receipt, "absolute_path": str(revision.resolve()), "launch": "Give only this packet to a fresh client reviewer context."}


def workflow_status(root):
    """Use the shipped stage validator; never substitute the seller's summary."""
    spec = importlib.util.spec_from_file_location("offerist_workflow_gate", Path(__file__).with_name("workflow.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.command(argparse.Namespace(action="status", run=str(root)))


def normalized_site(value):
    """Normalize spelling only; do not infer ownership of aliases or protocols."""
    if not isinstance(value, str):
        raise ValueError("Scope site must be an HTTP(S) URL")
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Scope site must be an HTTP(S) URL")
    port = parsed.port
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    if port and (parsed.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
        host += ":" + str(port)
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, parsed.fragment))


def workflow_binding_errors(root, cfg, key, run):
    """Bind a stage receipt to this run, including its actual intake output."""
    errors = []
    expected = {"agency_id": key, "run_id": run, "site": cfg.get("site"),
                "geo": cfg.get("geo"), "geo_user_quote": cfg.get("geo_user_quote", "")}
    state = read(root / "workflow-state.json")
    if cfg.get("agency_id") != key or cfg.get("run_id") != run or state.get("run_id") != run:
        errors.append("Workflow run identity mismatch")
    scope = read(inside(root, state.get("scope_ref", "")))
    attempts = state.get("attempts", {}).get("intake", [])
    sources = [("scope", scope)]
    if attempts:
        reference = attempts[-1].get("outputs", {}).get("scope", {})
        path = inside(root, reference.get("path", ""))
        if digest(path) != reference.get("sha256"):
            errors.append("Workflow intake scope changed")
        sources.append(("intake", read(path)))
    else:
        errors.append("Workflow intake scope missing")
    for label, source in sources:
        for field, wanted in expected.items():
            actual = source.get(field)
            try:
                same = normalized_site(actual) == normalized_site(wanted) if field == "site" else actual == wanted
            except (ValueError, TypeError):
                same = False
            if not same:
                errors.append(f"Workflow {label} {field} mismatch")
    return errors


def workflow_lineage_errors(root, cfg, receipt, review):
    """Require the stage graph and main gate to name the same exact files."""
    errors = []
    try:
        attempts = read(root / "workflow-state.json")["attempts"]
        proposal = attempts["proposal"][-1]
        client = attempts["client_review"][-1]
        if proposal.get("status") != "complete" or client.get("status") != "complete":
            errors.append("Workflow proposal/client review is not complete")
        document_ref = proposal["outputs"]["proposal_document"]
        packet_ref = proposal["outputs"]["client_packet"]
        if document_ref.get("path") != "proposal.md" or document_ref.get("sha256") != digest(root / "proposal.md"):
            errors.append("Workflow proposal differs from canonical proposal.md")
        packet_file = inside(root, packet_ref["path"])
        if digest(packet_file) != packet_ref.get("sha256"):
            errors.append("Workflow client packet register changed")
        stage_packet = read(packet_file)
        manifest_ref = str(Path(receipt["path"]) / "manifest.json").replace("\\", "/")
        if (stage_packet.get("proposal_sha256") != digest(root / "proposal.md")
                or stage_packet.get("packet_sha256") != receipt["packet_sha256"]
                or stage_packet.get("packet_manifest_ref") != manifest_ref
                or stage_packet.get("packet_receipt_ref") != "packet-receipt.json"):
            errors.append("Workflow packet lineage differs from main packet manifest")
        if client["inputs"]["review_receipt"].get("path") != "review.json" or client["inputs"]["review_receipt"].get("sha256") != digest(root / "review.json"):
            errors.append("Workflow review differs from canonical review.json")
        verdict_ref = client["outputs"]["client_verdict"]
        verdict_path = inside(root, verdict_ref["path"])
        if digest(verdict_path) != verdict_ref.get("sha256"):
            errors.append("Workflow client verdict changed")
        stage_verdict = read(verdict_path)
        for field in ("candidate_sha256", "proposal_sha256", "packet_sha256", "content_verdict", "verdict"):
            if stage_verdict.get(field) != review.get(field):
                errors.append("Workflow client verdict " + field + " mismatch")
        if (review.get("candidate_sha256") != receipt["packet_sha256"]
                or review.get("proposal_sha256") != digest(root / "proposal.md")
                or review.get("packet_sha256") != receipt["packet_sha256"]):
            errors.append("Workflow reviewer candidate differs from main proposal/packet")
        if (review.get("producer_id") != cfg.get("seller_id") or not review.get("context_id")
                or review.get("context_id") == review.get("producer_context_id")
                or review.get("reviewer_id") == review.get("producer_id")):
            errors.append("Workflow reviewer/producer lineage mismatch")
        host_ref = client["inputs"]["host_receipt"]
        host_path = inside(root, host_ref["path"])
        if digest(host_path) != host_ref.get("sha256"):
            errors.append("Workflow host receipt changed")
        host = read(host_path)
        if (host.get("candidate_sha256") != receipt["packet_sha256"]
                or host.get("reviewer_id") != review.get("reviewer_id")
                or host.get("producer_id") != review.get("producer_id")
                or host.get("reviewer_context_id") != review.get("context_id")
                or host.get("producer_context_id") != review.get("producer_context_id")):
            errors.append("Workflow host/reviewer lineage mismatch")
    except (KeyError, IndexError, ValueError, TypeError, OSError) as exc:
        errors.append("Incomplete workflow proposal/review lineage: " + str(exc))
    return errors


def check(store, key, run):
    root = runpath(store, key, run)
    cfg = read(root / "run.json")
    errors = []
    warnings = []
    workflow = None
    review = {}
    scope_consistent = False
    lineage_consistent = False
    def require(condition, reason):
        if not condition:
            errors.append(reason)
    stage_run = cfg.get("schema_version", 1) >= 2 or (root / "workflow-state.json").exists()
    if stage_run:
        try:
            workflow = workflow_status(root)
            binding_errors = workflow_binding_errors(root, cfg, key, run)
            errors.extend(binding_errors)
            scope_consistent = not binding_errors
            declared_commercial = cfg.get("commercial_status")
            stage_commercial = workflow.get("commercial_status")
            require(declared_commercial == stage_commercial,
                    "Commercial status mismatch: run and workflow")
            require(workflow.get("content_scope_complete") is True,
                    "Workflow content scope is incomplete; inspect stage status and resume")
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append("Incomplete workflow: " + str(exc))
    require(cfg.get("geo") in {"yes", "no"} and bool(cfg.get("geo_user_quote", "").strip()), "GEO decision from the user is missing")
    for name in ("brief.md", "proposal.md"):
        require((root / name).is_file() and bool((root / name).read_text(encoding="utf-8").strip()), f"Missing {name}")
    criteria = cfg.get("criteria", [])
    require(bool(criteria), "No agreed quality criteria")
    ids = [c.get("id") for c in criteria]
    require(all(isinstance(i, str) and i.strip() for i in ids) and len(set(ids)) == len(ids), "Criteria require distinct IDs")
    for c in criteria:
        status = cfg.get("quality_checks", {}).get(c.get("id"), {})
        require(status.get("status") == "pass" and bool(status.get("reason")), f"Quality criterion not passed: {c.get('id')}")
    evidence = read(root / "evidence.json")
    require(bool(evidence), "No evidence or recorded access limitations")
    surfaces = set()
    exceptions = cfg.get("exceptions", [])
    def excepted(scope):
        return any(e.get("scope") == scope and e.get("user_quote", "").strip() and e.get("reason", "").strip() for e in exceptions)
    evidence_ids = [e.get("id") for e in evidence]
    require(all(isinstance(i, str) and i.strip() for i in evidence_ids) and len(set(evidence_ids)) == len(evidence_ids), "Evidence IDs must be distinct")
    for e in evidence:
        for field in ("source", "captured_at", "scope", "observation"):
            require(bool(e.get(field)), f"Evidence {e.get('id')} lacks {field}")
        require(e.get("status") in {"observed", "unavailable"}, f"Evidence {e.get('id')} has invalid status")
        if e.get("status") == "unavailable":
            warnings.append(f"Unavailable: {e.get('id')}: {e.get('limitation', '')}")
            require(bool(e.get("limitation")), f"Missing limitation: {e.get('id')}")
        if e.get("kind") == "screenshot" and e.get("status") == "observed":
            try:
                screenshot_source(root, e)
                valid = e.get("inspected") is True
            except (ValueError, OSError):
                valid = False
            require(valid, f"Screenshot missing, changed or uninspected: {e.get('id')}")
            if valid:
                surfaces.add(e.get("surface"))
        if e.get("kind") == "example" and e.get("status") == "observed":
            try:
                p = inside(root, e.get("path", ""))
                valid = (p.suffix.lower() in {".md", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".pdf"}
                         and p.is_file() and p.stat().st_size > 0 and e.get("sha256") == digest(p))
            except (ValueError, OSError):
                valid = False
            require(valid, f"Example missing or changed: {e.get('id')}")
        if e.get("kind") == "chart" and e.get("status") == "observed":
            try:
                chart_source(root, e)
                valid = True
            except (ValueError, OSError):
                valid = False
            require(valid, f"Chart missing, changed or incomplete: {e.get('id')}")
    # Analytics are presented from API/export data as charts. Analytics GUI
    # access is not a release condition. GEO still needs real answer captures.
    required_surfaces = {"yandex", "google", "chatgpt"} if cfg.get("geo") == "yes" else set()
    for surface in required_surfaces - surfaces:
        scope = "screenshot:" + surface
        if excepted(scope):
            warnings.append("User accepted missing " + scope)
        else:
            errors.append("Required screenshot missing: " + surface)
    try:
        receipt = read(root / "packet-receipt.json")
        folder = inside(root, receipt["path"])
        manifest = read(folder / "manifest.json")
        require(digest(folder / "manifest.json") == receipt["packet_sha256"], "Packet manifest changed")
        for rel, expected in manifest["files"].items():
            require(digest(inside(folder, rel)) == expected, f"Client packet file changed: {rel}")
        require(digest(root / "brief.md") == manifest["files"]["brief.md"], "Brief changed after review packet")
        proposal_hash = digest(root / "proposal.md")
        require(proposal_hash == manifest["proposal_sha256"], "Proposal changed after review packet")
        review = read(root / "review.json")
        if stage_run:
            lineage_errors = workflow_lineage_errors(root, cfg, receipt, review)
            errors.extend(lineage_errors)
            lineage_consistent = not lineage_errors
        human_estimate = (workflow or {}).get("commercial_status", cfg.get("commercial_status")) == "estimate_pending_human"
        content_verdict = review.get("content_verdict", review.get("verdict"))
        require(content_verdict == "accept", "Client content has not accepted")
        if not human_estimate:
            require(review.get("verdict") == "accept", "Client has not accepted")
        else:
            require(review.get("verdict") != "accept", "Pending human estimate cannot imply purchase acceptance")
            warnings.append("Human estimate pending; content review is separate from purchase readiness")
        require(review.get("context_mode") == "fresh" and bool(cfg.get("seller_id")) and bool(review.get("reviewer_id")) and review.get("reviewer_id") != cfg.get("seller_id"), "Fresh independent reviewer receipt required")
        require(review.get("proposal_sha256") == proposal_hash and review.get("packet_sha256") == receipt["packet_sha256"], "Client review is stale")
        require(isinstance(review.get("issues"), list), "Review issues list required")
        require(not any(i.get("severity") in {"critical", "major"} for i in review.get("issues", [])), "Critical or major client issues remain")
        for c in criteria:
            result = review.get("criteria_results", {}).get(c.get("id"), {})
            require(result.get("status") == "pass" and bool(result.get("reason")), f"Client criterion not passed: {c.get('id')}")
    except (FileNotFoundError, KeyError, ValueError, OSError) as exc:
        errors.append("Incomplete client review packet: " + str(exc))
    if not stage_run:
        warnings.append("Legacy receipt: full stage coverage was not verified")
    return {"status": "draft_with_limits" if errors else "review_receipt_valid", "errors": errors, "limitations": warnings,
            "workflow": workflow, "scope_consistent": scope_consistent, "lineage_consistent": lineage_consistent,
            "content_scope_complete": bool(scope_consistent and lineage_consistent and workflow and workflow.get("content_scope_complete") is True),
            "content_verdict": review.get("content_verdict", review.get("verdict", "unknown")),
            "commercial_status": (workflow or {}).get("commercial_status", cfg.get("commercial_status", "unknown")),
            "purchase_verdict": review.get("verdict", "unknown"), "user_acceptance": "unknown",
            "review_independence_verified": False, "release_ready": False,
            "readiness_blockers": ["Independent review requires trusted host or manual verification"],
            "note": "Structural check only; semantic quality, image authenticity and actual context isolation require agent inspection."}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store", default=".offerist")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("init-profile"); s.add_argument("agency"); s.add_argument("--name", required=True)
    s = sub.add_parser("init-run"); s.add_argument("agency"); s.add_argument("run"); s.add_argument("--site", required=True); s.add_argument("--geo", choices=["yes", "no", "undecided"], default="undecided"); s.add_argument("--project")
    s = sub.add_parser("context"); s.add_argument("agency"); s.add_argument("--run")
    s = sub.add_parser("feedback"); s.add_argument("agency"); s.add_argument("--input", required=True)
    s = sub.add_parser("retire-rule"); s.add_argument("agency"); s.add_argument("rule"); s.add_argument("--reason", required=True)
    for command in ("packet", "check"):
        s = sub.add_parser(command); s.add_argument("agency"); s.add_argument("run")
    s = sub.add_parser("calculate"); s.add_argument("--input", required=True)
    args = p.parse_args(argv)
    try:
        if args.command == "init-profile": result = init_profile(args.store, args.agency, args.name)
        elif args.command == "init-run": result = init_run(args.store, args.agency, args.run, args.site, args.geo, args.project)
        elif args.command == "context": result = context(args.store, args.agency, args.run)
        elif args.command == "feedback": result = feedback(args.store, args.agency, read(args.input))
        elif args.command == "retire-rule": result = retire_rule(args.store, args.agency, args.rule, args.reason)
        elif args.command == "packet": result = packet(args.store, args.agency, args.run)
        elif args.command == "check": result = check(args.store, args.agency, args.run)
        else: result = calculate(read(args.input))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 2 if result.get("status") == "draft_with_limits" else 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
