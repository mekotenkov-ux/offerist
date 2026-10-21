"""Offline, manual, hash-bound workflow ledger for one Offerist run.

This checks artifact integrity and declared coverage. Human reviewers remain
responsible for truth, source completeness, and the quality of conclusions.
No configured command is ever executed.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
from html.parser import HTMLParser
import importlib.util
import inspect
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


CONTRACT = Path(__file__).resolve().parents[1] / "skills/offerist/references/workflow-contract.json"
BAR_MANIFEST = Path(__file__).resolve().parent / "bar/manifest.json"
SITE_GAP = Path(__file__).resolve().parent / "site_gap.py"
BAR_CLI = Path(__file__).resolve().parent / "bar/cli.mjs"
BAR_ADAPTER = Path(__file__).resolve().parent / "bar/adapter.mjs"
BAR_BUNDLE = Path(__file__).resolve().parent / "bar/core.bundle.cjs"
STATE_NAME = "workflow-state.json"
VALID_STATUSES = {"complete", "partial", "blocked", "skipped"}
PARTIAL_OUTPUT = {"intake": "scope", "audit": "audit_findings", "analytics": "analytics_findings",
                  "research": "research_findings", "semantic_types": "semantic_map", "gap": "gap_register",
                  "block_gap": "block_register", "text_bar": "text_register", "commercial": "commercial_findings",
                  "geo": "geo_findings", "strategy": "strategy_map", "implementation": "implementation_plan",
                  "team": "team_needs", "estimate": "estimate_sheet", "company_cases": "company_case_pack",
                  "decision_map": "decision_map", "structure_review": "structure_verdict",
                  "proposal": "client_packet", "client_review": "client_verdict", "release_gate": "release_receipt"}
COUNT_MEASUREMENT_UNITS = {"unique_confirmed_inquiry", "confirmed_order", "commercial_search_clicks",
                           "event_count", "visit_count", "click_count"}
GEO_INDEPENDENT = {"audit", "analytics", "research", "semantic_types", "gap", "block_gap", "text_bar", "commercial", "company_cases"}
GEO_FIELDS = {"geo", "geo_source_ref", "geo_user_quote"}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha_file(path):
    return sha_bytes(path.read_bytes())


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def relative_file(root, name):
    if not name or Path(name).is_absolute():
        raise ValueError("Artifact paths must be run-relative")
    target = (root / name).resolve()
    if target == root or not target.is_relative_to(root) or not target.is_file():
        raise ValueError(f"Missing or outside-run artifact: {name}")
    return target


def validate_planned_route_sources(root, site_input):
    """Resolved route evidence is a saved file, not an unbound logical ID."""
    for route in site_input.get("planned_url_dispositions", []):
        if route["disposition"] != "pending":
            relative_file(root, route["source_ref"])


def audit_resource_target(root, source):
    """Resolve an exact saved audit resource; a logical join key never builds a request URL."""
    if not isinstance(source, dict) or not source.get("resource_id") or not source.get("logical_key"):
        raise ValueError("Audit resource needs resource_id and logical_key")
    kind = source.get("source_kind")
    if kind == "unresolved":
        if not source.get("reason") or source.get("request_url") or source.get("target_url"):
            raise ValueError("Unresolved audit resource needs a reason and no invented URL")
        return {"status": "unresolved", "source_kind": kind, "target_url": None, "diagnosis_scope": None}

    def absolute(url, label):
        if not isinstance(url, str) or not url or url != url.strip():
            raise ValueError(f"{label} needs an exact absolute URL")
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError(f"{label} needs an exact absolute HTTP(S) URL")
        return parsed

    def saved(ref_key, sha_key):
        path = relative_file(root, source.get(ref_key))
        if sha_file(path) != source.get(sha_key):
            raise ValueError("Audit URL source file SHA-256 mismatch")
        return path

    if kind == "html_canonical":
        fetched = absolute(source.get("fetched_url"), "fetched URL")
        html = saved("saved_html_ref", "saved_html_sha256").read_text(encoding="utf-8-sig")

        class CanonicalLinks(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.hrefs = []

            def handle_starttag(self, tag, attrs):
                if tag.lower() == "link":
                    values = dict(attrs)
                    if "canonical" in (values.get("rel") or "").lower().split():
                        self.hrefs.append(values.get("href"))

        parser = CanonicalLinks()
        parser.feed(html)
        if len(parser.hrefs) != 1 or not parser.hrefs[0]:
            return {"status": "unresolved", "source_kind": kind, "target_url": None,
                    "diagnosis_scope": None, "reason": "No single saved HTML canonical href"}
        target = parser.hrefs[0]
        parsed = absolute(target, "saved HTML canonical")
        if parsed.fragment:
            raise ValueError("HTML canonical with fragment needs manual resolution")
        if parsed.hostname != fetched.hostname or parsed.port != fetched.port:
            raise ValueError("Cross-host canonical needs separate approved review; no automatic inspection target")
        return {"status": "resolved", "source_kind": kind, "target_url": target, "diagnosis_scope": "canonical"}
    if kind == "http_final":
        receipt = read_json(saved("http_receipt_ref", "http_receipt_sha256"))
        if not isinstance(receipt, dict) or receipt.get("request_url") != source.get("fetched_url"):
            raise ValueError("HTTP final URL receipt must bind the exact fetched URL")
        fetched = absolute(receipt["request_url"], "fetched URL")
        target = receipt.get("final_url")
        parsed = absolute(target, "saved HTTP final URL")
        if parsed.fragment or parsed.hostname != fetched.hostname or parsed.port != fetched.port:
            raise ValueError("Cross-host or fragment HTTP final URL needs separate review")
        return {"status": "resolved", "source_kind": kind, "target_url": target, "diagnosis_scope": "final_url"}
    if kind in {"query_variant", "fragment_variant"}:
        receipt = read_json(saved("variant_receipt_ref", "variant_receipt_sha256"))
        target = receipt.get("observed_url") if isinstance(receipt, dict) else None
        parsed = absolute(target, "saved URL variant")
        if (kind == "query_variant" and (not parsed.query or parsed.fragment)) or (kind == "fragment_variant" and not parsed.fragment):
            raise ValueError("Query and fragment URL sources must remain distinct")
        return {"status": "resolved", "source_kind": kind, "target_url": target, "diagnosis_scope": kind}
    raise ValueError("Unknown audit URL source_kind")


def pairs(items):
    found = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key or not value or key in found:
            raise ValueError(f"Expected distinct name=run-relative-path: {item}")
        found[key] = value
    return found


def manifest_args(run, manifest_ref):
    root, _ = root_and_state(run)
    data = checked_object(root, manifest_ref, "workflow manifest")
    allowed = {"schema_version", "node", "status", "actor", "inputs", "outputs", "checks", "reason", "blockers"}
    if set(data) != allowed or data.get("schema_version") != 1:
        raise ValueError("Workflow manifest needs schema_version=1 and only supported declaration fields")
    if not isinstance(data["inputs"], dict) or not isinstance(data["outputs"], dict) or not isinstance(data["checks"], list) or not isinstance(data["blockers"], list):
        raise ValueError("Workflow manifest inputs/outputs must be maps; checks/blockers must be lists")
    if any(not isinstance(k, str) or not isinstance(v, str) for group in (data["inputs"], data["outputs"]) for k, v in group.items()):
        raise ValueError("Workflow manifest names and paths must be strings")
    if any(not isinstance(value, str) for value in data["checks"] + data["blockers"]):
        raise ValueError("Workflow manifest checks and blockers must be strings")
    if data["reason"] is not None and not isinstance(data["reason"], str):
        raise ValueError("Workflow manifest reason must be text")
    return argparse.Namespace(run=run, node=data["node"], status=data["status"], actor=data["actor"],
                              input=[f"{k}={v}" for k, v in data["inputs"].items()],
                              output=[f"{k}={v}" for k, v in data["outputs"].items()],
                              check=data["checks"], reason=data["reason"], blocker=data["blockers"],
                              manifest_ref=manifest_ref)


def scaffold_template(node):
    return {"schema_version": 1, "node": node["id"], "status": "", "actor": node["actor"],
            "inputs": {name: "" for name in node["inputs"]},
            "outputs": {name: "" for name in node["outputs"]},
            "checks": [], "reason": "", "blockers": []}


def node_plan(node_id, nodes, scope, states, state):
    node = nodes[node_id]
    parents = required_parents(node, nodes, scope)
    dependency_states = {parent: states[parent]["state"] for parent in parents}
    blocking = [parent for parent in parents if states[parent]["state"] not in {"complete", "partial", "skipped"}]
    attempts = state["attempts"].get(node_id, [])
    last = attempts[-1] if attempts else {}
    return {"node": node_id, "state": states[node_id]["state"], "actor": node["actor"],
            "required_inputs": node["inputs"], "required_outputs": node["outputs"],
            "required_checks": node["checks"], "dependency_states": dependency_states,
            "blocking_dependencies": blocking, "last_blockers": last.get("blockers", []),
            "last_reason": last.get("reason", ""), "current_reason": states[node_id].get("reason", "")}


def contract():
    raw = read_json(CONTRACT)
    nodes = {node["id"]: node for node in raw["nodes"]}
    if len(nodes) != len(raw["nodes"]):
        raise ValueError("Duplicate contract node")
    for node in nodes.values():
        for parent in node["depends_on"]:
            if parent not in nodes:
                raise ValueError(f"Unknown dependency: {parent}")
    order = []
    while len(order) < len(nodes):
        ready = [n for n in nodes if n not in order and all(p in order for p in nodes[n]["depends_on"])]
        if not ready:
            raise ValueError("Contract dependency cycle")
        order.extend(ready)
    return raw, nodes, order


def policy_sha(raw):
    return sha_bytes(canonical({k: v for k, v in raw.items() if k not in {"nodes", "contract_version"}}))


def compute_sha(node_id):
    # Source for a validator is a code fingerprint, never a configured hook.
    relevant = [record, current, stage_content_checks, partial_stage_content_checks,
                pending_item_checks, consumed_source_bindings, typed_evidence_refs]
    if node_id == "audit":
        relevant.extend((audit_resource_target, audit_artifact_refs, audit_stage_checks))
    if node_id in {"semantic_types", "gap", "block_gap", "text_bar"}:
        relevant.extend((coverage_types, partial_coverage_types))
    if node_id in {"semantic_types", "gap", "block_gap"}:
        relevant.extend((source_pages, source_pages_partial))
    if node_id in {"semantic_types", "gap"}:
        relevant.extend((source_clusters, source_clusters_partial))
    if node_id in {"semantic_types", "proposal"}:
        relevant.append(source_query_rows)
    if node_id in {"research", "gap"}:
        relevant.extend((research_rank_rows, saved_rank_source_rows, rank_number))
    if node_id == "research":
        relevant.append(research_source_claims)
    if node_id == "gap":
        relevant.extend((rank_handoff_checks, rank_handoff_scaffold, own_rank_url_identity,
                         own_rank_aliases, raw_link_metadata_checks, raw_link_primary_checks,
                         json_pointer_segments, json_pointer))
    if node_id == "text_bar":
        relevant.append(replay_bar)
        relevant.append(bounded_bar_stream)
        relevant.append(bar_input_url)
        relevant.append(material_task_scope_checks)
        relevant.extend((bar_reading_checks, bar_native_pointer_domain, json_pointer_segments, json_pointer))
    if node_id in {"strategy", "implementation", "team", "estimate"}:
        relevant.append(volume_checks)
        relevant.append(phase_projection)
    if node_id == "strategy":
        relevant.extend((material_sources, material_source_requirements))
    if node_id in {"decision_map", "proposal"}:
        relevant.extend((ledger_projection, bar_reading_projection))
        relevant.extend((measurement_checks, measurement_receipt_checks))
    if node_id == "decision_map":
        relevant.extend((release_contract_context, release_contract_scaffold, release_contract_checks))
    if node_id in {"decision_map", "proposal"}:
        relevant.extend((structure_evidence_refs, structure_export))
    if node_id in {"estimate", "decision_map", "proposal"}:
        relevant.append(estimate_line_checks)
    if node_id == "proposal":
        relevant.extend((client_handoff_checks, client_material_refs, client_evidence_checks, client_type_line, client_query_ids_by_type,
                         client_demand_line, expected_client_demand, client_source_line,
                         expected_client_sources, markdown_tables, visible_identity, client_main_table_checks, client_block_line,
                         client_proof_line, client_work_line, expected_client_proofs,
                         client_measurement_line, client_case_line, client_claim_line,
                         client_numeric_origin_line, json_pointer_segments, pointer_descends,
                         json_pointer, client_bar_line))
    if node_id in {"structure_review", "client_review"}:
        relevant.append(review_binding)
    if node_id == "release_gate":
        relevant.append(readiness)
    sources = {f.__name__: inspect.getsource(f) for f in relevant}
    if node_id == "gap":
        sources["site_gap.py"] = sha_file(SITE_GAP)
    if node_id == "text_bar":
        for path in (BAR_MANIFEST, BAR_CLI, BAR_ADAPTER, BAR_BUNDLE):
            sources[path.name] = sha_file(path)
    return sha_bytes(canonical(sources))


def root_and_state(run):
    root = Path(run).resolve()
    if not root.is_dir():
        raise ValueError("Run folder does not exist")
    state_path = root / STATE_NAME
    if not state_path.is_file():
        raise ValueError("Workflow not initialized")
    return root, read_json(state_path)


def active(node, scope):
    return node.get("when") != "geo=yes" or scope.get("geo") == "yes"


def required_parents(node, nodes, scope):
    return [p for p in node["depends_on"] if active(nodes[p], scope)]


def scope_fingerprint(node_id, scope):
    data = {k: v for k, v in scope.items() if k not in GEO_FIELDS} if node_id in GEO_INDEPENDENT else scope
    return sha_bytes(canonical(data))


def dependency_signature(root, state, parent, child, current_signature):
    if parent != "intake" or child not in GEO_INDEPENDENT:
        return current_signature
    attempt = state["attempts"]["intake"][-1]
    scope = previous_output(root, state, "intake", "scope")
    return sha_bytes(canonical({"inputs": attempt["inputs"],
                                "scope_without_geo": {k: v for k, v in scope.items() if k not in GEO_FIELDS | {"pending_items"}},
                                "node_contract": attempt["node_contract_sha256"],
                                "policy": attempt["shared_policy_sha256"],
                                "compute": attempt["compute_sha256"]}))


def checked_object(root, path, label):
    value = read_json(relative_file(root, path))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def names(value, label, allow_empty=False):
    if not isinstance(value, list) or (not allow_empty and not value) or any(not isinstance(x, str) or not x.strip() for x in value) or len(set(value)) != len(value):
        raise ValueError(f"{label} needs distinct nonempty IDs")
    return set(value)


def evidence_map(value, expected, label):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} must cover its exact type set")
    for entries in value.values():
        names(entries, label + " references")


def source_pages(urls):
    """Classify every crawl row and require an explicit canonical dedup trail."""
    rows = urls.get("pages")
    if not isinstance(rows, list):
        raise ValueError("URL universe needs raw pages")
    by_id, canonical_rows = {}, {}
    for row in rows:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k].strip() for k in ("source_row_id", "url", "type_id", "variant_id")):
            raise ValueError("Every raw URL row needs source_row_id, URL, type and variant")
        if row["type_id"].lower() == "unknown" or row["variant_id"].lower() == "unknown":
            raise ValueError("Unknown raw URL type or variant cannot complete")
        row_id = row["source_row_id"]
        if row_id in by_id:
            raise ValueError("Duplicate source_row_id")
        canonical_url = row.get("canonical_url") or row["url"]
        if not isinstance(canonical_url, str) or not canonical_url.strip():
            raise ValueError("Raw URL row needs canonical_url")
        if canonical_url in canonical_rows:
            original = canonical_rows[canonical_url]
            if row.get("duplicate_of") != original["source_row_id"] or not row.get("dedup_reason") or (row["type_id"], row["variant_id"]) != (original["type_id"], original["variant_id"]):
                raise ValueError("Duplicate canonical URL needs explicit, consistent dedup source row")
        else:
            if row.get("duplicate_of"):
                raise ValueError("duplicate_of points to absent canonical URL")
            canonical_rows[canonical_url] = row
        by_id[row_id] = row
    return {url: (row["type_id"], row["variant_id"]) for url, row in canonical_rows.items()}


def source_pages_partial(urls, pending_items):
    """Return classified canonical rows and preserve addressed unknown rows in the denominator."""
    rows = urls.get("pages")
    if not isinstance(rows, list):
        raise ValueError("Partial URL universe needs raw pages")
    pending = {item["scope_id"] for item in pending_items if item["scope_kind"] in {"url", "source"} and item["counted_in"] == "urls"}
    by_id, canonical_rows = set(), {}
    for row in rows:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k].strip() for k in ("source_row_id", "url")):
            raise ValueError("Partial raw URL row needs source row ID and URL")
        if row["source_row_id"] in by_id:
            raise ValueError("Duplicate partial source_row_id")
        by_id.add(row["source_row_id"])
        url = row.get("canonical_url") or row["url"]
        if url in canonical_rows:
            original = canonical_rows[url]
            if (row.get("duplicate_of") != original["source_row_id"] or not row.get("dedup_reason")
                    or (row.get("type_id"), row.get("variant_id")) != (original.get("type_id"), original.get("variant_id"))):
                raise ValueError("Partial duplicate URL needs explicit canonical dedup trail")
            continue
        if row.get("duplicate_of"):
            raise ValueError("Partial duplicate_of points to absent canonical URL")
        canonical_rows[url] = row
    known, unknown = {}, set()
    for url, row in canonical_rows.items():
        typ, variant = row.get("type_id"), row.get("variant_id")
        if not typ or not variant or str(typ).lower() == "unknown" or str(variant).lower() == "unknown":
            if url not in pending and row["source_row_id"] not in pending:
                raise ValueError("Unknown raw URL needs addressed pending item counted in URLs")
            unknown.add(url)
        else:
            known[url] = (typ, variant)
    return known, len(canonical_rows), unknown


def source_clusters(demand):
    rows = demand.get("clusters")
    if not isinstance(rows, list):
        raise ValueError("Demand universe needs raw clusters")
    found = set()
    for row in rows:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k].strip() for k in ("id", "type_id", "status")) or row["id"] in found:
            raise ValueError("Every raw demand cluster needs unique ID, type and status")
        if row["type_id"].lower() == "unknown" or row["status"] not in {"covered", "create_candidate", "rejected", "needs_business_answer"}:
            raise ValueError("Unknown demand cluster type/status cannot complete")
        found.add(row["id"])
    return rows


def source_clusters_partial(demand, pending_items):
    rows = demand.get("clusters")
    if not isinstance(rows, list):
        raise ValueError("Partial demand universe needs raw cluster list")
    pending = {item["scope_id"] for item in pending_items if item["scope_kind"] in {"source", "type"} and item["counted_in"] in {"types", "sources"}}
    seen, known = set(), []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in seen:
            raise ValueError("Partial demand cluster needs unique source ID")
        seen.add(row["id"])
        if not row.get("type_id") or str(row["type_id"]).lower() == "unknown" or row.get("status") in {None, "unknown"}:
            if row["id"] not in pending:
                raise ValueError("Unknown demand cluster needs addressed pending item")
        elif row["status"] not in {"covered", "create_candidate", "rejected", "needs_business_answer"}:
            raise ValueError("Unknown partial demand status")
        else:
            known.append(row)
    return known, len(rows)


def source_query_rows(root, demand):
    """Read the exact saved demand phrases; IDs alone cannot identify a client's query."""
    source = demand.get("raw_query_source")
    if not isinstance(source, dict) or not isinstance(source.get("path"), str):
        raise ValueError("Raw demand needs a saved query_id/phrase source")
    path = relative_file(root, source["path"])
    if path.suffix.lower() != ".csv" or sha_file(path) != source.get("sha256"):
        raise ValueError("Raw demand CSV path or SHA-256 differs from source")
    reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig")))
    if not reader.fieldnames or not {"query_id", "phrase"} <= set(reader.fieldnames):
        raise ValueError("Raw demand CSV needs query_id and phrase columns")
    rows = {}
    for row in reader:
        query_id, phrase = row.get("query_id"), row.get("phrase")
        if not isinstance(query_id, str) or not query_id.strip() or not isinstance(phrase, str) or not phrase.strip() or query_id in rows:
            raise ValueError("Raw demand CSV has blank or repeated ID/phrase")
        rows[query_id] = phrase
    if set(rows) != names(demand.get("raw_query_ids"), "raw demand IDs"):
        raise ValueError("Saved raw query phrases differ from demand IDs")
    return rows


class BARReplayFailure(ValueError):
    def __init__(self, message, details):
        super().__init__(message)
        self.details = details


def bounded_bar_stream(value, limit=2048):
    """Keep a small diagnostic excerpt without echoing likely credential values."""
    excerpt = value[:limit]
    if excerpt.lstrip().startswith(("{", "[")):
        return {"text": "[structured child output omitted]", "chars": len(value),
                "truncated": len(value) > limit, "structured_omitted": True}
    excerpt = re.sub(r"(?i)\bBearer\s+[^\s]+", "Bearer [redacted]", excerpt)
    excerpt = re.sub(r'''(?i)(["']?(?:authorization|token|secret|password|api[_-]?key)["']?\s*[:=]\s*["']?)[^\s,}"']+''',
                     r"\1[redacted]", excerpt)
    sanitized_truncated = len(excerpt) > limit
    return {"text": excerpt[:limit], "chars": len(value),
            "truncated": len(value) > limit or sanitized_truncated,
            "structured_omitted": False}


def replay_bar(root, input_ref, saved_report):
    node = shutil.which("node")
    if node is None:
        raise ValueError("Node.js is required to verify BAR complete")
    with tempfile.TemporaryDirectory(prefix="bar-replay-", dir=root) as temp:
        output = Path(temp) / "report.json"
        process = subprocess.run([node, str(BAR_CLI), "--input", str(relative_file(root, input_ref)), "--out", str(output)],
                                 capture_output=True, text=True, timeout=90, check=False)
        if process.returncode != 0 or not output.is_file():
            raise BARReplayFailure(f"Bundled BAR replay failed (exit {process.returncode})", {
                "failure_kind": "nonzero_exit" if process.returncode != 0 else "missing_output",
                "child_exit": process.returncode,
                "input_ref": input_ref,
                "entrypoint": "bar/cli.mjs",
                "entrypoint_sha256": sha_file(BAR_CLI),
                "stdout": bounded_bar_stream(process.stdout),
                "stderr": bounded_bar_stream(process.stderr),
            })
        actual = read_json(output)
    def stable(value):
        data = json.loads(canonical(value))
        report = data.get("report", {})
        for key in ("analyzedAt", "snapshotId"):
            report.get("meta", {}).pop(key, None)
        for key in ("createdAt", "id"):
            report.get("snapshot", {}).pop(key, None)
        data.get("peer_benchmark", {}).get("meta", {}).pop("snapshot_id", None)
        return data
    if canonical(stable(actual)) != canonical(stable(saved_report)):
        raise ValueError("Saved BAR report differs from recomputed bundled BAR output")


def bar_input_url(input_data):
    root_url = input_data.get("url")
    own_url = (input_data.get("own_page") or {}).get("url")
    if root_url is not None and own_url is not None and root_url != own_url:
        raise ValueError("BAR input root and own_page URLs conflict")
    return root_url if root_url is not None else own_url


def stage_artifact_checks(root, refs):
    for label, path in refs["outputs"].items():
        target = relative_file(root, path)
        if not target.read_bytes().strip():
            raise ValueError(f"Empty output: {label}")
        if target.suffix.lower() == ".json" and label not in {"site_gap_report", "bar_report", "proposal_document"}:
            value = checked_object(root, path, label)
            names(value.get("evidence_refs"), f"{label}.evidence_refs")


def pending_item_checks(root, refs, node_id, node, state=None):
    """A partial attempt names each unresolved item; it cannot hide it in prose."""
    data = checked_object(root, refs["outputs"][PARTIAL_OUTPUT[node_id]], PARTIAL_OUTPUT[node_id])
    items = data.get("pending_items")
    if not isinstance(items, list) or not items:
        raise ValueError("Partial output needs explicit pending_items")
    raw = read_json(CONTRACT)["partial_item_schema"]
    required = set(raw["required"])
    denominators = {"block": {"blocks"}, "job": {"jobs"}, "task": {"tasks"}, "url": {"urls"},
                    "type": {"types"}, "variant": {"variants"}, "action": {"actions"},
                    "decision": {"decisions"}, "dependency": {"stage"}, "human": {"stage"},
                    "stage": {"stage"}, "source": {"sources", "types", "urls"}}
    seen = set()
    unknown_blocks = None
    for item in items:
        if not isinstance(item, dict) or not required <= set(item):
            raise ValueError("Pending item lacks required identity, source, reason or denominator fields")
        ident = item["pending_id"]
        if not isinstance(ident, str) or not ident.strip() or ident in seen:
            raise ValueError("Pending item IDs must be unique and nonempty")
        seen.add(ident)
        if item["state"] not in raw["states"] or item["scope_kind"] not in raw["scope_kinds"] or item["counted_in"] not in raw["denominators"]:
            raise ValueError("Pending item has unknown state, scope or denominator")
        if item["counted_in"] not in denominators[item["scope_kind"]]:
            raise ValueError("Pending item denominator differs from addressed scope")
        if not all(isinstance(item[k], str) and item[k].strip() for k in ("scope_id", "reason", "blocker")):
            raise ValueError("Pending item needs exact scope ID, reason and blocker")
        if item["type_id"] is not None and (not isinstance(item["type_id"], str) or not item["type_id"].strip()):
            raise ValueError("Pending item type_id must be null or nonempty")
        if item["scope_kind"] in {"type", "variant", "block"} and item["type_id"] is None:
            raise ValueError("Typed pending scope needs type_id")
        if item["source_ref"] is not None:
            if not isinstance(item["source_ref"], str):
                raise ValueError("Pending source_ref must be a run-relative file or null")
            relative_file(root, item["source_ref"])
        if item["scope_kind"] == "dependency" and item["scope_id"] not in node["depends_on"]:
            raise ValueError("Pending dependency does not name a parent node")
        if item["scope_kind"] == "block":
            if unknown_blocks is None:
                if node_id == "block_gap":
                    block = data
                else:
                    if state is None:
                        state = read_json(root / STATE_NAME)
                    block = previous_output(root, state, "block_gap", "block_register")
                rows = block.get("rows")
                if not isinstance(rows, list):
                    raise ValueError("Pending block must name an unknown canonical block row")
                unknown_blocks = {
                    (f"{row.get('type_id')}:{row.get('variant_id')}:{row.get('block_id')}", row["type_id"])
                    for row in rows if isinstance(row, dict) and row.get("status") == "unknown"
                    and isinstance(row.get("type_id"), str)
                }
            if (item["scope_id"], item["type_id"]) not in unknown_blocks:
                raise ValueError("Pending block must name an unknown canonical block row")
        if item["scope_kind"] == "job" and node_id == "gap":
            if not any(isinstance(job, dict) and job.get("landing_job_id") == item["scope_id"]
                       and job.get("disposition") == "business_question" for job in data.get("landing_jobs", [])):
                raise ValueError("Pending job must name a business_question landing job")
    return items


def typed_evidence_refs(document):
    """Enumerate declared evidence relationships, not arbitrary strings or links.

    Both node binding and client export use these relationships. Export still
    applies its own stricter path policy and requires recorded/declared hashes.
    """
    fields = {"source_ref", "source_refs", "source_file", "evidence_ref", "evidence_refs",
              "file_ref", "saved_html_ref", "http_receipt_ref", "variant_receipt_ref",
              "response_ref", "artifact_ref", "own_proof", "peer_example", "text_ref",
              "html_ref", "adapter_input_ref", "adapter_report_ref", "source_artifact_ref",
              "report_ref", "site_gap_report_ref", "peer_report_ref"}
    maps = {"source_files", "evidence_files", "files"}
    def walk(value, origin="", parent=""):
        if isinstance(value, list):
            for index, item in enumerate(value):
                yield from walk(item, f"{origin}[{index}]", parent)
        elif isinstance(value, dict):
            for key, child in value.items():
                location = f"{origin}.{key}"
                if key in maps and isinstance(child, dict):
                    for ref, digest in child.items():
                        yield ref, digest if isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest) else None, location
                elif key == "evidence_by_type" and isinstance(child, dict):
                    for item in child.values():
                        for ref in item if isinstance(item, list) else [item]:
                            if isinstance(ref, str):
                                yield ref, None, location
                elif key in fields or (key == "path" and parent in {"evidence_index", "source_index", "raw_query_source"}):
                    digest = value.get(key.removesuffix("_ref") + "_sha256") or value.get("sha256")
                    for ref in child if isinstance(child, list) else [child]:
                        if isinstance(ref, str):
                            yield ref, digest, location
                else:
                    yield from walk(child, location, key if key in {"evidence_index", "source_index", "raw_query_source"} else parent)
    yield from walk(document)


def consumed_source_bindings(root, refs, node_id):
    """Bind direct, typed source citations of this node, never a global run inventory."""
    bound = {}
    role_sets = {}
    resolved_paths = {}

    def resolve_candidate(candidate):
        # Cache successful exact spellings only for this collection. A later
        # record/preflight must resolve the filesystem afresh.
        if candidate not in resolved_paths:
            resolved_paths[candidate] = relative_file(root, candidate)
        return resolved_paths[candidate]

    def add(path, role, container=None, required=True):
        if not isinstance(path, str) or not path or "://" in path or path.startswith("#"):
            return
        base = Path(path)
        if base.is_absolute():
            raise ValueError("Consumed source must be run-relative: " + path)
        candidates = [path]
        if container is not None:
            candidates.append(str(Path(container).parent / path).replace("\\", "/"))
        resolved = None
        for candidate in candidates:
            try:
                resolved = resolve_candidate(candidate)
                break
            except ValueError:
                pass
        if resolved is None:
            if required:
                raise ValueError("Claimed source is missing: " + path)
            return
        relative = resolved.relative_to(root).as_posix()
        if relative not in bound:
            bound[relative] = {"path": relative, "sha256": sha_file(resolved), "roles": []}
            role_sets[relative] = set()
        row = bound[relative]
        if role not in role_sets[relative]:
            row["roles"].append(role)
            role_sets[relative].add(role)

    def looks_like_file(value, container):
        if not isinstance(value, str) or "://" in value or value.startswith("#") or value.endswith(":source"):
            return False
        path = Path(value)
        if path.is_absolute() or path.drive:
            return True  # Let add() reject an absolute or drive-relative file claim.
        if ":" in value.split("/", 1)[0].split("\\", 1)[0]:
            return False  # Logical evidence IDs such as peer:1 are not file paths.
        candidates = [value]
        if container is not None:
            candidates.append(str(Path(container).parent / value).replace("\\", "/"))
        for candidate in candidates:
            try:
                resolve_candidate(candidate)
                return True  # A saved run-relative file is a source regardless of suffix.
            except ValueError:
                pass
        return "/" in value or "\\" in value or bool(path.suffix) or value.startswith(".")

    ref_fields = {"source_ref", "source_refs", "source_file", "evidence_ref", "evidence_refs", "file_ref",
                  "section_briefs_ref",
                  "saved_html_ref", "http_receipt_ref", "variant_receipt_ref", "response_ref",
                  "artifact_ref", "own_proof", "peer_example"}
    optional_fields = {"html_file", "data_file"}

    def visit(value, container):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "source_files" and isinstance(child, dict):
                    for file_ref in child:
                        if looks_like_file(file_ref, container):
                            add(file_ref, "source_files", container)
                elif key == "evidence_files" and isinstance(child, dict):
                    for file_ref in child:
                        if looks_like_file(file_ref, container):
                            add(file_ref, "evidence_files", container)
                elif key in ref_fields:
                    values = child if isinstance(child, list) else [child]
                    for entry in values:
                        if looks_like_file(entry, container):
                            add(entry, key, container)
                elif key in optional_fields and looks_like_file(child, container):
                    add(child, key, container, required=False)
                else:
                    visit(child, container)
        elif isinstance(value, list):
            for child in value:
                visit(child, container)

    for label, path in refs["inputs"].items():
        if Path(path).suffix.lower() == ".json":
            data = checked_object(root, path, label)
            visit(data, path)
            for ref, digest, origin in typed_evidence_refs(data):
                if looks_like_file(ref, path):
                    add(ref, "typed" + origin, path)
                    if digest is not None:
                        resolved = next((entry for entry in bound.values() if entry["path"] in
                                         {ref, str(Path(path).parent / ref).replace("\\", "/")}), None)
                        if resolved is None or resolved["sha256"] != digest:
                            raise ValueError("Typed evidence SHA mismatch: " + ref)
    for label, path in refs["outputs"].items():
        if Path(path).suffix.lower() == ".json":
            data = checked_object(root, path, label)
            visit(data, path)
            for ref, digest, origin in typed_evidence_refs(data):
                if looks_like_file(ref, path):
                    add(ref, "typed" + origin, path)
                    if digest is not None:
                        resolved = next((entry for entry in bound.values() if entry["path"] in
                                         {ref, str(Path(path).parent / ref).replace("\\", "/")}), None)
                        if resolved is None or resolved["sha256"] != digest:
                            raise ValueError("Typed evidence SHA mismatch: " + ref)
    if node_id == "semantic_types":
        demand = checked_object(root, refs["inputs"]["demand_universe"], "demand_universe")
        source = demand.get("raw_query_source")
        if isinstance(source, dict) and source.get("path"):
            add(source["path"], "raw_query")
    if node_id == "gap":
        link = checked_object(root, refs["inputs"]["link_snapshot"], "link_snapshot")
        assessment = link.get("links_assessment") or {}
        for path in assessment.get("evidence_files", {}):
            add(path, "link_raw")
        for binding in assessment.get("raw_metric_basis", []):
            if isinstance(binding, dict) and binding.get("metadata_ref"):
                add(binding["metadata_ref"], "link_raw_metadata")
        for source in assessment.get("raw_primary_sources", []):
            if isinstance(source, dict) and source.get("source_ref"):
                add(source["source_ref"], "link_primary")
            if isinstance(source, dict):
                for excluded in source.get("excluded_rows", []):
                    if isinstance(excluded, dict) and excluded.get("evidence_ref"):
                        add(excluded["evidence_ref"], "link_primary_exclusion")
        for source in assessment.get("raw_supporting_sources", []):
            if isinstance(source, dict) and source.get("source_ref"):
                add(source["source_ref"], "link_primary_context")
    if node_id == "audit":
        for path in audit_artifact_refs(root, refs["inputs"]["audit_sources"]):
            add(path, "audit_raw")
    if node_id == "block_gap":
        block = checked_object(root, refs["outputs"]["block_register"], "block_register")
        for item in (block.get("evidence_index") or {}).values():
            if isinstance(item, dict) and item.get("path"):
                add(item["path"], "block_html_or_render")
    if node_id == "text_bar":
        text = checked_object(root, refs["outputs"]["text_register"], "text_register")
        scopes = checked_object(root, refs["inputs"]["material_task_scopes"], "material_task_scopes")
        for path in text.get("files", {}):
            add(path, "bar_file")
        for path in scopes.get("source_files", {}):
            add(path, "bar_raw")
    if node_id == "decision_map":
        plan = checked_object(root, refs["outputs"]["decision_map"], "decision_map").get("measurement_plan")
        if isinstance(plan, dict):
            for path in measurement_receipt_checks(root, plan):
                add(path, "measurement_raw")
    for row in bound.values():
        row["roles"].sort()
    return [bound[path] for path in sorted(bound)]


def audit_artifact_refs(root, source_ref):
    sources = checked_object(root, source_ref, "audit_sources")
    paths = []
    for source in sources.get("resources") or []:
        if isinstance(source, dict):
            paths.extend(source[key] for key in ("saved_html_ref", "http_receipt_ref", "variant_receipt_ref") if source.get(key))
    for receipt in sources.get("inspection_receipts") or []:
        if isinstance(receipt, dict) and receipt.get("response_ref"):
            paths.append(receipt["response_ref"])
    for artifact in sources.get("source_artifacts") or []:
        if isinstance(artifact, dict) and artifact.get("artifact_ref"):
            paths.append(artifact["artifact_ref"])
    if any(not isinstance(path, str) for path in paths):
        raise ValueError("Audit nested artifact references must be run-relative paths")
    return sorted(set(paths))


def client_cell(value):
    """Stable, readable packet cell; unknown measurements stay unknown."""
    if value is None:
        return "н/д"
    if isinstance(value, dict):
        value = ", ".join(f"{key}: {client_cell(item)}" for key, item in sorted(value.items()))
    elif isinstance(value, list):
        value = ", ".join(client_cell(item) for item in value) if value else "—"
    return str(value).replace("|", "/").replace("\n", " ").strip() or "—"


def client_query_ids_by_type(demand_rows):
    """Group validated source query records by semantic type, never by URL or variant."""
    if demand_rows is None:
        return None
    if not isinstance(demand_rows, list):
        raise ValueError("Client type query source must be a saved demand row list")
    grouped, seen = {}, set()
    for cluster in demand_rows:
        if not isinstance(cluster, dict) or not isinstance(cluster.get("type_id"), str) or not cluster["type_id"]:
            raise ValueError("Client type query source needs a type ID")
        queries = cluster.get("queries")
        if not isinstance(queries, list):
            raise ValueError("Client type query source needs query rows")
        for query in queries:
            ident = query.get("query_id") if isinstance(query, dict) else None
            if not isinstance(ident, str) or not ident or ident in seen:
                raise ValueError("Client type query source has blank or repeated query ID")
            seen.add(ident)
            grouped.setdefault(cluster["type_id"], []).append(ident)
    return {kind: sorted(ids) for kind, ids in grouped.items()}


def client_type_line(row, raw_query_ids_by_type):
    rank_scope = row["rank_scope"]
    slice_text = " / ".join(client_cell(rank_scope.get(key)) for key in ("source", "date", "region")
                          if rank_scope.get(key)) if isinstance(rank_scope, dict) else client_cell(rank_scope)
    coverage = row["rank_coverage"]
    coverage_text = (f"в сохранённом срезе типа: {coverage['observed_own_rows']} наблюдений собственных позиций / "
                     f"{coverage['ranked_own_urls']} URL с позициями / {coverage['own_urls']} URL типа")
    positions = row["rank_positions"]
    position_text = "; ".join(f"{source}: {client_cell(value.get('average_position') if isinstance(value, dict) else value)}"
                               for source, value in positions.items()) if isinstance(positions, dict) else client_cell(positions)
    decisions = "; ".join(f"{url} → {decision}" for url, decision in sorted(row["url_decisions"].items())) or "—"
    task = {"create": "создать", "improve": "улучшить", "keep": "сохранить", "reject": "отклонить",
            "covered": "покрыто", "integrate": "объединить", "business_question": "решение владельца"}.get(row["task_decision"], row["task_decision"])
    if raw_query_ids_by_type is None:
        query_text = "исходные записи запросов типа: н/д (источник недоступен)"
    else:
        query_ids = raw_query_ids_by_type.get(row["type_id"], [])
        query_text = ("исходные записи запросов сохранённого источника, отнесённые к типу "
                      f"(не по URL/варианту; не суммировать по вариантам): {len(query_ids)}; "
                      f"ID: {', '.join(query_ids) or '—'}")
    fields = (row.get("type_label") or row["type_id"], row.get("variant_label") or row["variant_id"],
              ", ".join(row["own_urls"]) or "—", ", ".join(row["peer_urls"]) or "—",
              slice_text, coverage_text, position_text, decisions, task,
              f"{query_text}; ID интентов: {', '.join(row['intent_ids']) or '—'}", row["basis"])
    return "| " + " | ".join(client_cell(value) for value in fields) + " |"


def client_demand_line(row):
    queries = "; ".join(
        f"{item['query_id']} «{client_cell(item['phrase'])}»: {client_cell(item['disposition'])}; "
        f"own {item['own_status']}, peer {item['peer_status']}; "
        + (" / ".join(f"{rank['role']} {rank['source']} {rank['url']} #{rank['position']} "
                       f"[{client_cell(rank['scope'])}]" for rank in item['rank_observations'])
           or "own/peer позиция неизвестна") for item in row["queries"])
    fields = (row["cluster_id"], row["type_id"], row["task_decision"],
              ", ".join(row["own_urls"]) or "—", ", ".join(row["landing_job_ids"]) or "—",
              ", ".join(row["peer_urls"]) or "peer неизвестны", queries, row["reason"])
    return "| " + " | ".join(client_cell(value) for value in fields) + " |"


def client_source_line(row):
    fields = (row["source_id"], row["source_label"], f"[Сохранённые строки]({row['packet_ref']})",
              row["source_sha256"], row["limitation"])
    return "| " + " | ".join(client_cell(value) for value in fields) + " |"


def markdown_tables(document):
    """Extract actual Markdown tables, not claimed section names or prose."""
    tables = []
    lines = document.splitlines()
    fenced = set()
    fence = None
    for line_index, line in enumerate(lines):
        stripped = line.lstrip()
        marker = stripped[:3] if stripped.startswith(("```", "~~~")) else None
        if marker and fence is None:
            fence = marker
        elif marker == fence:
            fence = None
        if fence is not None or marker is not None:
            fenced.add(line_index)
    for index in range(len(lines) - 2):
        if index in fenced or index + 1 in fenced:
            continue
        header, divider = lines[index:index + 2]
        if not (header.strip().startswith("|") and divider.strip().startswith("|")):
            continue
        cells = lambda line: [cell.strip() for cell in line.strip().strip("|").split("|")]
        header_cells, divider_cells = cells(header), cells(divider)
        if len(header_cells) < 2 or len(header_cells) != len(divider_cells) or not all(
                cell.replace(":", "").replace("-", "") == "" and "-" in cell for cell in divider_cells):
            continue
        rows = []
        cursor = index + 2
        while cursor < len(lines) and cursor not in fenced and lines[cursor].strip().startswith("|"):
            row = cells(lines[cursor])
            if len(row) == len(header_cells):
                rows.append(row)
            cursor += 1
        heading = next((line.strip() for line in reversed(lines[:index]) if line.strip().startswith("#")), None)
        tables.append({"heading": heading, "header": header_cells, "rows": rows})
    return tables


def visible_identity(cells, identity):
    """Match a complete visible ID/label phrase, not a substring of another name."""
    def terms(value):
        return "".join(character if character.isalnum() else " " for character in str(value).casefold()).split()
    needle = terms(identity)
    return bool(needle) and any(
        any(terms(cell)[index:index + len(needle)] == needle
            for index in range(len(terms(cell)) - len(needle) + 1))
        for cell in cells)


def client_main_table_checks(document, bindings, semantic, block, bar, client_type_labels=None):
    """Bind the compact buyer tables to real Markdown rows and upstream coverage IDs."""
    required = {"overall_gap": None, "gap_types": set(semantic["type_ids"]),
                "blocks": set(block["variants_by_type"]),
                "bar_types": set(bar["type_ids"]), "site_state": {"gsc", "webmaster", "visual", "crawl"},
                "b_horizons": None, "roadmap": {"1-3", "4-6", "7-12"}}
    if not isinstance(bindings, dict) or set(bindings) != set(required):
        raise ValueError("Main proposal needs exact GAP, block, BAR, site-state, B and roadmap table bindings")
    tables = markdown_tables(document)
    type_labels = semantic.get("type_labels") or {}
    if not isinstance(type_labels, dict) or not set(type_labels) <= set(semantic["type_ids"]) or any(
            not isinstance(label, str) or not label.strip() for label in type_labels.values()):
        raise ValueError("Upstream type labels must map known types to readable names")
    if client_type_labels is not None:
        if (not isinstance(client_type_labels, dict)
                or not set(client_type_labels) <= set(semantic["type_ids"])
                or any(not isinstance(label, str) or not label.strip() for label in client_type_labels.values())):
            raise ValueError("Client type labels must map known types to readable names")
        def normalized_label(label):
            return tuple("".join(c if c.isalnum() else " " for c in label.casefold()).split())
        if any(key in type_labels and normalized_label(label) != normalized_label(type_labels[key])
               for key, label in client_type_labels.items()):
            raise ValueError("Client type labels conflict with upstream readable names")
        type_labels = {**type_labels, **client_type_labels}
        identities = {normalized_label(key): key for key in semantic["type_ids"]}
        for key, label in type_labels.items():
            normalized = normalized_label(label)
            if not normalized or normalized in identities and identities[normalized] != key:
                raise ValueError("Client type labels must identify each type unambiguously")
            identities[normalized] = key
    source_labels = {"gsc": ("GSC", "Google Search Console"),
                     "webmaster": ("webmaster", "Вебмастер", "Яндекс Вебмастер"),
                     "visual": ("visual", "Визуальный осмотр"),
                     "crawl": ("crawl", "Краул")}
    used = set()
    for kind, coverage in required.items():
        binding = bindings[kind]
        if not isinstance(binding, dict) or not isinstance(binding.get("header"), list) or not isinstance(binding.get("rows"), list):
            raise ValueError("Main table binding needs header and addressable rows")
        rows = binding["rows"]
        if not rows or any(not isinstance(row, dict) or not isinstance(row.get("key"), str)
                           or not isinstance(row.get("cells"), list) for row in rows):
            raise ValueError("Main table has no addressable data rows")
        keys = [row["key"] for row in rows]
        if len(keys) != len(set(keys)) or coverage is not None and set(keys) != coverage:
            raise ValueError("Main table loses an upstream type, source or phase")
        match = [i for i, table in enumerate(tables) if table["header"] == binding["header"]
                 and all(row["cells"] in table["rows"] for row in rows)]
        if len(match) != 1 or match[0] in used:
            raise ValueError("Declared main table is absent, duplicated or not a real Markdown table")
        table = tables[match[0]]
        used.add(match[0])
        if len(table["rows"]) != len(rows) or any(len(row["cells"]) != len(table["header"])
                or any(not str(cell).strip() or str(cell).strip() in {"[ ]", "[]"} for cell in row["cells"])
                for row in rows):
            raise ValueError("Main table rows are blank, placeholders or differ from the bound table")
        if kind in {"gap_types", "blocks", "bar_types", "site_state", "roadmap"}:
            for row in rows:
                key = row["key"]
                aliases = ((key, type_labels.get(key)) if kind in {"gap_types", "blocks", "bar_types"}
                           else source_labels[key] if kind == "site_state" else (key,))
                if not any(alias and visible_identity(row["cells"], alias) for alias in aliases):
                    raise ValueError("Main table visible row does not identify its upstream type, source or phase")
        if kind == "b_horizons" and not all(any(str(month) in cell for cell in table["header"]) for month in (3, 6, 12)):
            raise ValueError("Main B table needs visible 3/6/12 columns")
    if sum(1 for table in tables if table["header"] == bindings["roadmap"]["header"]) != 1:
        raise ValueError("Main proposal needs one roadmap table")


def expected_client_sources(root, state):
    """Small packet-visible slice of exact demand, rank and available link inputs."""
    demand_ref = state["attempts"]["semantic_types"][-1]["inputs"]["demand_universe"]["path"]
    demand = checked_object(root, demand_ref, "demand_universe")
    query_ref = demand["raw_query_source"]["path"]
    site_ref = state["attempts"]["gap"][-1]["inputs"]["site_gap_input"]["path"]
    site = checked_object(root, site_ref, "site_gap_input")
    link_ref = state["attempts"]["gap"][-1]["inputs"]["link_snapshot"]["path"]
    links = checked_object(root, link_ref, "link_snapshot")
    raw_ids = set(source_query_rows(root, demand))
    def selected(rankings):
        return {"scope": rankings.get("scope"), **{kind: [row for row in rankings.get(kind, [])
                if row.get("query_id") in raw_ids] for kind in ("gsc", "serp")}}
    rank_projection = {"source_ref": site_ref, "source_sha256": sha_file(relative_file(root, site_ref)),
                       "own": selected(site.get("rankings") or {}),
                       "peers": [{"domain": peer.get("domain"), **selected(peer.get("rankings") or {})}
                                 for peer in site.get("competitors", [])]}
    required = {"raw_demand": {"source_ref": query_ref,
                 "source_sha256": demand["raw_query_source"]["sha256"], "mode": "copy",
                 "source_label": "saved raw demand CSV"},
                "rank_rows": {"source_ref": site_ref, "source_sha256": rank_projection["source_sha256"],
                 "mode": "projection", "source_label": "saved site GAP rank input", "projection": rank_projection}}
    assessment = links.get("links_assessment") or {}
    if assessment.get("status") in {"available", "partial"}:
        for index, (ref, digest) in enumerate(sorted((assessment.get("evidence_files") or {}).items())):
            required[f"link_rows:{index}"] = {"source_ref": ref, "source_sha256": digest, "mode": "copy",
                                               "source_label": assessment.get("source_system") or "saved links"}
    return required


def expected_client_demand(root, state, gap, report, semantic):
    demand_ref = state["attempts"]["semantic_types"][-1]["inputs"]["demand_universe"]["path"]
    demand = checked_object(root, demand_ref, "demand_universe")
    site_ref = state["attempts"]["gap"][-1]["inputs"]["site_gap_input"]["path"]
    site = checked_object(root, site_ref, "site_gap_input")
    raw_ids = names(demand.get("raw_query_ids"), "client demand raw query IDs")
    phrases = source_query_rows(root, demand)
    clusters = source_clusters(demand)
    if not clusters or set().union(*(names(row.get("query_ids"), "cluster query IDs") for row in clusters)) != raw_ids:
        raise ValueError("Client demand source query IDs do not cover raw demand")
    occurrences = [query for row in clusters for query in row["query_ids"]]
    if len(occurrences) != len(raw_ids):
        raise ValueError("Client demand query IDs repeat across clusters")
    rankings = site.get("rankings") or {}
    observations = []
    for role, domain, rank_set in [("own", site.get("client_domain"), rankings)] + [
            ("peer", peer.get("domain"), peer.get("rankings") or {}) for peer in site.get("competitors", [])]:
        for source in ("gsc", "serp"):
            for item in rank_set.get(source) or []:
                if item.get("query_id") not in raw_ids:
                    continue
                position = item.get("position")
                if position is not None and (type(position) not in (int, float) or not math.isfinite(position) or position <= 0):
                    raise ValueError("Client raw rank observation needs a finite positive position")
                if item.get("canonical_url") and position is not None:
                    observations.append({"query_id": item["query_id"], "role": role, "domain": domain,
                                         "source": source, "url": item["canonical_url"],
                                         "position": item["position"], "scope": rank_set.get("scope")})
    jobs = gap["landing_jobs"]
    result = []
    for cluster in clusters:
        cluster_id = cluster["id"]
        reviewed = [row for row in (site.get("demand_review") or {}).get("clusters", [])
                    if row.get("id") == cluster_id and row.get("page_type") == cluster["type_id"]
                    and row.get("status") == cluster["status"]]
        accepted = reviewed[0] if len(reviewed) == 1 and reviewed[0].get("evidence") else {}
        reason = cluster.get("basis") or cluster.get("coverage_scope")
        if not reason:
            if accepted:
                # The recorded GAP input may own the analysis while the demand
                # universe only identifies raw rows. Never invent a new reason.
                reason = accepted.get("basis")
        related_jobs = sorted(job["landing_job_id"] for job in jobs if cluster_id in job.get("cluster_ids", []))
        own_urls = cluster.get("own_urls", [])
        target_ref = cluster.get("query_decisions_ref") or accepted.get("source_ref")
        if "own_urls" not in cluster and not related_jobs and accepted and target_ref:
            target_file = relative_file(root, target_ref)
            pins = [entry for entry in state["attempts"]["gap"][-1].get("consumed_sources", [])
                    if entry.get("path") == target_ref]
            if len(pins) != 1 or pins[0].get("sha256") != sha_file(target_file):
                raise ValueError("Client demand targets need the exact consumed GAP query-decision source")
            target_rows = checked_object(root, target_ref, "query decisions").get("rows")
            if not isinstance(target_rows, list) or any(not isinstance(row, dict) for row in target_rows):
                raise ValueError("Client demand target source needs query rows")
            matched = [row for row in target_rows if row.get("cluster_id") == cluster_id]
            if (len(matched) != len(cluster["query_ids"])
                    or {row.get("query_id") for row in matched} != set(cluster["query_ids"])):
                raise ValueError("Client demand targets must cover each raw cluster query exactly once")
            available_urls = {row["url"] for row in gap.get("page_allocations", [])
                              if row.get("type_id") == cluster["type_id"]}
            own_urls = set()
            dispositions = cluster.get("query_dispositions") or accepted.get("query_dispositions") or {}
            reasons = cluster.get("query_decision_reasons") or accepted.get("query_decision_reasons") or {}
            for row in matched:
                query_id = row["query_id"]
                targets = row.get("target_urls")
                if (row.get("phrase") != phrases[query_id]
                        or not isinstance(row.get("reason"), str) or not row["reason"].strip()
                        or not isinstance(row.get("evidence_refs"), list) or not row["evidence_refs"]
                        or any(not isinstance(ref, str) or not ref.strip() for ref in row["evidence_refs"])
                        or row.get("disposition") != dispositions.get(query_id)
                        or query_id in reasons and row["reason"] != reasons[query_id]
                        or not isinstance(targets, list) or any(not isinstance(url, str) for url in targets)
                        or not set(targets) <= available_urls):
                    raise ValueError("Client demand target row differs from raw query, reviewed decision or typed inventory")
                own_urls.update(targets)
        queries = []
        for query_id in sorted(cluster["query_ids"]):
            disposition = (cluster.get("query_dispositions") or {}).get(query_id,
                           cluster.get("disposition", cluster["status"]))
            selected_observations = sorted(
                                 [{k: value[k] for k in ("role", "domain", "source", "url", "position", "scope")}
                                  for value in observations if value["query_id"] == query_id],
                                 key=lambda row: (row["role"], row["source"], str(row["domain"]), str(row["url"])))
            queries.append({"query_id": query_id, "phrase": phrases[query_id], "disposition": disposition,
                            "own_status": "observed" if any(r["role"] == "own" for r in selected_observations) else "unknown",
                            "peer_status": "observed" if any(r["role"] == "peer" for r in selected_observations) else "unknown",
                            "rank_observations": selected_observations})
        result.append({"cluster_id": cluster_id, "type_id": cluster["type_id"],
                       "task_decision": semantic["decisions"][cluster["type_id"]],
                       "own_urls": sorted(own_urls),
                       "landing_job_ids": related_jobs, "peer_urls": sorted(cluster.get("peer_urls", [])),
                       "queries": queries,
                       "reason": reason})
    if any(not row["reason"] or not (row["own_urls"] or row["landing_job_ids"] or row["task_decision"] in {"reject", "business_question"})
           for row in result):
        raise ValueError("Client demand cluster needs source-backed target and reason")
    return result


def client_proof_line(row):
    proof = f"[Фрагмент]({row['packet_ref']})" if row["status"] == "shown" else "недоступно"
    fields = (row["source_id"], row["evidence_id"], row["source_url"], row.get("captured_at") or "дата неизвестна",
              proof, row["source_sha256"], row.get("supporting_quote") or "визуальное свидетельство",
              row["limitation"], row.get("claim_limit") or "—")
    return "| " + " | ".join(client_cell(value) for value in fields) + " |"


def client_work_line(row):
    return "| " + " | ".join(client_cell(row[key]) for key in
        ("action_id", "unit", "quantity", "phase", "acceptance", "estimate_ref")) + " |"


def expected_client_proofs(root, state, block, strategy):
    audit = previous_output(root, state, "audit", "audit_findings")
    audit_source_ref = state["attempts"]["audit"][-1]["inputs"]["audit_sources"]["path"]
    audit_sources = checked_object(root, audit_source_ref, "audit_sources")
    audit_dates = {row["response_ref"]: row.get("captured_at") for row in audit_sources.get("inspection_receipts", [])
                   if row.get("response_ref")}
    audit_dates.update({row["artifact_ref"]: row.get("captured_at") for row in audit_sources.get("source_artifacts", [])
                        if row.get("artifact_ref")})
    block_rows = {f"block:{r['type_id']}:{r['variant_id']}:{r['block_id']}": r for r in block["rows"]}
    audit_rows = {f"audit:{r['finding_id']}": r for r in audit["findings"]}
    required = {}
    for disposition in strategy["decision_ledger"]:
        source_id = disposition["source_id"]
        if source_id in block_rows:
            row = block_rows[source_id]
            if disposition["disposition"] != "selected":
                continue
            for evidence_id in row.get("own_evidence_refs", []) + row.get("peer_evidence_refs", []):
                item = block["evidence_index"][evidence_id]
                required[(source_id, evidence_id)] = {
                    "source_ref": item["path"], "source_sha256": item["sha256"],
                    "source_url": item["source_url"], "captured_at": row.get("date"),
                    "supporting_quote": item.get("supporting_quote")}
        elif disposition["disposition"] == "selected" and source_id in audit_rows:
            item = audit_rows[source_id]
            source_ref = item["source_ref"]
            required[(source_id, source_ref)] = {
                "source_ref": source_ref, "source_sha256": sha_file(relative_file(root, source_ref)),
                "source_url": item.get("diagnosed_request_url") or item.get("url") or "scope unknown",
                "captured_at": item.get("captured_at") or audit_dates.get(source_ref),
                "supporting_quote": item.get("supporting_quote")}
    for source_id, row in block_rows.items():
        if row["status"] not in {"sufficient", "not_applicable"}:
            continue
        for evidence_id in row.get("own_evidence_refs", []) + row.get("peer_evidence_refs", []):
            item = block["evidence_index"][evidence_id]
            required[(source_id, evidence_id)] = {
                "source_ref": item["path"], "source_sha256": item["sha256"],
                "source_url": item["source_url"], "captured_at": row.get("date"),
                "supporting_quote": item.get("supporting_quote")}
    for claim in audit.get("positive_claims", []):
        source_ref = claim["source_ref"]
        required[(f"audit_positive:{claim['claim_id']}", source_ref)] = {
            "source_ref": source_ref, "source_sha256": sha_file(relative_file(root, source_ref)),
            "source_url": ", ".join(claim["addressed_urls"]), "captured_at": claim.get("captured_at"),
            "supporting_quote": claim["supporting_quote"], "addressed_urls": claim["addressed_urls"]}
    return required


def client_block_line(row, works):
    work = works.get(row.get("action_id")) if row.get("action_id") else None
    if work:
        default_work_label = row["reason"]
    elif row["status"] == "sufficient" and row["disposition"] == "no_action_required":
        default_work_label = "Работа не требуется"
    elif row["status"] == "not_applicable" and row["disposition"] == "not_applicable":
        default_work_label = "Не применимо"
    elif row["status"] == "unknown" or row["disposition"] == "pending_verification":
        default_work_label = "Проверка ожидается"
    elif row["disposition"] in {"deferred_with_reason", "blocked"}:
        default_work_label = "Работа отложена"
    elif row["disposition"] == "rejected_with_reason":
        default_work_label = "Работа не включена"
    else:
        default_work_label = row["reason"]
    work_label = row.get("work_label") or default_work_label
    work_detail = (f"{client_cell(work['quantity'])} × {client_cell(work['unit'])}; {client_cell(work['phase'])}; "
                   f"приёмка: {client_cell(work['acceptance'])}") if work else "—"
    fields = (row.get("block_label") or row["block_id"], ", ".join(row["target_urls"]) or "—",
              ", ".join(row["landing_job_ids"]) or "—", row["status"], row["disposition"],
              work_label, work_detail, row["reason"])
    return "| " + " | ".join(client_cell(value) for value in fields) + " |"


def client_measurement_line(plan):
    baseline = plan["baseline"]
    baseline_text = (f"{client_cell(baseline['value'])} за {client_cell(baseline['period'])}"
                     if baseline["status"] == "known" else f"неизвестна: {client_cell(baseline['reason'])}")
    proxy = plan.get("proxy")
    proxy_text = (f"{client_cell(proxy.get('event'))}: {client_cell(proxy.get('observed_value'))} {client_cell(proxy.get('unit'))}"
                  if proxy and proxy.get("status") == "observed" else
                  f"{client_cell(proxy.get('status'))}: {client_cell(proxy.get('reason'))}" if proxy else "—")
    verification = plan["verification"]
    verification_text = f"{verification['method']}; {verification['cadence']}"
    fields = (plan["outcome_unit"], plan["event_definition"], plan.get("metric_rationale"), plan["source_status"],
              plan["source_kind"], plan.get("source_ref"), plan["period"], plan["dedup_rule"],
              baseline_text, proxy_text, verification_text, " / ".join(plan["comparison_periods"]))
    segments = plan.get("promised_segments", [])
    segment_text = "; ".join(
        f"{row['segment_id']}: {row['status']}; committed={str(row['committed']).lower()}"
        + (f"; capture={row['capture']}; persist={row['persist']}; join={row['join']}; "
           f"unknown={row['unknown_bucket']}; test={row['test']}; "
           f"dependencies={', '.join(row['dependencies'])}" if row["status"] == "planned"
           else f"; reason={row['reason']}")
        for row in segments
    ) or "нет обещанной детализации"
    qualification_text = "—"
    if plan["outcome_unit"] == "unique_confirmed_inquiry":
        protocol = plan["qualification_protocol"]
        rules, record = protocol["decision_rules"], protocol["record"]
        path_text = "; ".join(
            f"{row['intake_path']}: planned test={row['method']}; expected record={row['expected_record']}"
            for row in protocol["path_tests"])
        qualification_text = (f"include={rules['include']}; exclude={'; '.join(rules['exclude'])}; "
                              f"record roles={', '.join(record['fields'])}; "
                              f"decision values={', '.join(record['decision_values'])}; "
                              f"storage={record['storage']}; owner={verification['owner_role']}; "
                              f"chosen paths={', '.join(protocol['intake_paths'])}; {path_text}; "
                              "test status=planned, not observed")
    fields += (segment_text, qualification_text)
    header = "| Единица | Событие | Обоснование | Статус | Источник | Ref | Период | Дедупликация | База | Прокси | Проверка | Сравнение | Сегменты | Квалификация |"
    separator = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    row = "| " + " | ".join(client_cell(value) for value in fields) + " |"
    return "\n".join((header, separator, row))


def client_case_line(field):
    return "| " + " | ".join(client_cell(field.get(key)) for key in
                                 ("status", "label", "reason", "source_refs")) + " |"


def client_claim_line(row):
    source = f"[{client_cell(row['source_system'])}]({row['source_table_ref']})" if row["status"] != "unknown" else "Источник недоступен"
    return "| " + " | ".join(client_cell(value) for value in
        (row["visible_quote"], row["status"], row.get("value"), row["unit"], row["period"],
         row["scope"], source, row["limitation"] if row["status"] != "unknown" else row["reason"])) + " |"


def client_numeric_origin_line(row):
    origin = row["origin"]
    if origin["status"] == "unavailable":
        fields = (row["row_id"], "первичный источник недоступен", origin["reason"], origin["claim_limit"])
    else:
        filter_text = canonical(origin["filter"]).decode("utf-8")
        if origin.get("source_level_metadata"):
            filter_text += "; shared metadata " + canonical(origin["source_level_metadata"]).decode("utf-8")
        fields = (row["row_id"], origin["source_ref"], origin["source_sha256"],
                  f"[Первичная строка]({origin['packet_ref']})", row["period"],
                  filter_text)
    return "| " + " | ".join(client_cell(value) for value in fields) + " |"


def json_pointer_segments(pointer):
    if pointer == "":
        return ()
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("Numeric origin needs an absolute JSON pointer")
    if re.search(r"~(?![01])", pointer):
        raise ValueError("Malformed RFC 6901 JSON pointer escape")
    return tuple(token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/"))


def pointer_descends(pointer, parent):
    child_parts, parent_parts = json_pointer_segments(pointer), json_pointer_segments(parent)
    return len(child_parts) > len(parent_parts) and child_parts[:len(parent_parts)] == parent_parts


def json_pointer(document, pointer):
    current = document
    for token in json_pointer_segments(pointer):
        try:
            if isinstance(current, list):
                if not token.isdigit() or str(int(token)) != token:
                    raise ValueError("Numeric origin array index is invalid")
                current = current[int(token)]
            else:
                current = current[token]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("Numeric origin JSON pointer is absent") from exc
    return current


def raw_link_metadata_checks(root, link_snapshot):
    """Bind declared raw metric/grain identities to saved JSON metadata bytes."""
    assessment = link_snapshot.get("links_assessment") or {}
    bases = assessment.get("raw_metric_basis", [])
    if not isinstance(bases, list):
        raise ValueError("raw_metric_basis must be a list")
    for basis in bases:
        if not isinstance(basis, dict):
            raise ValueError("Raw link metadata binding must be an object")
        ref, digest = basis.get("metadata_ref"), basis.get("metadata_sha256")
        if (not isinstance(ref, str) or not ref or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("Raw link metadata needs saved path and SHA")
        source = relative_file(root, ref)
        if sha_file(source) != digest:
            raise ValueError("Raw link metadata SHA-256 mismatch")
        document = checked_object(root, ref, "raw link metadata")
        fields = basis.get("fields")
        if not isinstance(fields, dict):
            raise ValueError("Raw link metadata needs addressable fields")
        for role, field in fields.items():
            if not isinstance(field, dict) or not isinstance(field.get("pointer"), str):
                raise ValueError("Raw link metadata needs JSON pointers")
            actual = json_pointer(document, field["pointer"])
            if not isinstance(actual, str) or not actual.strip() or actual != field.get("value"):
                raise ValueError(f"Raw link metadata {role} differs from saved source")


def raw_link_primary_checks(root, link_snapshot):
    """Reconcile declared normalized link rows with bounded saved CSV/JSON primary rows."""
    assessment = link_snapshot.get("links_assessment") or {}
    if assessment.get("status") not in {"available", "partial"}:
        return
    evidence = assessment.get("evidence_files") or {}
    primary = assessment.get("raw_primary_sources", [])
    supporting = assessment.get("raw_supporting_sources", [])
    if not isinstance(primary, list) or not isinstance(supporting, list):
        raise ValueError("Raw primary and supporting source declarations must be lists")
    normalized = {}
    declared_raw_refs = set()
    for path in evidence:
        saved = checked_object(root, path, "normalized link source")
        if not isinstance(saved.get("links"), list):
            raise ValueError("Normalized link source needs links rows")
        refs = saved.get("raw_source_refs", [])
        if not isinstance(refs, list) or any(not isinstance(x, str) or not x for x in refs):
            raise ValueError("Normalized raw_source_refs must be a file-reference list")
        declared_raw_refs.update(refs)
        for row in saved["links"]:
            if not isinstance(row, dict):
                raise ValueError("Normalized link row must be an object")
            key = (row.get("source_ref"), row.get("source_row_id"))
            if row.get("source_row_id") is not None:
                if key in normalized:
                    raise ValueError("Duplicate normalized primary row identity")
                normalized[key] = row

    def saved_file(ref, digest, label):
        if (not isinstance(ref, str) or not ref or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError(label + " needs a saved run-relative path and SHA-256")
        path = relative_file(root, ref)
        if sha_file(path) != digest:
            raise ValueError(label + " SHA-256 mismatch")
        return path

    source_refs = set()
    for item in supporting:
        if not isinstance(item, dict) or item.get("role") not in {"metadata", "context"}:
            raise ValueError("Raw supporting source needs metadata/context role")
        ref = item.get("source_ref")
        saved_file(ref, item.get("source_sha256"), "Raw supporting source")
        if ref in source_refs:
            raise ValueError("Duplicate raw source declaration")
        source_refs.add(ref)

    claimed_normalized = set()
    for item in primary:
        if not isinstance(item, dict) or item.get("status") not in {"verified", "unverified", "unsupported"}:
            raise ValueError("Raw primary source needs verified/unverified/unsupported status")
        ref = item.get("source_ref")
        path = saved_file(ref, item.get("source_sha256"), "Raw primary source")
        if ref in source_refs:
            raise ValueError("Duplicate raw source declaration")
        source_refs.add(ref)
        if item["status"] != "verified":
            if not isinstance(item.get("reason"), str) or not item["reason"].strip():
                raise ValueError("Unverified raw primary source needs reason")
            continue
        fmt = item.get("format")
        mapping = item.get("field_map")
        row_ref = item.get("row_source_ref")
        included = item.get("included_row_ids")
        excluded = item.get("excluded_rows", [])
        if (fmt not in {"csv", "json"} or not isinstance(mapping, dict)
                or not {"source_row_id", "domain"}.issubset(mapping)
                or not any(k in mapping for k in ("raw_referring_domains", "raw_backlinks"))
                or set(mapping) - {"source_row_id", "domain", "raw_referring_domains", "raw_backlinks", "provider", "date", "scope"}
                or not isinstance(row_ref, str) or row_ref not in evidence
                or not isinstance(included, list)
                or not isinstance(excluded, list)):
            raise ValueError("Verified raw primary needs explicit row locator, field map and coverage")
        if path.stat().st_size > 8_000_000:
            raise ValueError("Raw primary file exceeds bounded parser size")
        if fmt == "csv":
            if item.get("rows_pointer") is not None or any(not isinstance(x, str) or not x for x in mapping.values()):
                raise ValueError("CSV primary needs column-name field map")
            reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"), newline=""))
            if (not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames))
                    or any(field not in reader.fieldnames for field in mapping.values())):
                raise ValueError("CSV primary columns differ from declared field map")
            rows = list(reader)
            if any(None in row for row in rows):
                raise ValueError("CSV primary row has extra columns")
        else:
            pointer = item.get("rows_pointer")
            if not isinstance(pointer, str) or not pointer.startswith("/"):
                raise ValueError("JSON primary needs an absolute rows_pointer")
            rows = json_pointer(checked_object(root, ref, "raw primary JSON"), pointer)
            if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
                    or any(not isinstance(x, str) or not x.startswith("/") for x in mapping.values())):
                raise ValueError("JSON primary needs array rows and row-relative field pointers")
        if len(rows) > 100_000:
            raise ValueError("Raw primary row count exceeds bounded parser size")

        def field(row, role):
            if role not in mapping:
                return None
            return row[mapping[role]] if fmt == "csv" else json_pointer(row, mapping[role])

        def count(value):
            if value is None or value == "":
                return None
            if fmt == "csv":
                if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]*", value):
                    raise ValueError("CSV raw count must be a nonnegative integer or blank")
                return int(value)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("JSON raw count must be a nonnegative integer or null")
            return value

        parsed = {}
        for row in rows:
            row_id, domain = field(row, "source_row_id"), field(row, "domain")
            if not isinstance(row_id, str) or not row_id or not isinstance(domain, str) or not domain:
                raise ValueError("Raw primary row needs ID and domain")
            if row_id in parsed:
                raise ValueError("Duplicate primary source row ID")
            parsed[row_id] = {"domain": domain,
                **{metric: count(field(row, metric)) for metric in ("raw_referring_domains", "raw_backlinks")
                   if metric in mapping},
                **{role: field(row, role) for role in ("provider", "date", "scope") if role in mapping}}
        excluded_ids = set()
        for exclusion in excluded:
            if not isinstance(exclusion, dict):
                raise ValueError("Excluded primary row needs source-backed reason")
            row_id = exclusion.get("source_row_id")
            if (not isinstance(row_id, str) or not row_id or row_id in excluded_ids
                    or not isinstance(exclusion.get("reason"), str) or not exclusion["reason"].strip()):
                raise ValueError("Excluded primary row needs unique ID and reason")
            saved_file(exclusion.get("evidence_ref"), exclusion.get("evidence_sha256"), "Excluded primary row evidence")
            excluded_ids.add(row_id)
        if (any(not isinstance(row_id, str) or not row_id for row_id in included)
                or len(included) != len(set(included))
                or set(included) & excluded_ids or set(parsed) != set(included) | excluded_ids):
            raise ValueError("Primary source row coverage differs from included/excluded IDs")
        for row_id in included:
            key = (row_ref, row_id)
            if key in claimed_normalized:
                raise ValueError("Normalized primary row claimed by multiple sources")
            claimed_normalized.add(key)
            norm = normalized.get(key)
            if norm is None or norm.get("domain") != parsed[row_id]["domain"]:
                raise ValueError("Primary source identity/domain differs from normalized row")
            for role, value in parsed[row_id].items():
                if role == "domain":
                    continue
                if norm.get(role) != value:
                    raise ValueError("Primary source field " + role + " differs from normalized row")
            for metric in ("raw_referring_domains", "raw_backlinks"):
                if metric not in mapping and norm.get(metric) is not None:
                    raise ValueError("Normalized raw metric lacks a primary field map")

    if declared_raw_refs != source_refs:
        raise ValueError("Declared raw_source_refs differ from primary/supporting source inventory")
    raw_normalized = {key for key, row in normalized.items()
        if row.get("raw_referring_domains") is not None or row.get("raw_backlinks") is not None}
    if raw_normalized and not primary:
        raise ValueError("Known raw link rows need a primary source declaration")
    # Every verified row is exact; unverified/unsupported rows may remain only in a partial GAP.
    verified_refs = {item.get("row_source_ref") for item in primary if item.get("status") == "verified"}
    if not raw_normalized.issubset(claimed_normalized | {key for key in raw_normalized if key[0] not in verified_refs}):
        raise ValueError("Normalized raw link row is absent from verified primary coverage")


def client_bar_line(row):
    source = f"[Отчёт БАР]({row['report_ref']})" if row["status"] == "scored" else f"[Основание]({row['evidence_ref']})"
    metric = row.get("display_metric")
    display = (f"{metric['path']}: {metric['value']}; метод {row['method']}; peer {row['peer_context']}; "
               f"шкала {row['scale']}; уверенность {row['confidence']}") if metric is not None else "Без числовой оценки"
    return "| " + " | ".join(client_cell(value) for value in
        (row["type_id"], row.get("url") or "Будущая страница", row["status"],
         row.get("finding") or row.get("reason"), row.get("action") or "Работа не назначена",
         display, source, row["limitation"])) + " |"


class ClientMediaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.media = set()

    def handle_starttag(self, tag, attrs):
        if tag in {"img", "embed", "object"}:
            properties = dict(attrs)
            for key in ("src", "data"):
                if properties.get(key):
                    self.media.add(properties[key])


def client_media_present(document, suffix, ref):
    if suffix == ".html":
        parser = ClientMediaParser()
        parser.feed(document)
        return ref in parser.media
    if suffix == ".md":
        import re
        return any(match.group(1).split()[0] == ref for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", document))
    return False


def client_evidence_checks(root, state, handoff, appendix, packet_path, files):
    """Check exact packet evidence closure; factual suitability remains a reviewer judgment."""
    packet_root = Path(packet_path)
    def packet_file(ref):
        if (not isinstance(ref, str) or not ref.startswith("client-evidence/")
                or ".." in Path(ref).parts or ref not in files):
            raise ValueError("Client evidence must be a packet-relative file in exact manifest")
        target = relative_file(root, str(packet_root / ref))
        if not target.is_relative_to((root / packet_root).resolve()):
            raise ValueError("Client evidence leaves exact packet")
        if sha_file(target) != files[ref] or not target.read_bytes().strip():
            raise ValueError("Client evidence file is missing, empty or changed")
        return target

    documents = {}
    for suffix in (".md", ".html"):
        name = "proposal" + suffix
        if name in files:
            documents[name] = relative_file(root, str(packet_root / name)).read_text(encoding="utf-8-sig")
    if "proposal.md" not in documents:
        raise ValueError("Client claims need a shipped proposal.md")
    visible = "\n".join(documents.values()) + "\n" + appendix
    claims = handoff.get("material_claims")
    if not isinstance(claims, list) or not claims:
        raise ValueError("Client material claims need an explicit observed/unknown inventory")
    ids = set()
    for claim in claims:
        if not isinstance(claim, dict) or not all(isinstance(claim.get(key), str) and claim[key].strip() for key in
                ("claim_id", "visible_quote", "unit", "period", "scope")):
            raise ValueError("Client material claim needs ID, visible quote, unit, period and scope")
        ident = claim["claim_id"]
        if ident in ids or claim["visible_quote"] not in visible or client_claim_line(claim) not in appendix:
            raise ValueError("Client material claim is duplicated or absent from readable packet")
        ids.add(ident)
        status = claim.get("status")
        if status == "unknown":
            if (claim.get("value") is not None or not claim.get("reason")
                    or claim.get("source_table_ref") is not None or claim.get("row_ids") not in (None, [])):
                raise ValueError("Unknown client claim must be null with a reason, never an invented zero")
            continue
        if status not in {"observed", "estimated"} or not all(claim.get(key) for key in
                ("source_system", "source_table_ref", "limitation", "calculation")):
            raise ValueError("Observed/estimated client claim needs a compact source table and calculation")
        if status == "observed" and claim.get("chart_fitness") not in {"suitable", "unsuitable"}:
            raise ValueError("Observed client claim needs a chart fitness assessment")
        if claim.get("chart_fitness") == "unsuitable" and not claim.get("chart_fitness_reason"):
            raise ValueError("Unsuitable client data needs a chart fitness reason")
        if status == "estimated" and not claim.get("basis"):
            raise ValueError("Estimated claim needs an explicit basis")
        if type(claim.get("value")) not in (int, float) or not math.isfinite(claim["value"]):
            raise ValueError("Client numeric claim needs a finite value")
        table = read_json(packet_file(claim["source_table_ref"]))
        rows = table.get("rows") if isinstance(table, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ValueError("Client source table needs saved rows")
        by_id = {r.get("row_id"): r for r in rows if isinstance(r, dict)}
        if len(by_id) != len(rows):
            raise ValueError("Client source table row IDs must be unique")
        row_ids = claim.get("row_ids")
        if not isinstance(row_ids, list) or not row_ids or len(row_ids) != len(set(row_ids)) or not set(row_ids) <= set(by_id):
            raise ValueError("Client claim cites missing or repeated source row IDs")
        values = []
        operation = claim["calculation"]
        ratio = isinstance(operation, dict) and operation.get("op") == "ratio"
        for row_id in row_ids:
            row = by_id[row_id]
            shared = ("period", "scope", "source_system") if ratio else ("unit", "period", "scope", "source_system")
            if any(row.get(key) != claim[key] for key in shared) or not row.get("unit") or not row.get("limitation") or not row.get("origin_ref"):
                raise ValueError("Client source row differs in unit, period, scope or provenance")
            if type(row.get("value")) not in (int, float) or not math.isfinite(row["value"]):
                raise ValueError("Client source row needs a finite number")
            origin = row.get("origin")
            if not isinstance(origin, dict):
                raise ValueError("Client numeric row needs a primary origin, not only an author table")
            if origin.get("status") == "unavailable":
                if (status != "estimated" or not origin.get("reason") or not origin.get("claim_limit")
                        or origin["claim_limit"] not in visible):
                    raise ValueError("Unavailable numeric origin needs an estimated claim and visible limit")
            elif origin.get("status") == "available":
                if (origin.get("mode") not in {"copy", "projection"}
                        or not all(origin.get(key) for key in ("source_ref", "source_sha256", "packet_ref",
                                                             "value_pointer", "period_pointer"))
                        or not isinstance(origin.get("row_pointer"), str)
                        or not isinstance(origin.get("filter"), dict) or not origin["filter"]):
                    raise ValueError("Numeric origin needs saved source, selectors, filter and packet ref")
                if origin["row_pointer"] == "" and any(len(json_pointer_segments(pointer)) != 1
                        for pointer in (origin["value_pointer"], origin["period_pointer"], *origin["filter"])):
                    raise ValueError("Root numeric row must use direct object fields, not nested source rows")
                source_ref = origin["source_ref"]
                if Path(source_ref).parts[0] in {"client-packet", "client-evidence"}:
                    raise ValueError("Author-prepared packet table cannot be the primary numeric origin")
                saved = relative_file(root, source_ref)
                if saved.suffix.lower() != ".json" or sha_file(saved) != origin["source_sha256"]:
                    raise ValueError("Numeric primary JSON source is missing or changed")
                primary = read_json(saved)
                raw_row = json_pointer(primary, origin["row_pointer"])
                if not pointer_descends(origin["value_pointer"], origin["row_pointer"]):
                    raise ValueError("Numeric value selector must descend from the selected primary row")
                shared = origin.get("source_level_metadata")
                if shared is not None:
                    if (not isinstance(shared, dict) or not isinstance(shared.get("context_pointer"), str)
                            or not isinstance(shared.get("filter_pointers"), list)
                            or any(not isinstance(pointer, str) for pointer in shared["filter_pointers"])):
                        raise ValueError("Numeric shared metadata needs a declared context and filter roles")
                    context_pointer = shared["context_pointer"]
                    context = json_pointer(primary, context_pointer)
                    if (not isinstance(context, dict) or context_pointer == origin["row_pointer"]
                            or pointer_descends(context_pointer, origin["row_pointer"])):
                        raise ValueError("Numeric shared metadata must be outside the selected primary row")
                    row_parent_pointer = origin["row_pointer"].rsplit("/", 1)[0]
                    row_parent = primary if not row_parent_pointer else json_pointer(primary, row_parent_pointer)
                    if isinstance(row_parent, list) and (
                            context_pointer == row_parent_pointer or pointer_descends(context_pointer, row_parent_pointer)):
                        raise ValueError("Numeric shared metadata cannot borrow a different row")
                row_parent_pointer = origin["row_pointer"].rsplit("/", 1)[0]
                def same_array_ancestors(pointer):
                    row_parts = json_pointer_segments(origin["row_pointer"])
                    selected_parts = json_pointer_segments(pointer)
                    current = primary
                    for depth, token in enumerate(row_parts):
                        if (isinstance(current, list) and len(selected_parts) > depth
                                and selected_parts[:depth] == row_parts[:depth]
                                and selected_parts[depth] != token):
                            return False
                        current = current[int(token)] if isinstance(current, list) else current[token]
                    return True
                def row_or_shared(pointer, role):
                    if pointer_descends(pointer, origin["row_pointer"]):
                        return True
                    if (not shared or not pointer_descends(pointer, shared["context_pointer"])
                            or not same_array_ancestors(pointer)
                            or (isinstance(row_parent, list) and
                                (pointer == row_parent_pointer or pointer_descends(pointer, row_parent_pointer)))):
                        return False
                    return (shared.get("period_pointer") == pointer if role == "period"
                            else pointer in shared["filter_pointers"])
                if not row_or_shared(origin["period_pointer"], "period") or any(
                        not row_or_shared(pointer, "filter") for pointer in origin["filter"]):
                    raise ValueError("Numeric period/filter selectors must belong to the row or declared shared metadata")
                raw_value = json_pointer(primary, origin["value_pointer"])
                raw_period = json_pointer(primary, origin["period_pointer"])
                if (not isinstance(raw_row, dict) or type(raw_value) not in (int, float)
                        or not math.isfinite(raw_value) or raw_value != row["value"]
                        or raw_period != row["period"]):
                    raise ValueError("Numeric source row value or period differs from primary saved data")
                for pointer, expected_value in origin["filter"].items():
                    if json_pointer(primary, pointer) != expected_value:
                        raise ValueError("Numeric source filter differs from primary saved data")
                candidate = packet_file(origin["packet_ref"])
                if origin["mode"] == "copy":
                    if candidate.read_bytes() != saved.read_bytes():
                        raise ValueError("Copied numeric primary source differs from saved data")
                else:
                    projection = {"source_ref": source_ref, "source_sha256": origin["source_sha256"],
                                  "row_pointer": origin["row_pointer"], "value": raw_value,
                                  "period": raw_period,
                                  "value_pointer": origin["value_pointer"], "period_pointer": origin["period_pointer"],
                                  "filter": origin["filter"]}
                    if shared is not None:
                        projection["source_level_metadata"] = shared
                    if canonical(read_json(candidate)) != canonical(projection):
                        raise ValueError("Numeric packet projection differs from selected primary row")
            else:
                raise ValueError("Numeric origin must be available or unavailable")
            if client_numeric_origin_line(row) not in appendix:
                raise ValueError("Numeric primary origin or its limitation is absent from readable appendix")
            values.append(row["value"])
        operations = {"identity": lambda v: v[0] if len(v) == 1 else None,
                      "sum": sum, "count": len, "mean": lambda v: sum(v) / len(v)}
        if ratio:
            numerator = operation.get("numerator_row_ids")
            denominator = operation.get("denominator_row_ids")
            scale = operation.get("scale")
            if (not isinstance(numerator, list) or not numerator or not isinstance(denominator, list) or not denominator
                    or set(numerator) & set(denominator) or set(numerator) | set(denominator) != set(row_ids)
                    or len(numerator) + len(denominator) != len(row_ids) or scale not in {1, 100}
                    or claim["unit"] != ("ratio" if scale == 1 else "percent")
                    or len({by_id[identifier]["unit"] for identifier in row_ids}) != 1
                    or any(by_id[identifier]["value"] < 0 for identifier in row_ids)):
                raise ValueError("Client ratio needs disjoint source rows with a common denominator unit")
            denominator_value = sum(by_id[identifier]["value"] for identifier in denominator)
            calculated = scale * sum(by_id[identifier]["value"] for identifier in numerator) / denominator_value if denominator_value > 0 else None
        else:
            calculated = operations[operation](values) if isinstance(operation, str) and operation in operations else None
        if calculated is None or not math.isclose(claim["value"], calculated, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("Client claim does not reconcile with saved source rows")
        if claim["source_table_ref"] not in appendix:
            raise ValueError("Client source table is not linked in readable appendix")

    visuals = handoff.get("visuals")
    if not isinstance(visuals, list) or not visuals:
        raise ValueError("Client packet needs an explicit chart disposition")
    visual_ids = set()
    for visual in visuals:
        if not isinstance(visual, dict) or not visual.get("visual_id") or visual["visual_id"] in visual_ids:
            raise ValueError("Client visual IDs must be unique")
        visual_ids.add(visual["visual_id"])
        if visual.get("status") == "not_applicable":
            if (visual.get("data_fitness") not in {"unavailable", "unsuitable"} or not visual.get("reason")
                    or any(c.get("status") == "observed" and c.get("chart_fitness") == "suitable" for c in claims)):
                raise ValueError("Suitable available client data cannot be hidden as chart N/A")
            if any(visual["reason"] not in body for body in documents.values()):
                raise ValueError("Chart N/A reason must be visible in every shipped client format")
            continue
        if visual.get("status") != "embedded" or not visual.get("caption") or not visual.get("chart_ref"):
            raise ValueError("Client chart needs an embedded file or reasoned N/A")
        if not set(visual.get("source_claim_ids", [])) <= ids or not visual.get("source_claim_ids"):
            raise ValueError("Client chart must cite material claims")
        if any(next(c for c in claims if c["claim_id"] == cid)["status"] == "unknown" for cid in visual["source_claim_ids"]):
            raise ValueError("Client chart cannot plot unknown values")
        chart = packet_file(visual["chart_ref"])
        if chart.suffix.lower() not in {".png", ".svg", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("Client chart must be an image")
        for name, body in documents.items():
            if not client_media_present(body, Path(name).suffix, visual["chart_ref"]) or visual["caption"] not in body:
                raise ValueError("Client chart and caption must be embedded in every shipped format")
        if not all(visual.get(key) and str(visual[key]) in visual["caption"] for key in ("unit", "period", "source_label", "limitation")):
            raise ValueError("Client chart caption needs unit, period, source and limitation")

    register = previous_output(root, state, "text_bar", "text_register")
    bar_rows = handoff.get("bar_summary")
    coverage = register["scope_coverage"]
    if (not isinstance(bar_rows, list) or len(bar_rows) != len(coverage)
            or {r.get("scope_id") for r in bar_rows if isinstance(r, dict)} != {r["scope_id"] for r in coverage}):
        raise ValueError("Client BAR summary must cover every material text scope")
    by_scope = {r["scope_id"]: r for r in coverage}
    dispositions = ledger_projection(previous_output(root, state, "strategy", "strategy_map"))
    for item in bar_rows:
        source = by_scope[item["scope_id"]]
        if source["status"] == "pending":
            pending_rows = [r for r in register["rows"] if r.get("mode") == "pending"
                            and r.get("scope_id") == item["scope_id"]]
            pending_items = [p for p in register.get("pending_items", [])
                             if p.get("scope_kind") == "task" and p.get("scope_id") == item["scope_id"]]
            if len(pending_rows) != 1 or len(pending_items) != 1 or source.get("row_index") is not None:
                raise ValueError("Client pending BAR needs one exact pending row and reason")
            row, pending = pending_rows[0], pending_items[0]
        else:
            index = source.get("row_index")
            if type(index) is not int or index < 0 or index >= len(register["rows"]):
                raise ValueError("Client BAR needs a valid saved row index")
            row = register["rows"][index]
        if item.get("status") != source["status"] or item.get("type_id") != row["type_id"] or item.get("url") != row.get("url") or not item.get("limitation"):
            raise ValueError("Client BAR applicability differs from saved text analysis")
        if item["status"] == "pending":
            reason = pending.get("reason")
            if (not reason or item.get("reason") != reason or reason not in item["limitation"]
                    or not item.get("finding") or item.get("action") != row.get("change")
                    or not item.get("action")
                    or any(item.get(k) is not None for k in ("display_metric", "report_ref", "input_ref", "own_score"))):
                raise ValueError("Client pending BAR needs saved reason/action and no report or score")
            pins = dict(register["files"])
            attempts = state.get("attempts", {}).get("text_bar", [])
            if attempts:
                pins.update({p["path"]: p["sha256"] for p in attempts[-1].get("consumed_sources", [])})
            allowed = ([row["own_text_ref"]] if row.get("own_text_ref") else
                       [*row.get("peer_text_refs", []), pending.get("source_ref")])
            proof_sha = sha_file(packet_file(item.get("evidence_ref")))
            if not any(ref in pins and pins[ref] == proof_sha for ref in allowed):
                raise ValueError("Client pending BAR evidence differs from saved scope proof")
        elif item["status"] == "not_applicable":
            if item.get("reason") != source["reason"] or item.get("display_metric") is not None:
                raise ValueError("Client BAR N/A needs exact reason and no score")
            proof = item.get("evidence_ref")
            if not any(sha_file(packet_file(proof)) == register["files"][ref]
                       for ref in row["applicability"]["evidence_files"]):
                raise ValueError("Client BAR N/A proof differs from source")
        else:
            if not all(item.get(key) for key in ("finding", "action", "report_ref")):
                raise ValueError("Client BAR needs a concrete textual finding, action and report")
            if sha_file(packet_file(item["report_ref"])) != register["files"][row["adapter_report_ref"]]:
                raise ValueError("Client BAR report differs from current native result")
            reading = source["reading"]
            disposition = dispositions[f"bar_scope:{item['scope_id']}"]
            expected_action = (disposition["source_change"] if disposition["disposition"] == "selected"
                               else reading["text_disposition"])
            if (item["finding"] != reading["note"] or item.get("source_pointer") != reading["source_pointer"]
                    or reading["applicability"] not in item["limitation"] or item["action"] != expected_action):
                raise ValueError("Client BAR line differs from the scoped reading and strategy decision")
            pointer = reading["source_pointer"]
            if pointer["kind"] == "saved_text":
                passage_ref = item.get("passage_ref")
                if (not isinstance(passage_ref, str) or not passage_ref.startswith("client-evidence/")
                        or sha_file(packet_file(passage_ref)) != register["files"][pointer["file_ref"]]
                        or pointer["fragment"] not in packet_file(passage_ref).read_text(encoding="utf-8-sig")):
                    raise ValueError("Client BAR saved passage is absent, stale or incomplete")
            elif item.get("passage_ref") is not None:
                raise ValueError("Native BAR report reading cannot claim an unrelated saved passage")
            metric = item.get("display_metric")
            if metric is not None:
                if not isinstance(metric, dict) or not isinstance(metric.get("path"), str) or not metric["path"]:
                    raise ValueError("Displayed BAR metric needs a native path")
                actual = read_json(packet_file(item["report_ref"]))
                source_input = checked_object(root, row["input_ref"], "BAR source input")
                peer_urls = {peer.get("url") for peer in source_input.get("peers", []) if isinstance(peer, dict)}
                value = actual
                for part in metric.get("path", "").split("."):
                    value = value.get(part) if isinstance(value, dict) else None
                if (not metric["path"].startswith("own_score.") or row.get("own_score") is None
                        or type(value) not in (int, float) or not math.isfinite(value) or metric.get("value") != value
                        or item.get("method") != "BAR" or not item.get("scale")
                        or item.get("confidence") != actual.get("coverage", {}).get("confidence")
                        or not peer_urls or not all(url in item.get("peer_context", "") for url in peer_urls)):
                    raise ValueError("Displayed BAR number needs native metric and method/peer/scale/confidence")
        if client_bar_line(item) not in appendix:
            raise ValueError("Client BAR finding is absent from readable appendix")


def measured_value_ok(value, unit, value_kind=None):
    """Counts are integers; continuous measurements must be finite."""
    if unit in COUNT_MEASUREMENT_UNITS:
        return value_kind in (None, "count") and type(value) is int and value >= 0
    if (unit == "revenue" and value_kind not in (None, "continuous")) or (unit != "revenue" and value_kind != "continuous"):
        return False
    return (type(value) in (int, float) and value >= 0
            and (type(value) is int or math.isfinite(value)))


def measurement_checks(plan):
    sources_by_unit = {
        "unique_confirmed_inquiry": {"crm", "call_log", "verified_form"},
        "confirmed_order": {"crm", "order_ledger"},
        "revenue": {"accounting", "order_ledger"},
        "commercial_search_clicks": {"gsc", "yandex_webmaster", "web_analytics"},
    }
    if not isinstance(plan, dict) or plan.get("outcome_unit") not in sources_by_unit or not plan.get("event_definition"):
        raise ValueError("Measurement plan needs a defined primary unit and event")
    if plan["outcome_unit"] != "unique_confirmed_inquiry" and not plan.get("metric_rationale"):
        raise ValueError("Alternative primary metric needs a project-specific rationale")
    if plan.get("source_status") not in {"available", "unavailable"}:
        raise ValueError("Measurement source availability is required")
    if plan["source_status"] == "available" and (plan.get("source_kind") not in sources_by_unit[plan["outcome_unit"]] or not plan.get("source_ref")):
        raise ValueError("Available primary metric needs a suitable saved source; a click goal is not a confirmed lead")
    if plan["source_status"] == "unavailable" and (plan.get("source_kind") != "unknown" or plan.get("source_ref")):
        raise ValueError("Unavailable primary source cannot claim measured provenance")
    if not plan.get("period") or not plan.get("dedup_rule") or plan.get("comparison_periods") != ["B3", "B6", "B12"]:
        raise ValueError("Measurement needs period, deduplication and B3/B6/B12 comparisons")
    baseline = plan.get("baseline")
    if not isinstance(baseline, dict) or baseline.get("status") not in {"known", "unknown"}:
        raise ValueError("Measurement baseline needs known or unknown status")
    if baseline["status"] == "unknown":
        if baseline.get("value") is not None or baseline.get("source_ref") is not None or not baseline.get("reason"):
            raise ValueError("Unknown baseline must be null with a reason")
    elif (plan["source_status"] != "available"
          or not measured_value_ok(baseline.get("value"), plan["outcome_unit"])
          or not baseline.get("period") or baseline.get("source_ref") != plan.get("source_ref")):
        raise ValueError("Known baseline needs a finite value matching its unit, period and source")
    proxy = plan.get("proxy")
    if plan["source_status"] == "unavailable" and not isinstance(proxy, dict):
        raise ValueError("Unavailable primary source needs an explicit proxy status")
    if proxy is not None:
        if not isinstance(proxy, dict) or proxy.get("status") not in {"observed", "planned", "unavailable"} or proxy.get("is_unique_inquiry") is not False or not proxy.get("limitation"):
            raise ValueError("Proxy needs status and cannot be presented as unique inquiries")
        if proxy["status"] == "observed":
            value = proxy.get("observed_value")
            if (not proxy.get("unit") or not proxy.get("event") or not proxy.get("source_ref")
                    or not proxy.get("source_kind") or not proxy.get("period") or not proxy.get("dedup_rule")
                    or (proxy["unit"] not in COUNT_MEASUREMENT_UNITS and proxy.get("value_kind") != "continuous")
                    or not measured_value_ok(value, proxy["unit"], proxy.get("value_kind"))):
                raise ValueError("Observed proxy needs a real source and finite value matching its unit")
        elif (proxy.get("source_ref") is not None or proxy.get("source_kind") is not None or proxy.get("observed_value") is not None
              or not proxy.get("reason") or (proxy["status"] == "planned" and (not proxy.get("unit") or not proxy.get("event")))
              or (proxy["status"] == "unavailable" and (proxy.get("unit") is not None or proxy.get("event") is not None))):
            raise ValueError("Planned/unavailable proxy cannot claim an observed source or value")
    verification = plan.get("verification")
    if not isinstance(verification, dict) or not all(verification.get(key) for key in ("owner_role", "method", "cadence")):
        raise ValueError("Measurement needs an owner and verification method/cadence")
    if plan["outcome_unit"] == "unique_confirmed_inquiry":
        protocol = plan.get("qualification_protocol")
        if not isinstance(protocol, dict):
            raise ValueError("Confirmed inquiry needs a qualification protocol")
        rules = protocol.get("decision_rules")
        if (not isinstance(rules, dict) or not isinstance(rules.get("include"), str)
                or not rules["include"].strip() or not isinstance(rules.get("exclude"), list)
                or not rules["exclude"] or any(not isinstance(x, str) or not x.strip()
                                                  for x in rules["exclude"])):
            raise ValueError("Qualification needs inclusion and exclusion rules")
        record = protocol.get("record")
        required = {"received_at", "intake_path", "service_or_problem", "contact_key",
                    "qualification_decision", "decision_reason", "dedup_group", "evidence_id"}
        if (not isinstance(record, dict) or not isinstance(record.get("fields"), list)
                or any(not isinstance(x, str) for x in record["fields"])
                or not required <= set(record["fields"])
                or not isinstance(record.get("decision_values"), list)
                or any(not isinstance(x, str) for x in record["decision_values"])
                or set(record["decision_values"]) != {"qualified", "excluded", "unknown"}
                or record.get("personal_data_in_packet") is not False
                or not isinstance(record.get("storage"), str) or not record["storage"].strip()):
            raise ValueError("Qualification needs safe minimum record roles and tri-state decision")
        paths, tests = protocol.get("intake_paths"), protocol.get("path_tests")
        if (not isinstance(paths, list) or not paths or any(not isinstance(x, str) or not x.strip() for x in paths)
                or len(paths) != len(set(paths)) or not isinstance(tests, list)
                or {x.get("intake_path") for x in tests if isinstance(x, dict)} != set(paths)
                or len(tests) != len(paths)
                or any(not isinstance(x, dict) or not isinstance(x.get("method"), str) or not x["method"].strip()
                       or not isinstance(x.get("expected_record"), str) or not x["expected_record"].strip()
                       for x in tests)):
            raise ValueError("Qualification needs a received-event test for each chosen intake path")
    # Existing .20 runs may omit this .21 field. A present list validates declared
    # promises; a human reviewer still checks that prose did not omit a segment.
    if "promised_segments" in plan:
        segments = plan["promised_segments"]
        if not isinstance(segments, list):
            raise ValueError("Promised outcome segments must be a list")
        seen = set()
        for segment in segments:
            if not isinstance(segment, dict) or not isinstance(segment.get("segment_id"), str) or not segment["segment_id"].strip() or segment["segment_id"] in seen:
                raise ValueError("Promised outcome segment needs a unique ID")
            seen.add(segment["segment_id"])
            if segment.get("status") == "planned":
                fields = ("capture", "persist", "join", "unknown_bucket", "test")
                if (type(segment.get("committed")) is not bool
                        or any(not isinstance(segment.get(key), str) or not segment[key].strip() for key in fields)
                        or not isinstance(segment.get("dependencies"), list)
                        or not segment["dependencies"]
                        or any(not isinstance(item, str) or not item.strip() for item in segment["dependencies"])):
                    raise ValueError("Planned outcome segment needs capture, persistence, join, unknown bucket, test and dependencies")
            elif segment.get("status") == "unavailable":
                if segment.get("committed") is not False or not isinstance(segment.get("reason"), str) or not segment["reason"].strip():
                    raise ValueError("Unavailable outcome segment cannot be committed and needs a reason")
            else:
                raise ValueError("Promised outcome segment status must be planned or unavailable")


def measurement_receipt_checks(root, plan):
    """Bind each observed metric to a saved receipt and a separate hashed source file."""
    refs = set()
    def check(receipt_ref, unit, event, period, dedup, value, source_kind, label):
        receipt = checked_object(root, receipt_ref, label + " receipt")
        source_ref = receipt.get("source_artifact_ref")
        if (not isinstance(source_ref, str) or source_ref == receipt_ref
                or receipt.get("source_artifact_sha256") != sha_file(relative_file(root, source_ref))
                or not relative_file(root, source_ref).read_bytes().strip()):
            raise ValueError(label + " needs a nonempty saved source artifact with matching SHA-256")
        expected = {"unit": unit, "event_definition": event, "period": period,
                    "dedup_rule": dedup, "observed_value": value, "source_kind": source_kind}
        if ((value is None and receipt.get("observed_value") is not None)
                or (value is not None and not measured_value_ok(receipt.get("observed_value"), unit,
                                                                  plan.get("proxy", {}).get("value_kind") if label == "Proxy measurement" else None))
                or any(receipt.get(key) != item for key, item in expected.items())
                or not receipt.get("captured_at") or not receipt.get("scope")
                or receipt.get("status") != ("observed" if value is not None else "available_no_baseline")):
            raise ValueError(label + " receipt differs from unit, event, period, dedup or observed value")
        refs.update((receipt_ref, source_ref))
    if plan["source_status"] == "available":
        baseline = plan["baseline"]
        check(plan["source_ref"], plan["outcome_unit"], plan["event_definition"],
              baseline.get("period") if baseline["status"] == "known" else plan["period"],
              plan["dedup_rule"], baseline.get("value"), plan["source_kind"], "Primary measurement")
    proxy = plan.get("proxy")
    if isinstance(proxy, dict) and proxy.get("status") == "observed":
        check(proxy["source_ref"], proxy["unit"], proxy["event"], proxy["period"],
              proxy["dedup_rule"], proxy["observed_value"], proxy["source_kind"], "Proxy measurement")
    return sorted(refs)


def estimate_line_checks(implementation, estimate):
    """Resolve each estimate reference to unique selected actions and their units."""
    actions = {row["action_id"]: row for row in implementation["actions"]}
    lines = estimate.get("lines")
    if not isinstance(lines, list) or not lines:
        raise ValueError("Estimate needs addressable work lines")
    by_ref, allocated = {}, set()
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("estimate_ref"), str) or not line["estimate_ref"].strip() or line["estimate_ref"] in by_ref or not line.get("basis"):
            raise ValueError("Estimate work line needs unique reference and basis")
        action_ids = names(line.get("action_ids"), "estimate line action IDs")
        if action_ids & allocated or not action_ids <= set(actions):
            raise ValueError("Estimate work line duplicates or invents an action")
        unit = line.get("unit")
        if not isinstance(unit, str) or any(actions[action_id]["unit"] != unit for action_id in action_ids):
            raise ValueError("Estimate work line unit differs from implementation")
        quantity = sum(actions[action_id]["quantity"] for action_id in action_ids)
        phases = {}
        for action_id in action_ids:
            action = actions[action_id]
            phases[action["phase"]] = phases.get(action["phase"], 0) + action["quantity"]
        if (isinstance(line.get("quantity"), bool) or line.get("quantity") != quantity
                or line.get("phase_quantities") != phases):
            raise ValueError("Estimate work line quantity or phase differs from implementation")
        scopes = line.get("action_scopes")
        if not isinstance(scopes, dict) or set(scopes) != action_ids or any(
                scopes[action_id] != actions[action_id].get("work_scope") for action_id in action_ids):
            raise ValueError("Estimate line needs each action's exact bounded work scope")
        allocated.update(action_ids)
        by_ref[line["estimate_ref"]] = line
    if allocated != set(actions):
        raise ValueError("Estimate work lines omit selected actions")
    return by_ref


def release_contract_context(root, state):
    """Only selected work, its existing URL targets, and its estimate joins."""
    strategy = previous_output(root, state, "strategy", "strategy_map")
    implementation = previous_output(root, state, "implementation", "implementation_plan")
    estimate = previous_output(root, state, "estimate", "estimate_sheet")
    gap = previous_output(root, state, "gap", "gap_register")
    actions = {row["action_id"]: row for row in implementation["actions"]}
    lines = estimate_line_checks(implementation, estimate)
    line_by_action = {action_id: ref for ref, line in lines.items() for action_id in line["action_ids"]}
    current_urls = {row["url"] for row in gap["page_allocations"]}
    existing_actions = {action_id for action_id, action in actions.items()
                        if set(action.get("affected_urls") or []) & current_urls}
    sources_by_action = {action_id: set() for action_id in actions}
    for row in strategy["decision_ledger"]:
        if row["disposition"] == "selected" and row["action_id"] in sources_by_action:
            sources_by_action[row["action_id"]].add(row["source_id"])
    return actions, line_by_action, existing_actions, sources_by_action


def release_contract_scaffold(root, state):
    """A non-recordable view of selected references; no release choice is preapproved."""
    actions, line_by_action, existing, sources = release_contract_context(root, state)
    checkpoint_rows = []
    for phase in ("B6", "B12"):
        ids = sorted(action_id for action_id, action in actions.items() if action["phase"] == phase)
        checkpoint_rows.append({"checkpoint_id": "", "phase": phase, "commercial_model": "",
                                "action_ids": ids, "estimate_refs": sorted({line_by_action[action_id] for action_id in ids}),
                                "outside_kind": None, "scope": "", "acceptance": "", "price_boundary": ""})
    return {"selection_inventory": [{"action_id": action_id, "unit": action["unit"],
                                      "quantity": action["quantity"], "phase": action["phase"],
                                      "estimate_ref": line_by_action[action_id],
                                      "existing_url_target": action_id in existing}
                                     for action_id, action in sorted(actions.items())],
            "release_contract": {
                "schema_version": 3,
                "first_release": {"plan_status": "", "included_action_ids": [], "interim_acceptance": "",
                                  "reason": "", "interim_action_ids": [], "remaining_work": [],
                                  "existing_url_actions": [
                                      {"action_id": action_id, "source_ids": sorted(sources[action_id]),
                                       "disposition": "", "reason": ""} for action_id in sorted(existing)]},
                "current_release_gate": {"status": "", "reason": ""},
                "conditional_acceptance_paths": [],
                "future_checkpoints": checkpoint_rows,
            },
            "recordable": False}


def scope_handoff_scaffold(root, state):
    """Non-recordable joins for the brief and declared checkpoint purchase boundary."""
    actions, _, _, _ = release_contract_context(root, state)
    strategy = previous_output(root, state, "strategy", "strategy_map")
    audit = previous_output(root, state, "audit", "audit_findings")
    findings = {row["finding_id"]: row for row in audit.get("findings", [])}
    rows = []
    for item in strategy["decision_ledger"]:
        if item["disposition"] != "selected" or not item["source_id"].startswith("audit:"):
            continue
        finding_id = item["source_id"][6:]
        finding = findings[finding_id]
        action = actions[item["action_id"]]
        rows.append({"finding_id": finding_id, "source_ref": finding["source_ref"],
                     "source_date_status": "", "finding_or_limit": "", "urls_or_group": finding.get("url") or "",
                     "scale": "", "action_id": item["action_id"], "phase": action["phase"],
                     "acceptance": ""})
    return {"structure_outline": {"section_ids": ["A", "GAP", "SEO", "B3", "B6", "B12", "implementation", "team", "estimate", "company_cases"],
                                   "section_briefs_ref": "", "section_briefs_sha256": "", "seo_state_rows": rows},
            "checkpoint_deliverables": [{"phase": phase, "commercial_model": "", "operation_action_id": None,
                                         "estimate_ref": None, "source_panel": "", "period_method": "",
                                         "finite_receipt": "", "role": "", "acceptance": ""}
                                        for phase in ("B3", "B6", "B12")],
            "recordable": False}


def release_contract_checks(root, state, data):
    contract = data.get("release_contract")
    if not isinstance(contract, dict) or set(contract) != {"schema_version", "first_release", "current_release_gate", "conditional_acceptance_paths", "future_checkpoints"} or contract["schema_version"] != 3:
        raise ValueError("Decision map needs versioned release_contract")
    actions, line_by_action, existing, sources = release_contract_context(root, state)
    selected = set(actions)
    def text_field(row, key, label):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise ValueError(f"{label} needs {key}")
    first = contract["first_release"]
    required_first = {"plan_status", "included_action_ids", "interim_acceptance", "reason", "interim_action_ids", "remaining_work", "existing_url_actions"}
    if not isinstance(first, dict) or set(first) != required_first or first["plan_status"] not in {"conditional_publishable", "blocked", "not_applicable"}:
        raise ValueError("First release needs explicit state and boundary")
    included = names(first["included_action_ids"], "first release action IDs", allow_empty=True)
    if not included <= selected or any(actions[action_id]["phase"] != "B3" for action_id in included):
        raise ValueError("First release references nonselected or non-B3 work")
    interim = names(first["interim_action_ids"], "first release interim action IDs", allow_empty=True)
    if not interim <= included:
        raise ValueError("Interim release work must be in the first package")
    rows = first["existing_url_actions"]
    if not isinstance(rows, list) or any(not isinstance(row, dict) or set(row) != {"action_id", "source_ids", "disposition", "reason"} for row in rows):
        raise ValueError("First release needs existing-URL action dispositions")
    if len(rows) != len(existing) or {row["action_id"] for row in rows} != existing:
        raise ValueError("First release must classify every selected existing-URL action")
    for row in rows:
        action_id = row["action_id"]
        if names(row["source_ids"], "release source IDs", allow_empty=True) != sources[action_id]:
            raise ValueError("First release action loses selected source IDs")
        if row["disposition"] not in {"included", "deferred_nonblocking", "blocks_release"}:
            raise ValueError("Existing-URL action has no release disposition")
        if (row["disposition"] == "included") != (action_id in included):
            raise ValueError("First release included actions and dispositions disagree")
        if row["disposition"] != "included":
            text_field(row, "reason", "Deferred or blocked release action")
    blockers = {row["action_id"] for row in rows if row["disposition"] == "blocks_release"}
    if first["plan_status"] == "conditional_publishable":
        if not included or blockers:
            raise ValueError("Publishable first release needs selected work and no declared blocking action")
        text_field(first, "interim_acceptance", "Publishable first release")
    elif first["plan_status"] == "blocked":
        text_field(first, "reason", "Blocked first release")
        if not blockers and not any(item["state"] in {"pending", "unknown", "unavailable"}
                                for item in data.get("pending_items", [])):
            raise ValueError("Blocked first release needs a declared action or pending blocker")
    elif included or any(action["phase"] == "B3" and (action.get("affected_urls") or action.get("landing_job_ids")) for action in actions.values()):
        raise ValueError("First release cannot be not_applicable while publishable work is selected")
    current_gate = contract["current_release_gate"]
    if (not isinstance(current_gate, dict) or set(current_gate) != {"status", "reason"}
            or current_gate["status"] not in {"untested", "unknown", "blocked", "not_applicable"}):
        raise ValueError("Current proposed-release gate needs a pre-sales state and reason")
    text_field(current_gate, "reason", "Current proposed-release gate")
    if (current_gate["status"] == "not_applicable") != (first["plan_status"] == "not_applicable"):
        raise ValueError("Current proposed-release N/A must match the absent first-release package")
    remaining = first["remaining_work"]
    if not isinstance(remaining, list) or any(not isinstance(row, dict) or set(row) != {"action_id", "estimate_ref", "condition", "final_acceptance"} for row in remaining):
        raise ValueError("Remaining first-release work needs an exact paid boundary")
    if len(remaining) != len(interim) or {row["action_id"] for row in remaining} != interim:
        raise ValueError("Each interim action needs one remaining-work boundary")
    for row in remaining:
        if row["estimate_ref"] != line_by_action[row["action_id"]]:
            raise ValueError("Remaining work must resolve to the same selected estimate line")
        text_field(row, "condition", "Remaining work")
        text_field(row, "final_acceptance", "Remaining work")
    gates = contract["conditional_acceptance_paths"]
    if not isinstance(gates, list):
        raise ValueError("Conditional acceptance paths must be a list")
    gate_ids, test_ids = set(), set()
    common = {"test_work_state", "functional_state", "publication_state", "measurement_state",
              "verified_path_ids", "receipt_requirement", "decision_owner", "future_work_boundary",
              "branch_allocation"}
    for gate in gates:
        if not isinstance(gate, dict) or set(gate) != {"gate_id", "test_action_id", "dependent_action_ids", "path_ids", "action_path_requirements", "outcomes"}:
            raise ValueError("Conditional gate needs selected test, dependents, paths and outcomes")
        text_field(gate, "gate_id", "Conditional gate")
        test_id = gate["test_action_id"]
        if gate["gate_id"] in gate_ids or test_id in test_ids or test_id not in selected:
            raise ValueError("Conditional gate ID or selected test action is duplicated/unknown")
        gate_ids.add(gate["gate_id"])
        test_ids.add(test_id)
        dependents = names(gate["dependent_action_ids"], "conditional dependent actions")
        if test_id in dependents or not dependents <= selected:
            raise ValueError("Conditional gate dependents must be other selected actions")
        paths = names(gate["path_ids"], "conditional functional paths")
        requirements = gate["action_path_requirements"]
        if not isinstance(requirements, dict) or set(requirements) != dependents:
            raise ValueError("Conditional gate needs declared path dependencies for each dependent action")
        for action_id, requirement in requirements.items():
            if (not isinstance(requirement, dict) or set(requirement) != {"path_ids", "acceptance_criterion"}
                    or not names(requirement["path_ids"], "action path requirements", allow_empty=True) <= paths
                    or requirement["acceptance_criterion"] not in actions[action_id]["acceptance_criteria"]):
                raise ValueError("Action path requirement needs declared paths and its existing acceptance criterion")
        outcomes = gate["outcomes"]
        if not isinstance(outcomes, dict) or set(outcomes) != {"success", "partial", "failure", "unavailable"}:
            raise ValueError("Conditional gate needs four generic outcomes")
        partial_rows = outcomes["partial"]
        if not isinstance(partial_rows, list) or not partial_rows:
            raise ValueError("Partial outcomes need an explicit nonempty list")
        partial_subsets = set()
        for kind in ("success", "partial", "failure", "unavailable"):
            choices = partial_rows if kind == "partial" else [outcomes[kind]]
            for outcome in choices:
                if kind == "partial" and isinstance(outcome, dict) and outcome.get("applicability") == "not_applicable":
                    if len(paths) != 1 or len(partial_rows) != 1 or set(outcome) != {"applicability", "reason"}:
                        raise ValueError("Partial not_applicable requires one indivisible path and reason")
                    text_field(outcome, "reason", "Single-path partial N/A")
                    continue
                integration = actions[test_id].get("work_scope", {}).get("operation_kind") == "integration"
                expected_outcome = common | {"applicability"} | ({"test_action_residual"} if integration else set())
                if not isinstance(outcome, dict) or set(outcome) != expected_outcome or outcome["applicability"] != "applicable":
                    raise ValueError("Applicable conditional outcome needs the full branch contract")
                if integration:
                    residual = outcome["test_action_residual"]
                    residual_keys = {"estimate_ref", "completed_work", "remaining_criteria", "scope_boundary"}
                    if (not isinstance(residual, dict) or set(residual) != residual_keys
                            or residual["estimate_ref"] != line_by_action[test_id]
                            or not isinstance(residual["completed_work"], str) or not residual["completed_work"].strip()):
                        raise ValueError("Integration test outcome needs its original estimate and explicit residual work")
                    criteria = names(residual["remaining_criteria"], "integration residual criteria", allow_empty=True)
                    if kind == "success":
                        if criteria or residual["scope_boundary"] != "original_scope_complete":
                            raise ValueError("Successful integration must close its original criteria")
                    elif (not criteria or not criteria <= set(actions[test_id]["acceptance_criteria"])
                          or residual["scope_boundary"] != "same_estimate_original_scope"):
                        raise ValueError("Incomplete integration keeps unmet criteria in the same original estimate")
                for key in ("receipt_requirement", "decision_owner"):
                    text_field(outcome, key, "Conditional outcome")
                verified = names(outcome["verified_path_ids"], "verified conditional paths", allow_empty=True)
                if not verified <= paths:
                    raise ValueError("Verified conditional path is undeclared")
                allocation = outcome["branch_allocation"]
                keys = {"prepared_action_ids", "published_action_ids", "accepted_action_ids",
                        "deferred_action_ids", "remaining_criteria", "target_units_by_unit",
                        "accepted_units_by_unit", "deferred_units_by_unit"}
                if not isinstance(allocation, dict) or set(allocation) != keys:
                    raise ValueError("Conditional branch needs exact prepared/published/accepted/deferred allocation")
                branch = {key: names(allocation[key], key, allow_empty=True)
                          for key in ("prepared_action_ids", "published_action_ids", "accepted_action_ids", "deferred_action_ids")}
                prepared, published, accepted, deferred = (branch[key] for key in
                    ("prepared_action_ids", "published_action_ids", "accepted_action_ids", "deferred_action_ids"))
                page_ids = {action_id for action_id in dependents if actions[action_id]["unit"] == "page"}
                if (not prepared <= dependents or not published <= prepared or not accepted <= prepared
                        or not accepted.isdisjoint(deferred) or accepted | deferred != dependents
                        or not (accepted & page_ids) <= published):
                    raise ValueError("Conditional branch action sets do not partition dependent work")
                if any(not set(requirements[action_id]["path_ids"]) <= verified for action_id in accepted):
                    raise ValueError("Accepted dependent action lacks its declared functional path")
                if kind in {"failure", "unavailable"} and accepted:
                    raise ValueError("Blocked dependent function cannot accept dependent work")
                if outcome["publication_state"] == "blocked" and published:
                    raise ValueError("Blocked publication cannot publish dependent work")
                remaining = allocation["remaining_criteria"]
                if (not isinstance(remaining, dict) or set(remaining) != deferred
                        or any(not isinstance(item, dict) or set(item) != {"criterion", "next_decision"}
                               or not all(isinstance(value, str) and value.strip() for value in item.values())
                               or item["criterion"] not in actions[action_id]["acceptance_criteria"]
                               for action_id, item in remaining.items())):
                    raise ValueError("Deferred branch work needs its existing acceptance criterion and next decision")
                def units(ids):
                    result = {}
                    for action_id in ids:
                        action = actions[action_id]
                        result[action["unit"]] = result.get(action["unit"], 0) + action["quantity"]
                    return result
                if (allocation["target_units_by_unit"] != units(dependents)
                        or allocation["accepted_units_by_unit"] != units(accepted)
                        or allocation["deferred_units_by_unit"] != units(deferred)):
                    raise ValueError("Conditional branch unit counts differ from selected action quantities")
                if outcome["test_work_state"] not in {"accepted_on_executed_receipt", "not_accepted"} or outcome["functional_state"] not in {"verified", "limited", "blocked"} or outcome["publication_state"] not in {"full", "limited", "blocked"} or outcome["measurement_state"] not in {"start_after_verified_receipt", "limited_after_verified_subset", "blocked"} or outcome["future_work_boundary"] not in {"none", "separate_decision"}:
                    raise ValueError("Conditional outcome has unsupported acceptance state")
                if kind == "success":
                    if verified != paths or outcome["test_work_state"] != "accepted_on_executed_receipt" or outcome["functional_state"] != "verified" or outcome["measurement_state"] == "limited_after_verified_subset":
                        raise ValueError("Success needs receipts for every declared path")
                elif kind == "partial":
                    if len(paths) < 2 or not verified or verified == paths or outcome["test_work_state"] != "accepted_on_executed_receipt" or outcome["functional_state"] == "verified" or outcome["publication_state"] == "full" or outcome["measurement_state"] == "start_after_verified_receipt":
                        raise ValueError("Partial outcome needs a real mixed path and limited dependent claim")
                    subset = frozenset(verified)
                    if subset in partial_subsets:
                        raise ValueError("Partial outcome repeats a declared path subset")
                    partial_subsets.add(subset)
                elif kind == "failure":
                    if verified or outcome["test_work_state"] != "accepted_on_executed_receipt" or outcome["functional_state"] != "blocked" or outcome["publication_state"] == "full" or outcome["measurement_state"] != "blocked":
                        raise ValueError("Failure can accept executed test work but not the dependent function")
                elif verified or outcome["test_work_state"] != "not_accepted" or outcome["functional_state"] != "blocked" or outcome["publication_state"] == "full" or outcome["measurement_state"] != "blocked":
                    raise ValueError("Unavailable test cannot accept test work or dependent function")
    checkpoints = contract["future_checkpoints"]
    if not isinstance(checkpoints, list) or not checkpoints:
        raise ValueError("Future checkpoints need B6/B12 commercial boundaries")
    seen_checkpoints, included_by_phase, phases = set(), {"B6": set(), "B12": set()}, set()
    for row in checkpoints:
        if not isinstance(row, dict) or set(row) != {"checkpoint_id", "phase", "commercial_model", "outside_kind", "action_ids", "estimate_refs", "scope", "acceptance", "price_boundary"}:
            raise ValueError("Future checkpoint needs a bounded scope and price boundary")
        text_field(row, "checkpoint_id", "Future checkpoint")
        if row["checkpoint_id"] in seen_checkpoints or row["phase"] not in included_by_phase or row["commercial_model"] not in {"included_work", "outside_purchase"}:
            raise ValueError("Future checkpoint ID, phase or model is invalid")
        seen_checkpoints.add(row["checkpoint_id"])
        phases.add(row["phase"])
        for key in ("scope", "acceptance", "price_boundary"):
            text_field(row, key, "Future checkpoint")
        ids = names(row["action_ids"], "future checkpoint action IDs", allow_empty=True)
        refs = names(row["estimate_refs"], "future checkpoint estimate refs", allow_empty=True)
        if row["commercial_model"] == "included_work":
            if not ids or row["outside_kind"] is not None or any(action_id not in selected or actions[action_id]["phase"] != row["phase"] for action_id in ids) or refs != {line_by_action[action_id] for action_id in ids} or ids & included_by_phase[row["phase"]]:
                raise ValueError("Included future work needs unique phase actions and exact estimate joins")
            included_by_phase[row["phase"]].update(ids)
        elif ids or refs or row["outside_kind"] not in {"decision_only", "separate_option"}:
            raise ValueError("Outside-purchase checkpoint cannot contain paid work")
    if phases != {"B6", "B12"} or any(included_by_phase[phase] != {action_id for action_id, action in actions.items() if action["phase"] == phase} for phase in phases):
        raise ValueError("Future checkpoints must cover selected B6/B12 work exactly once")


def scope_handoff_checks(root, state, data):
    """Join the Seller's bounded SEO and measurement promises to recorded sources/work."""
    actions, line_by_action, _, _ = release_contract_context(root, state)
    outline = data.get("structure_outline")
    if not isinstance(outline, dict) or set(outline) != {"section_ids", "section_briefs_ref", "section_briefs_sha256", "seo_state_rows"}:
        raise ValueError("Decision map needs a bound structure outline and SEO state rows")
    sections = outline["section_ids"]
    if (not isinstance(sections, list) or len(sections) != len(set(sections))
            or "GAP" not in sections or sections.index("GAP") + 1 >= len(sections)
            or sections[sections.index("GAP") + 1] != "SEO"):
        raise ValueError("Whole-site GAP must be followed directly by SEO state")
    brief_ref = outline["section_briefs_ref"]
    if (not isinstance(brief_ref, str) or not brief_ref.endswith("section-briefs.md")
            or outline["section_briefs_sha256"] != sha_file(relative_file(root, brief_ref))):
        raise ValueError("Structure outline needs saved section brief with matching SHA-256")
    audit = previous_output(root, state, "audit", "audit_findings")
    findings = {row["finding_id"]: row for row in audit.get("findings", [])}
    strategy = previous_output(root, state, "strategy", "strategy_map")
    selected = {row["source_id"][6:]: row["action_id"] for row in strategy["decision_ledger"]
                if row["disposition"] == "selected" and row["source_id"].startswith("audit:")}
    rows = outline["seo_state_rows"]
    if not isinstance(rows, list) or {row.get("finding_id") for row in rows if isinstance(row, dict)} != set(selected) or len(rows) != len(selected):
        raise ValueError("SEO state must cover each selected audit finding exactly once")
    for row in rows:
        keys = {"finding_id", "source_ref", "source_date_status", "finding_or_limit", "urls_or_group",
                "scale", "action_id", "phase", "acceptance"}
        if not isinstance(row, dict) or set(row) != keys or any(not isinstance(row[key], str) or not row[key].strip() for key in keys):
            raise ValueError("SEO state row needs source, date/status, URL scale, action, phase and acceptance")
        finding_id, action_id = row["finding_id"], row["action_id"]
        if (finding_id not in findings or selected.get(finding_id) != action_id
                or row["source_ref"] != findings[finding_id].get("source_ref")
                or row["phase"] != actions[action_id]["phase"]
                or row["acceptance"] not in actions[action_id]["acceptance_criteria"]):
            raise ValueError("SEO state row loses audit source or selected action/phase/acceptance join")
        relative_file(root, row["source_ref"])
    checkpoint_rows = data.get("checkpoint_deliverables")
    periods = set(data["measurement_plan"]["comparison_periods"]) & {"B3", "B6", "B12"}
    if (not isinstance(checkpoint_rows, list) or len(checkpoint_rows) != len(periods)
            or {row.get("phase") for row in checkpoint_rows if isinstance(row, dict)} != periods):
        raise ValueError("Each declared B3/B6/B12 measurement needs one checkpoint deliverable")
    for row in checkpoint_rows:
        keys = {"phase", "commercial_model", "operation_action_id", "estimate_ref",
                "source_panel", "period_method", "finite_receipt", "role", "acceptance"}
        if not isinstance(row, dict) or set(row) != keys or row["commercial_model"] not in {"included_work", "outside_purchase"}:
            raise ValueError("Checkpoint deliverable needs a bounded commercial model")
        if any(not isinstance(row[key], str) or not row[key].strip() for key in
               ("source_panel", "period_method", "finite_receipt", "role", "acceptance")):
            raise ValueError("Checkpoint deliverable needs source, method, finite receipt, role and acceptance")
        action_id = row["operation_action_id"]
        if row["commercial_model"] == "included_work":
            if (action_id not in actions or actions[action_id]["phase"] != row["phase"]
                    or row["estimate_ref"] != line_by_action[action_id]
                    or row["acceptance"] not in actions[action_id]["acceptance_criteria"]
                    or row["finite_receipt"] not in actions[action_id]["work_scope"].get("checkpoint_receipts", [])):
                raise ValueError("Included checkpoint needs same-phase action, estimate and acceptance")
        elif action_id is not None or row["estimate_ref"] is not None:
            raise ValueError("Outside-purchase checkpoint cannot claim current action or estimate")


def client_handoff_checks(root, state, packet, packet_path, files):
    required = {"visible-appendix.md", "client-handoff.json"}
    if not required <= set(files):
        raise ValueError("Complete client packet needs real readable appendix and handoff map files")
    if "visible-appendix.md" not in (root / "proposal.md").read_text(encoding="utf-8"):
        raise ValueError("Main proposal must link the readable client appendix")
    appendix = relative_file(root, str(Path(packet_path) / "visible-appendix.md")).read_text(encoding="utf-8-sig")
    handoff = checked_object(root, str(Path(packet_path) / "client-handoff.json"), "client handoff")
    packet_root = (root / packet_path).resolve()
    def attached(ref):
        if (not isinstance(ref, str) or not ref.startswith("client-evidence/") or ".." in Path(ref).parts
                or ref not in files):
            raise ValueError("Client evidence must be packet-relative and in exact manifest")
        target = (packet_root / ref).resolve()
        if not target.is_relative_to(packet_root) or not target.is_file() or sha_file(target) != files[ref]:
            raise ValueError("Client evidence file is missing or changed in exact packet")
        return target
    if handoff.get("schema_version") != 4 or not all(title in appendix for title in
        ("Карта типов и вариантов", "Решения по спросу", "Первичные строки спроса и источники",
         "Поблочные решения", "Основания текущего состояния",
         "Реестр уникальных работ", "Измерение результата", "Релевантный кейс",
         "Основания чисел", "Визуальные материалы", "Текстовые выводы БАР")):
        raise ValueError("Client appendix needs readable type, block, measurement and case sections")
    semantic = previous_output(root, state, "semantic_types", "semantic_map")
    gap = previous_output(root, state, "gap", "gap_register")
    report = previous_output(root, state, "gap", "site_gap_report")
    block = previous_output(root, state, "block_gap", "block_register")
    strategy = previous_output(root, state, "strategy", "strategy_map")
    implementation = previous_output(root, state, "implementation", "implementation_plan")
    estimate = previous_output(root, state, "estimate", "estimate_sheet")
    decision = previous_output(root, state, "decision_map", "decision_map")
    cases = previous_output(root, state, "company_cases", "company_case_pack")
    bar_register = previous_output(root, state, "text_bar", "text_register")
    client_main_table_checks((root / "proposal.md").read_text(encoding="utf-8-sig"),
                             handoff.get("main_tables"), semantic, block, bar_register, handoff.get("type_labels"))
    variants = {(kind, variant["variant_id"]): variant for kind, rows in block["variants_by_type"].items() for variant in rows}
    type_rows = handoff.get("type_variants")
    if (not isinstance(type_rows, list) or len(type_rows) != len(variants)
            or {(r.get("type_id"), r.get("variant_id")) for r in type_rows if isinstance(r, dict)} != set(variants)):
        raise ValueError("Client type map must cover every sourced type and variant")
    type_gap = {row["page_type"]: row for row in report["type_gap"]}
    allocations = {row["url"]: row["action"] for row in gap["page_allocations"]}
    expected_demand = expected_client_demand(root, state, gap, report, semantic)
    type_query_ids = client_query_ids_by_type(expected_demand)
    for row in type_rows:
        kind, variant_id = row["type_id"], row["variant_id"]
        variant = variants[kind, variant_id]
        peer_refs = {ref for item in block["rows"] if (item["type_id"], item["variant_id"]) == (kind, variant_id)
                     for ref in item.get("peer_evidence_refs", [])}
        peer_urls = sorted({block["evidence_index"][ref].get("source_url") for ref in peer_refs if block["evidence_index"][ref].get("source_url")})
        own_urls = sorted(variant.get("url_refs", []))
        expected = {"own_urls": own_urls, "peer_urls": peer_urls, "rank_scope": report["ranks"].get("scope"),
                    "rank_coverage": type_gap[kind]["rank_coverage"],
                    "rank_positions": type_gap[kind].get("average_position_ours"),
                    "url_decisions": {url: allocations[url] for url in own_urls},
                    "task_decision": semantic["decisions"][kind],
                    "intent_ids": sorted(semantic["intent_ids_by_type"][kind])}
        if any(canonical(row.get(key)) != canonical(value) for key, value in expected.items()) or not row.get("basis"):
            raise ValueError("Client type map loses source URL, peer, position/slice, intent or decision")
        if client_type_line(row, type_query_ids) not in appendix:
            raise ValueError("Client type map row is absent from readable appendix")
    demand_rows = handoff.get("demand_decisions")
    if canonical(demand_rows) != canonical(expected_demand) or any(client_demand_line(row) not in appendix for row in expected_demand):
        raise ValueError("Client demand decisions lose raw query, cluster, target, rank or disposition")
    source_requirements = expected_client_sources(root, state)
    source_rows = handoff.get("source_rows")
    if (not isinstance(source_rows, list) or len(source_rows) != len(source_requirements)
            or {row.get("source_id") for row in source_rows if isinstance(row, dict)} != set(source_requirements)):
        raise ValueError("Client packet omits saved raw demand, rank or available link rows")
    for row in source_rows:
        expected = source_requirements[row["source_id"]]
        if any(row.get(key) != expected[key] for key in ("source_ref", "source_sha256", "mode", "source_label")) or not row.get("limitation"):
            raise ValueError("Client source rows differ from saved analysis input or lack limits")
        ref = row.get("packet_ref")
        candidate = attached(ref)
        if row["mode"] == "copy":
            if candidate.read_bytes() != relative_file(root, row["source_ref"]).read_bytes():
                raise ValueError("Client raw demand/link source differs from saved source")
        elif canonical(read_json(candidate)) != canonical(expected["projection"]):
            raise ValueError("Client rank rows omit own/peer observations or source slice")
        if client_source_line(row) not in appendix:
            raise ValueError("Client source rows link is absent from readable appendix")
    ledger = {row["source_id"]: row for row in strategy["decision_ledger"]}
    proof_requirements = expected_client_proofs(root, state, block, strategy)
    proofs = handoff.get("material_proofs")
    if (not isinstance(proofs, list) or len(proofs) != len(proof_requirements)
            or {(row.get("source_id"), row.get("evidence_id")) for row in proofs if isinstance(row, dict)} != set(proof_requirements)):
        raise ValueError("Client material proof map must cover selected block and audit sources")
    for proof in proofs:
        expected = proof_requirements[(proof["source_id"], proof["evidence_id"])]
        if any(proof.get(key) != expected[key] for key in ("source_ref", "source_sha256", "source_url", "captured_at", "supporting_quote")):
            raise ValueError("Client material proof differs from saved source")
        if expected.get("addressed_urls") and proof.get("addressed_urls") != expected["addressed_urls"]:
            raise ValueError("Client positive proof loses addressed source URLs")
        source = relative_file(root, proof["source_ref"])
        if sha_file(source) != proof["source_sha256"] or not proof.get("limitation"):
            raise ValueError("Client material proof source changed or lacks limitation")
        if proof.get("status") == "shown":
            ref = proof.get("packet_ref")
            candidate = attached(ref)
            if not candidate.read_bytes().strip():
                raise ValueError("Client material proof is missing, empty or changed")
            if proof.get("mode") == "copy":
                if candidate.read_bytes() != source.read_bytes():
                    raise ValueError("Client material proof copy differs from saved source")
            elif proof.get("mode") == "excerpt":
                if candidate.read_text(encoding="utf-8-sig").strip() not in source.read_text(encoding="utf-8-sig"):
                    raise ValueError("Client material proof excerpt differs from saved source")
            else:
                raise ValueError("Client material proof needs copy or exact excerpt mode")
            if source.suffix.lower() in {".html", ".htm", ".txt", ".json", ".csv", ".md"}:
                quote = proof.get("supporting_quote")
                if not isinstance(quote, str) or not quote.strip() or quote not in source.read_text(encoding="utf-8-sig") or quote not in candidate.read_text(encoding="utf-8-sig"):
                    raise ValueError("Client proof omits its source-declared supporting quote")
            if expected.get("addressed_urls") and any(url not in candidate.read_text(encoding="utf-8-sig") for url in expected["addressed_urls"]):
                raise ValueError("Client positive proof omits an addressed source URL")
        elif proof.get("status") == "unavailable":
            if proof.get("packet_ref") is not None or not proof.get("reason") or not proof.get("claim_limit") or proof["claim_limit"] not in appendix:
                raise ValueError("Unavailable client material proof needs visible claim limit")
        else:
            raise ValueError("Client material proof needs shown or unavailable status")
        if client_proof_line(proof) not in appendix:
            raise ValueError("Client material proof row is absent from readable appendix")
    source_blocks = {f"block:{r['type_id']}:{r['variant_id']}:{r['block_id']}": r for r in block["rows"]}
    rows = handoff.get("block_findings")
    if (not isinstance(rows, list) or len(rows) != len(source_blocks)
            or {r.get("source_id") for r in rows if isinstance(r, dict)} != set(source_blocks)):
        raise ValueError("Client block map must account for every sourced block assessment")
    implemented = {row["action_id"]: row for row in implementation["actions"]}
    estimate_lines = estimate_line_checks(implementation, estimate)
    decision_rows = {row["action_id"]: row for row in decision["decisions"]}
    selected_block_actions = set()
    works = handoff.get("unique_works")
    if not isinstance(works, list) or any(not isinstance(row, dict) for row in works):
        raise ValueError("Client block map needs a unique-work register")
    works_by_id = {row.get("action_id"): row for row in works}
    if len(works_by_id) != len(works):
        raise ValueError("Client block work is duplicated")
    for row in rows:
        source_id = row["source_id"]
        source = source_blocks[source_id]
        material = source["status"] in {"weak", "missing"}
        if source["status"] not in {"weak", "missing", "sufficient", "not_applicable"}:
            raise ValueError("Unknown block cannot be presented as no work")
        disposition = ledger[source_id]["disposition"] if material else ("no_action_required" if source["status"] == "sufficient" else "not_applicable")
        action_id = ledger[source_id].get("action_id") if disposition == "selected" else None
        reason = ledger[source_id].get("reason") if material and disposition != "selected" else source.get("change") if material else source.get("assessment_reason")
        expected = {"type_id": source["type_id"], "variant_id": source["variant_id"], "block_id": source["block_id"],
                    "status": source["status"], "target_urls": sorted(source.get("affected_urls", [])),
                    "landing_job_ids": sorted(source.get("landing_job_ids", [])), "disposition": disposition,
                    "action_id": action_id, "reason": reason}
        if any(canonical(row.get(key)) != canonical(value) for key, value in expected.items()):
            raise ValueError("Client block finding loses address, disposition or selected action")
        if action_id:
            selected_block_actions.add(action_id)
        if not row.get("reason") or client_block_line(row, works_by_id) not in appendix:
            raise ValueError("Client block work row is absent from readable appendix")
    if set(works_by_id) != set(implemented) or not selected_block_actions <= set(works_by_id):
        raise ValueError("Client unique works differ from selected implementation actions")
    for action_id, work in works_by_id.items():
        source = implemented.get(action_id)
        if not source or action_id not in decision_rows:
            raise ValueError("Client block work lacks implementation or estimate decision")
        expected = {"unit": source["unit"], "quantity": source["quantity"], "phase": source["phase"],
                    "acceptance": source["acceptance"], "estimate_ref": decision_rows[action_id]["estimate_ref"]}
        line = estimate_lines.get(work.get("estimate_ref"))
        if (any(work.get(key) != value for key, value in expected.items())
                or not line or action_id not in line["action_ids"]
                or line["unit"] != source["unit"]
                or estimate["volumes_by_unit"].get(source["unit"], 0) < source["quantity"]):
            raise ValueError("Client block work disagrees with unique implementation/estimate unit")
        if client_work_line(work) not in appendix:
            raise ValueError("Client unique work is absent from readable appendix")
    plan = handoff.get("measurement_plan")
    measurement_checks(plan)
    if canonical(plan) != canonical(decision["measurement_plan"]) or client_measurement_line(plan) not in appendix:
        raise ValueError("Client measurement protocol differs from reviewed decision map or visible appendix")
    case_field = handoff.get("case_field")
    confirmed = sorted(row["source_ref"] for row in cases["cases"] if row["fit_status"] == "relevant_confirmed")
    if confirmed:
        if not isinstance(case_field, dict) or case_field.get("status") != "relevant_confirmed" or case_field.get("source_refs") != confirmed or not case_field.get("label"):
            raise ValueError("Client case handoff needs exact confirmed relevant cases")
    elif canonical(case_field) != canonical(cases["human_case_field"]):
        raise ValueError("Client packet omits the pending human relevant-case field")
    if client_case_line(case_field) not in appendix:
        raise ValueError("Human relevant-case field is absent from readable appendix")
    client_evidence_checks(root, state, handoff, appendix, packet_path, files)


def audit_stage_checks(root, state, refs, data):
    access = data.get("source_access")
    if (not isinstance(access, dict) or not isinstance(access.get("gsc"), str)
            or access["gsc"] not in {"available", "partial", "unavailable"}
            or any(not isinstance(value, str) or value not in {"available", "partial", "unavailable"} for value in access.values())):
        raise ValueError("Audit needs explicit source access, including GSC availability")
    if not isinstance(data.get("findings"), list) or not isinstance(data.get("inspected_urls"), list):
        raise ValueError("Audit needs findings and an explicit inspected URL list, even when empty")
    sources = checked_object(root, refs["inputs"]["audit_sources"], "audit_sources")
    resources = sources.get("resources", [])
    receipts = sources.get("inspection_receipts", [])
    artifacts = sources.get("source_artifacts", [])
    diagnostics = data.get("url_diagnostics", [])
    if any(not isinstance(items, list) for items in (resources, receipts, artifacts, diagnostics)):
        raise ValueError("Audit resources, receipts, source artifacts and diagnostics must be lists")
    artifact_by_ref = {}
    source_ids = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not all(artifact.get(k) for k in ("source_id", "source_system", "artifact_ref", "sha256", "captured_at", "scope")):
            raise ValueError("Audit source artifact needs system, saved file, hash, date and scope")
        system, ref = artifact["source_system"], artifact["artifact_ref"]
        if system not in access or access[system] == "unavailable" or artifact["source_id"] in source_ids or ref in artifact_by_ref:
            raise ValueError("Audit source artifact must have a unique ID and available or partial declared source")
        saved = relative_file(root, ref)
        if sha_file(saved) != artifact["sha256"] or not saved.read_bytes().strip():
            raise ValueError("Audit source artifact is empty or SHA-256 changed")
        artifact_by_ref[ref] = artifact
        source_ids.add(artifact["source_id"])
    if access["gsc"] != "available":
        unknowns = data.get("unverified_checks")
        if not isinstance(unknowns, list) or not any(isinstance(row, dict) and row.get("source_system") == "gsc"
                and row.get("check") == "index_status" and row.get("reason") for row in unknowns):
            raise ValueError("Unavailable GSC requires explicit unverified index status, not zero errors")
    targets = {}
    site_host = urlsplit(previous_output(root, state, "intake", "scope")["site"]).hostname
    for source in resources:
        target = audit_resource_target(root, source)
        ident = source["resource_id"]
        if ident in targets:
            raise ValueError("Audit resource IDs must be unique")
        if target["status"] != "resolved":
            raise ValueError("Unresolved canonical/final audit target keeps the audit partial")
        if urlsplit(target["target_url"]).hostname != site_host:
            raise ValueError("Audit inspection target differs from approved site host")
        targets[ident] = (source, target)
    if len(receipts) != len(targets) or len(diagnostics) != len(targets):
        raise ValueError("Every performed exact audit inspection needs one receipt and diagnostic row")
    receipts_by_id = {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or receipt.get("resource_id") not in targets or receipt["resource_id"] in receipts_by_id:
            raise ValueError("Audit inspection receipt has unknown or duplicate resource")
        ident = receipt["resource_id"]
        system = receipt.get("source_system")
        if system not in access or access[system] == "unavailable":
            raise ValueError("Audit inspection receipt cannot claim an unavailable source")
        if receipt.get("request_url") != targets[ident][1]["target_url"] or not receipt.get("captured_at"):
            raise ValueError("Audit inspection request must use exact observed canonical/final/variant URL")
        response = relative_file(root, receipt.get("response_ref"))
        if sha_file(response) != receipt.get("response_sha256"):
            raise ValueError("Audit inspection response SHA-256 mismatch")
        result = read_json(response)
        if not isinstance(result, dict):
            raise ValueError("Audit inspection response must be a JSON object")
        if system == "gsc":
            inspection = result.get("inspectionResult")
            index_status = inspection.get("indexStatusResult") if isinstance(inspection, dict) else None
            observed = index_status.get("verdict") if isinstance(index_status, dict) else None
            if not isinstance(observed, str) or not observed:
                raise ValueError("GSC inspection receipt lacks an actual saved index verdict")
        else:
            path = receipt.get("result_path")
            if not isinstance(path, list) or not path or any(not isinstance(key, str) or not key for key in path):
                raise ValueError("Non-GSC inspection needs a source-specific saved result_path")
            observed = result
            for key in path:
                observed = observed.get(key) if isinstance(observed, dict) else None
            if not isinstance(observed, str) or not observed:
                raise ValueError("Non-GSC inspection status must exist at saved result_path")
            url_path = receipt.get("response_url_path")
            if not isinstance(url_path, list) or not url_path or any(not isinstance(key, str) or not key for key in url_path):
                raise ValueError("Non-GSC inspection needs an exact saved response_url_path")
            observed_url = result
            for key in url_path:
                observed_url = observed_url.get(key) if isinstance(observed_url, dict) else None
            if observed_url != receipt["request_url"]:
                raise ValueError("Non-GSC saved response URL differs from exact inspected URL")
        receipts_by_id[ident] = (receipt, observed, system)
    diagnostics_by_id = {}
    for row in diagnostics:
        if not isinstance(row, dict) or row.get("resource_id") not in targets or row["resource_id"] in diagnostics_by_id:
            raise ValueError("Audit diagnostic has unknown or duplicate resource")
        ident = row["resource_id"]
        source, target = targets[ident]
        receipt, observed, system = receipts_by_id[ident]
        status_field = "inspection_verdict" if system == "gsc" else "observed_status"
        other_field = "observed_status" if system == "gsc" else "inspection_verdict"
        if (row.get("logical_key") != source["logical_key"] or row.get("request_url") != target["target_url"]
                or row.get("diagnosis_scope") != target["diagnosis_scope"] or row.get("source_kind") != target["source_kind"]
                or row.get("source_system") != system or row.get("response_ref") != receipt["response_ref"]
                or row.get(status_field) != observed or row.get(other_field) is not None):
            raise ValueError("Audit diagnostic must bind exact saved resource, source-specific status and response")
        diagnostics_by_id[ident] = row
    if (set(diagnostics_by_id) != set(targets) or set(receipts_by_id) != set(targets)
            or names(data["inspected_urls"], "exact inspected URLs", allow_empty=True) != {target["target_url"] for _, target in targets.values()}):
        raise ValueError("Audit inspected URLs differ from source-resolved request targets")
    if not receipts and not artifacts:
        raise ValueError("Audit complete needs at least one saved inspected response or other source artifact")
    for row in data["findings"]:
        if not isinstance(row, dict) or not all(row.get(k) for k in ("finding_id", "source_ref", "url", "action", "acceptance")):
            raise ValueError("Audit finding lacks source, URL, action or acceptance")
        if row.get("evidence_kind") == "url_inspection":
            ident = row.get("diagnostic_resource_id")
            if ident not in targets:
                raise ValueError("URL Inspection finding needs a saved diagnostic resource")
            source, target = targets[ident]
            receipt, observed, system = receipts_by_id[ident]
            status_field = "inspection_verdict" if system == "gsc" else "observed_status"
            other_field = "observed_status" if system == "gsc" else "inspection_verdict"
            if (row["url"] != source["logical_key"] or row.get("diagnosed_request_url") != target["target_url"]
                    or row.get("diagnosis_scope") != target["diagnosis_scope"] or row["source_ref"] != receipt["response_ref"]
                    or row.get("source_system") != system or row.get(status_field) != observed or row.get(other_field) is not None):
                raise ValueError("URL Inspection finding cannot apply a variant/source status to another resource")
        elif row.get("evidence_kind") == "other":
            artifact = artifact_by_ref.get(row["source_ref"])
            if artifact is None or row.get("source_system") != artifact["source_system"]:
                raise ValueError("Non-inspection audit finding needs saved source artifact provenance")
        else:
            raise ValueError("Audit finding needs explicit URL Inspection or other evidence kind")
    names([row["finding_id"] for row in data["findings"]], "audit finding IDs", allow_empty=True)
    for claim in data.get("positive_claims", []):
        if (not isinstance(claim, dict) or not all(claim.get(key) for key in
                ("claim_id", "source_ref", "supporting_quote", "limitation"))
                or not isinstance(claim.get("addressed_urls"), list) or not claim["addressed_urls"]):
            raise ValueError("Positive audit claim needs ID, saved source, addressed URLs, quote and limit")
        source_ref = claim["source_ref"]
        if source_ref not in artifact_by_ref and source_ref not in {row.get("response_ref") for row in receipts}:
            raise ValueError("Positive audit claim lacks a saved audit source")
        saved_text = relative_file(root, source_ref).read_text(encoding="utf-8-sig")
        if claim["supporting_quote"] not in saved_text or any(url not in saved_text for url in
                                                           names(claim["addressed_urls"], "positive audit URLs")):
            raise ValueError("Positive audit claim quote or addressed URL is absent from saved source")
    names([row["claim_id"] for row in data.get("positive_claims", [])], "positive audit claim IDs", allow_empty=True)


def rank_number(value, label, *, nullable=False):
    """Parse saved CSV/JSON rank numerics without treating blanks as zero."""
    if value is None or value == "":
        if nullable:
            return None
        raise ValueError(f"saved rank source {label} is missing")
    if isinstance(value, bool):
        raise ValueError(f"saved rank source {label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"saved rank source {label} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"saved rank source {label} must be positive and finite")
    return number


def saved_rank_source_rows(root, research, strict):
    """Project declared rank tables, including an empty table, from saved bytes.

    The source index is an explicit role ledger for *every* declared research
    source. It does not try to guess a provider format from a file name.
    """
    refs = research.get("source_refs")
    index = research.get("source_index")
    if not isinstance(refs, list) or not isinstance(index, list):
        if strict or research.get("rank_rows"):
            raise ValueError("saved rank source needs an explicit source index")
        return None
    declared = names(refs, "research source_refs", allow_empty=True)
    if any(not isinstance(item, dict) for item in index):
        raise ValueError("saved rank source index needs objects")
    if names([item.get("source_ref") for item in index], "research source index", allow_empty=True) != declared:
        raise ValueError("saved rank source index must account for every declared research source")

    status = research.get("rank_source_status")
    if status not in {"available", "unavailable", "pending"}:
        raise ValueError("saved rank source status must be available, unavailable or pending")
    if status == "unavailable":
        if not isinstance(research.get("rank_unavailable_reason"), str) or not research["rank_unavailable_reason"].strip():
            raise ValueError("saved rank source unavailable status needs a reason")
    elif research.get("rank_unavailable_reason"):
        raise ValueError("saved rank source available/pending status cannot assert unavailable reason")

    projected = {}
    rank_sources = 0
    unsupported = False
    for item in index:
        path = relative_file(root, item["source_ref"])
        if sha_file(path) != item.get("sha256"):
            raise ValueError("saved rank source SHA-256 differs")
        role = item.get("role")
        if role == "other":
            continue
        if role != "rank_rows":
            raise ValueError("saved rank source role must be rank_rows or other")
        rank_sources += 1
        fmt = item.get("format")
        if fmt not in {"json_rows", "csv_rows"}:
            unsupported = True
            continue
        if fmt == "json_rows":
            value = read_json(path)
            row_path = item.get("rows_path")
            if not isinstance(row_path, list) or not row_path or any(not isinstance(part, str) or not part for part in row_path):
                raise ValueError("saved rank source JSON needs a rows_path")
            for part in row_path:
                if not isinstance(value, dict) or part not in value:
                    raise ValueError("saved rank source rows_path is absent")
                value = value[part]
            rows = value
        else:
            if item.get("rows_path") is not None:
                raise ValueError("saved rank source CSV cannot have rows_path")
            reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig")))
            rows = list(reader)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("saved rank source needs tabular rows")
        fields = item.get("fields")
        if not isinstance(fields, dict) or any(not isinstance(fields.get(key), str) or not fields[key]
                                                for key in ("observation_id", "query_id", "domain", "url", "position")):
            raise ValueError("saved rank source needs an explicit identity/value field map")
        if fields.get("weight") is not None and not isinstance(fields["weight"], str):
            raise ValueError("saved rank source weight field map is invalid")
        fixed_scope = item.get("scope")
        scope_field = item.get("scope_field")
        if (fixed_scope is None) == (scope_field is None):
            raise ValueError("saved rank source needs exactly one fixed or row scope mapping")
        if scope_field is not None and (fmt != "json_rows" or not isinstance(scope_field, str) or not scope_field):
            raise ValueError("saved rank source row scope needs a JSON field")
        for row in rows:
            ident = row.get(fields["observation_id"])
            if not isinstance(ident, str) or not ident.strip() or ident in projected:
                raise ValueError("saved rank source IDs must be unique and nonempty")
            scope = fixed_scope if fixed_scope is not None else row.get(scope_field)
            if not isinstance(scope, dict) or scope.get("source") != item["source_ref"] or not scope.get("period"):
                raise ValueError("saved rank source row needs its exact dated source scope")
            projected[ident] = {
                "observation_id": ident,
                "query_id": row.get(fields["query_id"]),
                "domain": row.get(fields["domain"]),
                "url": row.get(fields["url"]),
                "position": rank_number(row.get(fields["position"]), "position", nullable=True),
                "weight": rank_number(row.get(fields["weight"]), "weight") if fields.get("weight") and row.get(fields["weight"]) not in (None, "") else 1.0,
                "scope": scope,
                "source_ref": item["source_ref"],
            }
            if not all(isinstance(projected[ident][key], str) and projected[ident][key].strip()
                       for key in ("query_id", "domain", "url")):
                raise ValueError("saved rank source row identity is incomplete")

    if status == "available" and (not rank_sources or unsupported):
        raise ValueError("saved rank source available claim needs supported declared rank tables")
    if status == "unavailable" and rank_sources:
        raise ValueError("saved rank source cannot be unavailable while rank tables are declared")
    if status == "pending" and strict:
        raise ValueError("saved rank source pending/unsupported shape cannot complete research")
    if unsupported:
        if research.get("rank_rows"):
            raise ValueError("saved rank source unsupported shape cannot support included rows")
        return None

    exclusions = research.get("rank_source_exclusions", [])
    if not isinstance(exclusions, list):
        raise ValueError("saved rank source exclusions need a list")
    removed = set()
    for excluded in exclusions:
        if (not isinstance(excluded, dict) or set(excluded) != {"observation_id", "source_ref", "reason", "evidence_ref"}
                or excluded.get("observation_id") not in projected or excluded["observation_id"] in removed
                or excluded.get("source_ref") != projected[excluded["observation_id"]]["source_ref"]
                or not isinstance(excluded.get("reason"), str) or not excluded["reason"].strip()):
            raise ValueError("saved rank source exclusion needs a unique raw row and reason")
        relative_file(root, excluded.get("evidence_ref"))
        removed.add(excluded["observation_id"])
    return {ident: row for ident, row in projected.items() if ident not in removed}


def research_rank_rows(root, research, strict):
    """Normalized saved observations; an empty list is an explicit no-data claim."""
    rows = research.get("rank_rows")
    if rows is None and not strict:
        return []
    if not isinstance(rows, list):
        raise ValueError("Complete research needs explicit rank_rows, even when no ranks are available")
    found = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Research rank observation needs a saved row")
        ident = row.get("observation_id") or row.get("row_id")
        if not all(isinstance(row.get(key), str) and row[key].strip()
                   for key in ("query_id", "domain", "url", "source_ref")) or not isinstance(ident, str) or not ident.strip():
            raise ValueError("Research rank row needs ID, query, domain, URL and saved source")
        if ident in found:
            raise ValueError("Research rank observation IDs must be unique")
        found.add(ident)
        relative_file(root, row["source_ref"])
        if strict and row["source_ref"] not in research.get("source_refs", []):
            raise ValueError("Complete research rank source is absent from declared source_refs")
        slice_scope = row.get("scope")
        if not isinstance(slice_scope, dict) or slice_scope.get("source") != row["source_ref"] or not slice_scope.get("period"):
            raise ValueError("Research rank row needs its exact saved source and dated scope")
        position = row.get("position")
        if position is None:
            if not isinstance(row.get("reason"), str) or not row["reason"].strip():
                raise ValueError("Unobserved research rank stays null with reason")
        elif isinstance(position, bool) or not isinstance(position, (int, float)) or not math.isfinite(position) or position <= 0:
            raise ValueError("Observed research rank position must be finite and positive")
    saved = saved_rank_source_rows(root, research, strict)
    if saved is not None:
        normalized = {row.get("observation_id") or row.get("row_id"): row for row in rows}
        if set(normalized) != set(saved):
            raise ValueError("saved rank source rows differ from normalized research IDs or completeness")
        for ident, source in saved.items():
            row = normalized[ident]
            if (row["query_id"] != source["query_id"] or row["domain"] != source["domain"]
                    or row["url"] != source["url"] or row["position"] != source["position"]
                    or row.get("weight", 1) != source["weight"] or row["source_ref"] != source["source_ref"]
                    or row["scope"] != source["scope"]):
                raise ValueError("saved rank source row differs from normalized research identity/value/scope")
    return rows


def research_source_claims(root, refs, research):
    """The pre-research source inventory fixes roles before normalized output."""
    snapshot = checked_object(root, refs["inputs"]["research_snapshot"], "research_snapshot")
    source_refs = snapshot.get("source_refs")
    roles = snapshot.get("source_roles")
    if not isinstance(source_refs, list) or not isinstance(roles, dict):
        raise ValueError("Research snapshot needs explicit source refs and roles")
    declared = names(source_refs, "research snapshot source refs", allow_empty=True)
    if set(roles) != declared or any(role not in {"rank_rows", "other"} for role in roles.values()):
        raise ValueError("Research snapshot source roles must cover every declared source")
    if names(research.get("source_refs"), "research output source refs", allow_empty=True) != declared:
        raise ValueError("Research output cannot forget a declared raw rank source")
    index = research.get("source_index")
    if not isinstance(index, list) or {item.get("source_ref"): item.get("role") for item in index if isinstance(item, dict)} != roles:
        raise ValueError("Research output cannot change a declared raw rank source role")


def rank_handoff_scaffold(root, state):
    research = previous_output(root, state, "research", "research_findings")
    rows = research_rank_rows(root, research, False)
    own_domain = urlsplit(previous_output(root, state, "intake", "scope")["site"]).hostname
    def listed(own):
        return [
        {"observation_id": row.get("observation_id") or row.get("row_id"), "query_id": row["query_id"],
         "domain": row["domain"], "url": row["url"], "source_ref": row["source_ref"],
         "scope": row["scope"], "choice": None}
        for row in rows if row.get("position") is not None and (row["domain"] == own_domain) is own]
    return {"recordable": False, "own_observations": listed(True),
            "peer_observations": listed(False),
        "choices": ["include_in_matching_rankings", "exclude_with_saved_reason"]}


def own_rank_url_identity(url, client_domain):
    if not isinstance(url, str) or not url:
        raise ValueError("GAP own rank needs a canonical URL")
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or parsed.hostname != client_domain:
            raise ValueError("GAP own rank URL is outside the client site")
    if not parsed.path.startswith("/") or parsed.fragment:
        raise ValueError("GAP own rank needs an exact canonical path")
    return parsed.path.rstrip("/") or "/", parsed.query


def own_rank_aliases(root, site_input):
    """Resolve observed own URLs only through exact saved crawl/inventory rows."""
    domain = site_input.get("client_domain")
    aliases = site_input.get("own_rank_url_aliases", [])
    if not isinstance(aliases, list):
        raise ValueError("GAP own rank aliases need a list")
    mapped = {}
    for item in aliases:
        if (not isinstance(item, dict) or item.get("source_kind") not in {"saved_inventory", "saved_crawl"}
                or item.get("format") != "json_rows" or not isinstance(item.get("rows_path"), list)
                or not item["rows_path"] or any(not isinstance(part, str) or not part for part in item["rows_path"])):
            raise ValueError("GAP own rank alias needs a supported saved inventory/crawl row locator")
        fields = item.get("fields")
        if (not isinstance(fields, dict) or not isinstance(fields.get("observed_url"), str)
                or not isinstance(fields.get("canonical_url"), str)
                or not isinstance(fields.get("observed_page_type"), str)):
            raise ValueError("GAP own rank alias needs observed/canonical URL and physical type field mapping")
        path = relative_file(root, item.get("source_ref"))
        if path.suffix.lower() != ".json" or sha_file(path) != item.get("source_sha256"):
            raise ValueError("GAP own rank alias saved source SHA-256 differs")
        value = read_json(path)
        for part in item["rows_path"]:
            if not isinstance(value, dict) or part not in value:
                raise ValueError("GAP own rank alias rows_path is absent")
            value = value[part]
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise ValueError("GAP own rank alias source needs rows")
        observed = own_rank_url_identity(item.get("observed_url"), domain)
        canonical = own_rank_url_identity(item.get("canonical_url"), domain)
        matches = [row for row in value if row.get(fields["observed_url"]) == item["observed_url"]]
        if (len(matches) != 1 or own_rank_url_identity(matches[0].get(fields["canonical_url"]), domain) != canonical
                or observed in mapped):
            raise ValueError("GAP own rank alias differs from its unique saved source row")
        observed_type = matches[0].get(fields["observed_page_type"])
        if not isinstance(observed_type, str) or not observed_type.strip():
            raise ValueError("GAP own rank alias saved row lacks observed physical page type")
        mapped[observed] = (canonical, observed_type)
    return mapped


def rank_handoff_checks(root, state, site_input, strict):
    research = previous_output(root, state, "research", "research_findings")
    rows = research_rank_rows(root, research, strict)
    own_domain = site_input.get("client_domain")
    all_rows = {row.get("observation_id") or row.get("row_id"): row for row in rows}
    expected = {ident: row for ident, row in all_rows.items() if row.get("position") is not None}
    included = {}
    inventory = site_input.get("inventory", {})
    own_pages = {}
    for page in inventory.get("pages", []):
        identity = own_rank_url_identity(page.get("canonical_url"), own_domain)
        if identity in own_pages:
            raise ValueError("GAP own canonical rank inventory is duplicated")
        own_pages[identity] = page
    own_aliases = own_rank_aliases(root, site_input)
    own_rank_set = site_input.get("rankings") or {}
    for row in own_rank_set.get("serp", []):
        ident = row.get("observation_id")
        if ident not in all_rows or ident in included or all_rows[ident]["domain"] != own_domain:
            raise ValueError("GAP own rank is unknown or duplicated against saved research observations")
        source = all_rows[ident]
        observed_identity = own_rank_url_identity(source["url"], own_domain)
        alias = own_aliases.get(observed_identity)
        if (own_rank_url_identity(row.get("canonical_url"), own_domain)
                != (alias[0] if alias else observed_identity)):
            raise ValueError("GAP own rank differs from saved research URL identity")
        physical = own_pages.get(own_rank_url_identity(row["canonical_url"], own_domain))
        if physical is None or (row.get("page_type") is not None and row["page_type"] != physical.get("page_type")):
            raise ValueError("GAP own rank needs its physical canonical inventory page type")
        observed_type = alias[1] if alias else physical.get("page_type")
        if row.get("observed_page_type", observed_type) != observed_type:
            raise ValueError("GAP own rank observed physical type differs from saved source")
        if alias and row.get("observed_page_type") != observed_type:
            raise ValueError("GAP own rank alias must retain its saved observed physical type")
        if (row.get("query_id") != source["query_id"] or row.get("position") != source["position"]
                or row.get("weight", 1) != source.get("weight", 1)):
            raise ValueError("GAP own rank differs from saved research row identity or position")
        scope = own_rank_set.get("scope")
        if not isinstance(scope, dict) or any(scope.get(key) != value for key, value in source["scope"].items()):
            raise ValueError("GAP own rank source or slice differs from saved research observation")
        included[ident] = row
    for peer in site_input.get("competitors", []):
        rank_set = peer.get("rankings") or {}
        physical_by_url = {}
        for page in peer.get("pages", []):
            url = page.get("canonical_url")
            if not isinstance(url, str) or not url or url in physical_by_url or not isinstance(page.get("page_type"), str):
                raise ValueError("GAP peer physical pages need unique typed canonical URLs")
            physical_by_url[url] = page["page_type"]
        peer_types = peer.get("type_counts")
        if peer.get("type_counts_complete") is True and isinstance(peer_types, dict) and peer.get("count") == len(physical_by_url):
            actual = {kind: sum(value == kind for value in physical_by_url.values()) for kind in peer_types}
            if actual != peer_types:
                raise ValueError("GAP peer physical page types differ from complete type counts")
        for row in rank_set.get("serp", []):
            ident = row.get("observation_id")
            if ident not in all_rows or ident in included or all_rows[ident]["domain"] == own_domain:
                raise ValueError("GAP peer rank is unknown or duplicated against saved research observations")
            source = all_rows[ident]
            scope_keys = ("definition", "source", "period", "engine", "region", "device", "segment", "rank_threshold")
            inventory_scope = site_input.get("inventory", {}).get("scope", {})
            page_kind = physical_by_url.get(row.get("canonical_url"))
            if page_kind is None or (row.get("page_type") is not None and row["page_type"] != page_kind):
                raise ValueError("GAP peer rank type differs from its physical ranking URL page")
            if (peer.get("relevant") is not True or peer.get("complete") is not True
                    or peer.get("type_counts_complete") is not True or site_input.get("inventory", {}).get("complete") is not True
                    or not isinstance(peer_types, dict) or not isinstance(page_kind, str)
                    or isinstance(peer_types.get(page_kind), bool) or not isinstance(peer_types.get(page_kind), int)
                    or peer_types[page_kind] < 1
                    or not isinstance(peer.get("scope"), dict) or not isinstance(inventory_scope, dict)
                    or any(peer["scope"].get(key) != inventory_scope.get(key) for key in scope_keys)):
                raise ValueError("Included GAP peer rank cannot produce a comparable typed descriptive result")
            if (peer.get("domain") != source["domain"] or row.get("canonical_url") != source["url"]
                    or row.get("query_id") != source["query_id"] or row.get("position") != source["position"]
                    or row.get("weight", 1) != source.get("weight", 1)):
                raise ValueError("GAP peer rank differs from saved research row identity or position")
            scope = rank_set.get("scope")
            if not isinstance(scope, dict) or any(scope.get(key) != value for key, value in source["scope"].items()):
                raise ValueError("GAP peer rank source or slice differs from saved research observation")
            included[ident] = row
    exclusions = site_input.get("rank_observation_exclusions", [])
    if not isinstance(exclusions, list):
        raise ValueError("GAP rank exclusions need an explicit list")
    excluded = set()
    for item in exclusions:
        if not isinstance(item, dict) or set(item) != {"observation_id", "reason", "evidence_ref"}:
            raise ValueError("GAP rank exclusion needs row ID, reason and saved evidence")
        ident = item["observation_id"]
        if ident not in all_rows or ident in included or ident in excluded or not isinstance(item["reason"], str) or not item["reason"].strip():
            raise ValueError("GAP rank exclusion is unknown, duplicate or unexplained")
        if not isinstance(item["evidence_ref"], str):
            raise ValueError("GAP rank exclusion evidence must be a saved file")
        relative_file(root, item["evidence_ref"])
        excluded.add(ident)
    if strict and set(expected) != (set(included) | excluded) & set(expected):
        raise ValueError("Complete GAP silently omits saved own or peer rank observations")


def partial_stage_content_checks(root, state, refs, node_id, scope):
    data = checked_object(root, refs["outputs"][PARTIAL_OUTPUT[node_id]], PARTIAL_OUTPUT[node_id])
    if node_id == "intake":
        if not data.get("agency_id") or not data.get("site") or data.get("geo") != scope["geo"] or not data.get("goals"):
            raise ValueError("Partial intake still needs sourced identity, site and goals")
    elif node_id == "audit":
        sources = checked_object(root, refs["inputs"]["audit_sources"], "audit_sources")
        access = data.get("source_access")
        if not isinstance(access, dict) or any(value not in {"available", "partial", "unavailable"} for value in access.values()):
            raise ValueError("Partial audit needs explicit per-source availability")
        if access.get("gsc") != "available" and not any(isinstance(row, dict) and row.get("source_system") == "gsc"
                and row.get("check") == "index_status" and row.get("reason") for row in data.get("unverified_checks", [])):
            raise ValueError("Unavailable audit GSC index status needs addressed unverified check")
        for resource in sources.get("resources", []):
            audit_resource_target(root, resource)
        for item in sources.get("source_artifacts", []):
            if not isinstance(item, dict) or not item.get("artifact_ref") or item.get("sha256") != sha_file(relative_file(root, item["artifact_ref"])):
                raise ValueError("Partial audit saved source artifact SHA differs")
        for item in sources.get("inspection_receipts", []):
            if not isinstance(item, dict) or not item.get("response_ref") or item.get("response_sha256") != sha_file(relative_file(root, item["response_ref"])):
                raise ValueError("Partial audit inspection response SHA differs")
        findings = data.get("findings")
        if not isinstance(findings, list):
            raise ValueError("Partial audit needs explicit known findings list")
        names([row.get("finding_id") for row in findings], "audit finding IDs", allow_empty=True)
        for row in findings:
            if not row.get("source_ref") or not row.get("url") or not row.get("action") or not row.get("acceptance"):
                raise ValueError("Known audit finding lacks source, target, change or acceptance")
            if row.get("supporting_quote") and row["supporting_quote"] not in relative_file(root, row["source_ref"]).read_text(encoding="utf-8-sig"):
                raise ValueError("Known partial audit finding quote is absent from saved source")
    elif node_id == "analytics":
        if not isinstance(data.get("periods"), list) or not isinstance(data.get("segments"), dict):
            raise ValueError("Partial analytics needs explicit known periods and segments")
        denominator = data.get("denominator")
        if denominator is not None and (not isinstance(denominator, (int, float)) or denominator <= 0):
            raise ValueError("Known analytics denominator must be positive")
        if data["periods"] and (len(data["periods"]) < 2 or denominator is None or not data["segments"]):
            raise ValueError("Available analytics periods need denominator and segment basis")
        if not data["periods"] and not any(item["state"] in {"unavailable", "unknown"}
                                               and item["counted_in"] in {"sources", "stage"}
                                               for item in data["pending_items"]):
            raise ValueError("Unavailable analytics periods need explicit source availability item")
    elif node_id == "research":
        competitors = data.get("competitors")
        if not isinstance(competitors, list) or any(not row.get("selection_reason") or not row.get("source_ref") for row in competitors):
            raise ValueError("Known research peers need source and selection basis")
        research_source_claims(root, refs, data)
        research_rank_rows(root, data, False)
    elif node_id == "commercial":
        observations = data.get("observations")
        if not isinstance(observations, list):
            raise ValueError("Partial commercial findings need explicit observations")
        names([row.get("observation_id") for row in observations], "commercial observation IDs", allow_empty=True)
        if any(not all(row.get(k) for k in ("url", "factor", "evidence_ref", "change")) for row in observations):
            raise ValueError("Known commercial observation lacks source, target or change")
    elif node_id == "geo":
        surfaces = data.get("surfaces")
        if not isinstance(surfaces, list) or any(not row.get("surface") or not row.get("query") for row in surfaces):
            raise ValueError("Partial GEO needs explicit known surface queries")
    elif node_id == "company_cases":
        if not isinstance(data.get("cases"), list) or not isinstance(data.get("company_sources"), list):
            raise ValueError("Partial company proof needs explicit sources and case list")
        if any(not row.get("source_ref") or not row.get("relevance") or not row.get("limitation") for row in data["cases"]):
            raise ValueError("Known case lacks source, relevance or limitation")
    elif node_id == "decision_map":
        strategy = previous_output(root, state, "strategy", "strategy_map")
        if data.get("source_dispositions") != ledger_projection(strategy):
            raise ValueError("Partial decision map loses known source dispositions")
        if data.get("bar_readings") != bar_reading_projection(root, state, strategy):
            raise ValueError("Partial decision map loses scoped BAR readings")
        measurement_checks(data.get("measurement_plan"))
        measurement_receipt_checks(root, data["measurement_plan"])
    elif node_id == "structure_review":
        receipt = checked_object(root, refs["inputs"]["review_receipt"], "review_receipt")
        if data.get("candidate_sha256") != receipt.get("candidate_sha256") or data.get("verdict") != receipt.get("verdict"):
            raise ValueError("Partial structure verdict differs from review receipt")
    elif node_id == "proposal":
        strategy = previous_output(root, state, "strategy", "strategy_map")
        if data.get("source_dispositions") != ledger_projection(strategy):
            raise ValueError("Partial packet loses known source dispositions")
        if data.get("proposal_sha256") != sha_file(relative_file(root, refs["outputs"]["proposal_document"])):
            raise ValueError("Partial packet differs from saved proposal document")
    elif node_id == "client_review":
        receipt = checked_object(root, refs["inputs"]["review_receipt"], "review_receipt")
        if data.get("candidate_sha256") != receipt.get("candidate_sha256") or data.get("verdict") != receipt.get("verdict"):
            raise ValueError("Partial client verdict differs from review receipt")
    elif node_id == "release_gate":
        if data.get("content_scope_complete") is True or data.get("user_acceptance") == "accepted":
            raise ValueError("Partial release cannot assert complete content or user acceptance")


def stage_content_checks(root, state, refs, node_id, scope, strict):
    if node_id == "decision_map":
        decision_data = checked_object(root, refs["outputs"]["decision_map"], "decision_map")
        release_contract_checks(root, state, decision_data)
        scope_handoff_checks(root, state, decision_data)
    if not strict:
        partial_stage_content_checks(root, state, refs, node_id, scope)
        return
    output = refs["outputs"]
    def obj(key):
        return checked_object(root, output[key], key)
    if node_id == "intake":
        data = obj("scope")
        if scope["geo"] == "undecided":
            raise ValueError("Intake cannot be complete while GEO decision is pending")
        if not data.get("agency_id") or not data.get("site") or data.get("geo") != scope["geo"] or not data.get("goals") or not data.get("source_refs"):
            raise ValueError("Intake scope lacks agency, site, geo, goals or source refs")
    elif node_id == "audit":
        audit_stage_checks(root, state, refs, obj("audit_findings"))
    elif node_id == "analytics":
        data = obj("analytics_findings")
        if not isinstance(data.get("periods"), list) or len(data["periods"]) < 2 or not isinstance(data.get("denominator"), (int, float)) or data["denominator"] <= 0 or not data.get("segments") or data.get("sales_status") not in {"measured", "unknown"}:
            raise ValueError("Analytics needs comparable periods, denominator, segments and sales status")
    elif node_id == "research":
        data = obj("research_findings")
        if not data.get("competitors") or not data.get("region") or not data.get("snapshot_date") or not data.get("metric_definition"):
            raise ValueError("Research lacks competitors or comparable slice definition")
        if any(not row.get("source_ref") or not row.get("selection_reason") for row in data["competitors"]):
            raise ValueError("Research competitor lacks source or selection reason")
        research_source_claims(root, refs, data)
        research_rank_rows(root, data, True)
    elif node_id == "commercial":
        data = obj("commercial_findings")
        if not data.get("observations") or any(not all(row.get(k) for k in ("observation_id", "url", "factor", "evidence_ref", "change")) for row in data["observations"]):
            raise ValueError("Commercial factors need URL, evidence and change")
        names([row["observation_id"] for row in data["observations"]], "commercial observation IDs")
    elif node_id == "geo":
        data = obj("geo_findings")
        surfaces = data.get("surfaces")
        if not isinstance(surfaces, list) or len(surfaces) != 3 or any(not isinstance(row, dict) for row in surfaces) or {row.get("surface") for row in surfaces} != {"yandex", "google", "chatgpt"} or any(not row.get("query") or not row.get("date") or not row.get("answer_ref") for row in surfaces):
            raise ValueError("GEO needs three dated real answer surfaces")
        surface_ids = names([row.get("surface_id") for row in surfaces], "GEO surface IDs")
        findings = data.get("findings")
        if not isinstance(findings, list):
            raise ValueError("GEO needs an explicit findings list, even when empty")
        material_by_surface = {surface_id: 0 for surface_id in surface_ids}
        finding_ids = []
        for finding in findings:
            if not isinstance(finding, dict) or finding.get("surface_id") not in surface_ids or finding.get("materiality") not in {"material", "not_material"} or not finding.get("finding_id") or not finding.get("conclusion") or not finding.get("evidence_refs"):
                raise ValueError("GEO finding needs stable ID, surface, conclusion, evidence and materiality")
            names(finding["evidence_refs"], "GEO finding evidence")
            if finding["materiality"] == "material":
                if not finding.get("action") or not finding.get("acceptance"):
                    raise ValueError("Material GEO finding needs proposed action and acceptance")
                material_by_surface[finding["surface_id"]] += 1
            elif not finding.get("reason"):
                raise ValueError("Nonmaterial GEO observation needs reason")
            finding_ids.append(finding["finding_id"])
        names(finding_ids, "GEO finding IDs", allow_empty=True)
        for surface in surfaces:
            assessment = surface.get("assessment")
            observed = material_by_surface[surface["surface_id"]]
            if (assessment == "material_findings" and observed < 1) or (assessment == "no_material_finding" and (observed or not surface.get("assessment_reason"))) or assessment not in {"material_findings", "no_material_finding"}:
                raise ValueError("Every GEO surface needs reconciled material assessment or reasoned no-finding")
    elif node_id == "company_cases":
        data = obj("company_case_pack")
        if not data.get("company_sources") or not data.get("why_resultup") or not isinstance(data.get("cases"), list):
            raise ValueError("Company/cases need sourced company facts and case list")
        if any(not row.get("source_ref") or not row.get("relevance") or not row.get("limitation")
               or row.get("fit_status") not in {"relevant_confirmed", "illustrative_only"} for row in data["cases"]):
            raise ValueError("Case lacks source, relevance or limitation")
        if not any(row["fit_status"] == "relevant_confirmed" for row in data["cases"]):
            field = data.get("human_case_field")
            if (not isinstance(field, dict) or field.get("status") != "pending"
                    or not field.get("label") or not field.get("reason")):
                raise ValueError("No confirmed relevant case requires a human case field with reason")
    elif node_id == "decision_map":
        data = obj("decision_map")
        measurement_checks(data.get("measurement_plan"))
        measurement_receipt_checks(root, data["measurement_plan"])
        strategy = previous_output(root, state, "strategy", "strategy_map")
        implementation = previous_output(root, state, "implementation", "implementation_plan")
        estimate = previous_output(root, state, "estimate", "estimate_sheet")
        estimate_lines = estimate_line_checks(implementation, estimate)
        if data.get("source_dispositions") != ledger_projection(strategy):
            raise ValueError("Decision map must preserve every source disposition")
        if data.get("bar_readings") != bar_reading_projection(root, state, strategy):
            raise ValueError("Decision map must preserve scoped BAR readings and decisions")
        nonselected = {row["source_id"] for row in strategy["decision_ledger"] if row["disposition"] != "selected"}
        if names(data.get("nonselected_source_ids"), "decision map nonselected sources", allow_empty=True) != nonselected:
            raise ValueError("Decision map must carry reasoned nonselected source IDs")
        rows = data.get("decisions")
        if not isinstance(rows, list) or {row.get("action_id") for row in rows} != set(strategy["selected_action_ids"]):
            raise ValueError("Decision map must cover selected actions")
        if any(not all(row.get(k) for k in ("a_evidence_ref", "gap_evidence_ref", "b_metric", "acceptance", "estimate_ref")) for row in rows):
            raise ValueError("Decision map lacks A→GAP→B or estimate binding")
        if any(row["estimate_ref"] not in estimate_lines
               or row["action_id"] not in estimate_lines[row["estimate_ref"]]["action_ids"] for row in rows):
            raise ValueError("Decision estimate reference does not resolve to this action and unit")
    elif node_id == "structure_review":
        data = obj("structure_verdict")
        receipt = checked_object(root, refs["inputs"]["review_receipt"], "review_receipt")
        if data.get("verdict") != receipt.get("verdict") or data.get("candidate_sha256") != receipt.get("candidate_sha256"):
            raise ValueError("Structure verdict output contradicts review receipt")
    elif node_id == "proposal":
        packet = obj("client_packet")
        strategy = previous_output(root, state, "strategy", "strategy_map")
        if packet.get("source_dispositions") != ledger_projection(strategy):
            raise ValueError("Client packet must preserve every source disposition")
        nonselected = {row["source_id"] for row in strategy["decision_ledger"] if row["disposition"] != "selected"}
        if names(packet.get("nonselected_source_ids"), "client packet nonselected sources", allow_empty=True) != nonselected:
            raise ValueError("Client packet must carry reasoned nonselected source IDs")
        expected = ["A", "GAP", "SEO", "B3", "B6", "B12", "implementation", "team", "estimate", "company_cases"]
        if output["proposal_document"] != "proposal.md":
            raise ValueError("Complete proposal must use canonical proposal.md")
        document_path = relative_file(root, output["proposal_document"])
        declared_order = packet.get("section_order")
        if declared_order != expected or packet.get("proposal_sha256") != sha_file(document_path):
            raise ValueError("Proposal order or client packet binding is incomplete")
        if packet.get("packet_receipt_ref") != "packet-receipt.json":
            raise ValueError("Workflow packet must bind canonical packet receipt")
        main_receipt = checked_object(root, packet["packet_receipt_ref"], "packet-receipt")
        manifest_ref = str(Path(main_receipt.get("path", "")) / "manifest.json").replace("\\", "/")
        if packet.get("packet_manifest_ref") != manifest_ref:
            raise ValueError("Workflow packet manifest path differs from main packet")
        manifest = checked_object(root, manifest_ref, "packet manifest")
        packet_sha = sha_file(relative_file(root, manifest_ref))
        if packet.get("packet_sha256") != packet_sha or main_receipt.get("packet_sha256") != packet_sha:
            raise ValueError("Workflow packet manifest SHA differs from main packet")
        if manifest.get("proposal_sha256") != packet["proposal_sha256"] or manifest.get("files", {}).get("proposal.md") != packet["proposal_sha256"]:
            raise ValueError("Main packet proposal SHA differs from canonical proposal")
        if sha_file(relative_file(root, str(Path(main_receipt["path"]) / "proposal.md"))) != packet["proposal_sha256"]:
            raise ValueError("Client packet carries another proposal")
        files = manifest.get("files")
        if not isinstance(files, dict) or not files or any(not isinstance(name, str) or not isinstance(digest, str) for name, digest in files.items()):
            raise ValueError("Packet manifest needs file SHA-256 entries")
        for name, digest in files.items():
            if sha_file(relative_file(root, str(Path(main_receipt["path"]) / name))) != digest:
                raise ValueError("Packet manifest file changed: " + name)
        client_handoff_checks(root, state, packet, main_receipt["path"], files)
        decision = previous_output(root, state, "decision_map", "decision_map")
        material_refs = client_material_refs(root, state, decision)
        missing = [row["path"] for row in material_refs if row["sha256"] not in set(files.values())]
        if missing:
            raise ValueError("Final client packet omits material structure evidence: " + ", ".join(missing))
    elif node_id == "client_review":
        data = obj("client_verdict")
        receipt = checked_object(root, refs["inputs"]["review_receipt"], "review_receipt")
        if data.get("verdict") != receipt.get("verdict") or data.get("content_verdict") != receipt.get("content_verdict") or data.get("candidate_sha256") != receipt.get("candidate_sha256") or data.get("proposal_sha256") != receipt.get("proposal_sha256") or data.get("packet_sha256") != receipt.get("packet_sha256"):
            raise ValueError("Client verdict output contradicts review receipt")


def current(root, state, raw, nodes, order):
    """Recalculate validity from disk and graph; never trust saved status."""
    result = {}
    scope_path = relative_file(root, state["scope_ref"])
    scope = read_json(scope_path)
    for node_id in order:
        node = nodes[node_id]
        if not active(node, scope):
            result[node_id] = {"state": "decision_pending" if scope.get("geo") == "undecided" else "out_of_scope",
                               "reason": "geo undecided" if scope.get("geo") == "undecided" else "geo=no"}
            continue
        attempts = state["attempts"].get(node_id, [])
        if not attempts:
            result[node_id] = {"state": "pending", "reason": "no attempt"}
            continue
        a = attempts[-1]
        reasons = []
        if a.get("contract_version") != raw["contract_version"]:
            reasons.append("workflow contract version changed")
        if node_id in state.get("invalidated", {}):
            reasons.append("explicit invalidation")
        if (a.get("node_contract_sha256") != sha_bytes(canonical(node))
                or a.get("shared_policy_sha256") != policy_sha(raw)
                or a.get("compute_sha256") != compute_sha(node_id)
                or a["scope_sha256"] != scope_fingerprint(node_id, scope)):
            reasons.append("node contract, shared policy, compute or scope changed")
        for dep in required_parents(node, nodes, scope):
            dep_state = result[dep]
            if dep_state["state"] not in {"complete", "partial", "skipped"}:
                reasons.append(f"dependency {dep} is {dep_state['state']}")
            elif a["dependencies"].get(dep) != dependency_signature(root, state, dep, node_id, dep_state["signature"]):
                reasons.append(f"dependency {dep} signature changed")
        for kind in ("inputs", "outputs"):
            for label, ref in a[kind].items():
                try:
                    if sha_file(relative_file(root, ref["path"])) != ref["sha256"]:
                        reasons.append(f"{kind}:{label} hash changed")
                except ValueError:
                    reasons.append(f"{kind}:{label} missing")
        for binding in a.get("consumed_sources", []):
            try:
                if sha_file(relative_file(root, binding["path"])) != binding["sha256"]:
                    reasons.append(f"consumed source:{binding['path']} hash changed")
            except ValueError:
                reasons.append(f"consumed source:{binding['path']} missing")
        if (a.get("contract_version") == raw["contract_version"] and a.get("status") in {"complete", "partial"}
                and not reasons):
            try:
                declared = {"inputs": {label: a["inputs"][label]["path"] for label in node["inputs"]},
                            "outputs": {label: a["outputs"][label]["path"] for label in node["outputs"]}}
                if canonical(consumed_source_bindings(root, declared, node_id)) != canonical(a.get("consumed_sources")):
                    reasons.append("consumed source declaration differs from current source collector")
            except (ValueError, KeyError, TypeError):
                reasons.append("consumed source declaration cannot be reverified")
        if reasons:
            result[node_id] = {"state": "invalidated", "reason": "; ".join(reasons)}
        else:
            result[node_id] = {"state": a["status"], "signature": a["signature"], "reason": a.get("reason", "")}
            if a.get("contract_version") == raw["contract_version"]:
                result[node_id]["validation"] = a.get("validation", {"known_scope_valid": False})
    required = [n for n in order if active(nodes[n], scope)]
    full_scope = scope.get("geo") != "undecided" and all(result[n]["state"] == "complete" for n in required)
    # A release receipt may be complete only when its predecessors are complete.
    return scope, result, full_scope


def previous_output(root, state, node_id, key):
    try:
        path = state["attempts"][node_id][-1]["outputs"][key]["path"]
    except (KeyError, IndexError) as error:
        raise ValueError(f"Missing upstream output: {node_id}.{key}") from error
    return checked_object(root, path, f"{node_id}.{key}")


def bar_native_pointer_domain(report, parts, zones):
    """Allow only current BAR text analysis and corpus-limit fields, never service metadata."""
    if (not parts or not all(isinstance(zone, str) for zone in zones)
            or any(zone in parts[1:] and zone not in zones
                        for zone in ("all", "text", "title", "h1", "headings", "anchors"))):
        return False
    head = parts[0]
    index = lambda value: value.isdigit() and (value == "0" or not value.startswith("0"))
    cloud_fields = {"key", "word", "count", "weight", "frequency", "sites"}
    term_fields = {"key", "term", "phrase", "sites", "df", "coverage", "ownCount",
                   "ownFrequency", "medianCount", "q25Count", "q75Count", "densityFactor",
                   "benchmarkWords", "ownWords", "medianFrequency", "q25", "q75",
                   "averageFrequency", "idf", "competitorTfidf", "ownTfidf",
                   "tfidfNormalized", "keySimilarity", "importance", "priority",
                   "keywordHit", "bestKeyword", "keyMatch", "status"}
    zone_fields = {"available", "sites", "ownCount", "medianCount", "q25Count",
                   "q75Count", "densityFactor", "benchmarkWords", "ownWords",
                   "importance", "status"}
    nesting_fields = {"classification", "evidenceComplete", "occurrences",
                      "standaloneOccurrences", "nestedOccurrences", "nestedShare", "mergeable",
                      "mergeParentKey", "mergeParentPhrase", "ownClassification",
                      "ownEvidenceComplete", "ownOccurrences", "ownStandaloneOccurrences",
                      "ownNestedOccurrences", "ownNestedShare", "ownMergeParentOccurrences",
                      "ownMergeParentShare", "ownMergeParentComplete"}
    if head == "coverage":
        return (len(parts) == 2 and parts[1] in {
            "input_pages", "comparable_peer_pages", "analyzed_peer_pages", "excluded_peer_pages",
            "warning_count", "preparation_warning_count", "confidence", "language"})
    if report.get("mode") == "own_vs_peers":
        if head == "own_score":
            return len(parts) == 2 and parts[1] in {
                "key_tfidf_score", "weighted_coverage", "weighted_density"}
        if head != "report" or len(parts) < 3:
            return False
        section = parts[1]
        if section == "summary":
            return len(parts) == 3 and parts[2] in {
                "keyTfidfScore", "keyTfidfRaw", "weightedCoverage", "weightedDensity",
                "ownWords", "medianWords", "recommendedMin", "recommendedMax", "recommendation"}
        if section == "meta":
            return len(parts) == 3 and parts[2] in {
                "competitorsFound", "competitorsRequested", "competitorsAnalyzed",
                "confidence", "language", "phraseSimilarityCoverage", "phraseSimilarityError"}
        if section == "warnings":
            return len(parts) == 4 and index(parts[2]) and parts[3] in {"code", "error"}
        if section == "documents":
            return (len(parts) == 4 and index(parts[2]) and parts[3] in {
                "words", "contentWords", "uniqueTerms", "headings", "sentences", "truncated",
                "qualityScore", "keyTfidfScore", "keyTfidfRaw", "weightedCoverage", "weightedDensity"})
        if section == "comparisonProfiles":
            if len(parts) < 4 or not index(parts[2]):
                return False
            field = parts[3]
            if field == "wordCount":
                return len(parts) == 4
            if field == "cloud":
                return len(parts) == 6 and index(parts[4]) and parts[5] in cloud_fields
            if field == "lemmas":
                return ((len(parts) == 6 and index(parts[4])
                         and parts[5] in {"key", "lemma", "count", "frequency"})
                        or (len(parts) == 8 and index(parts[4]) and parts[5] == "zones"
                            and parts[7] in {"available", "count", "frequency"}))
            if field == "cloudZones":
                return (len(parts) == 7 and index(parts[5]) and parts[6] in cloud_fields)
            return False
        if section in {"terms", "lemmas", "ngrams"}:
            if len(parts) < 4 or not index(parts[2]):
                return False
            field = parts[3]
            return ((len(parts) == 4 and field in term_fields)
                    or (len(parts) == 5 and field == "forms" and index(parts[4]))
                    or (len(parts) == 6 and field == "zones" and parts[5] in zone_fields)
                    or (section == "ngrams" and len(parts) == 5 and field == "nesting"
                        and parts[4] in nesting_fields)
                    or (section == "ngrams" and len(parts) == 7 and field == "nesting"
                        and parts[4] == "parents" and index(parts[5])
                        and parts[6] in {"key", "phrase", "occurrences", "sites", "share"}))
        if section == "chart":
            return (len(parts) == 4 and index(parts[2])
                    and parts[3] in {"term", "competitors", "own", "q25", "q75"})
        if section == "clouds":
            return ((len(parts) == 5 and parts[2] in {"own", "competitors"}
                     and index(parts[3]) and parts[4] in cloud_fields)
                    or (len(parts) == 7 and parts[2] == "zones"
                        and parts[4] in {"own", "competitors"} and index(parts[5])
                        and parts[6] in cloud_fields))
        return False
    if report.get("mode") == "peer_only":
        if head == "limitation":
            return len(parts) == 1
        if head != "peer_benchmark" or len(parts) < 2:
            return False
        section = parts[1]
        if section in {"peer_median_words", "recommended_words_min", "recommended_words_max"}:
            return len(parts) == 2
        if section == "reference_metrics":
            return len(parts) == 3 and parts[2] in {
                "key_tfidf_score", "weighted_coverage", "weighted_density", "words"}
        if section in {"term_requirements", "phrase_requirements"}:
            return (len(parts) == 4 and parts[2].isdigit() and parts[3] in {
                "key", "label", "importance", "peer_reference_count", "peer_median_count",
                "peer_q25_count", "peer_q75_count", "peer_sites"})
        if section == "warnings":
            return len(parts) == 4 and parts[2].isdigit() and parts[3] in {"code", "error"}
        if section == "meta":
            return len(parts) == 3 and parts[2] in {
                "competitors_analyzed", "confidence", "language"}
    return False


def bar_reading_checks(root, register, coverage, row):
    """Check one analyst handoff's shape and exact current text source, not its meaning."""
    reading = coverage.get("reading")
    required = {"note", "source_pointer", "applicability", "text_disposition"}
    if (not isinstance(reading, dict) or not required <= set(reading)
            or set(reading) - required - {"proposed_text_change"}):
        raise ValueError("Scored BAR scope needs a bounded reading object")
    for key in required - {"source_pointer"}:
        value = reading[key]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError("BAR reading prose must be nonempty and trimmed")
    if "proposed_text_change" in reading:
        value = reading["proposed_text_change"]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError("BAR proposed text change must be nonempty and trimmed")
    pointer = reading["source_pointer"]
    if not isinstance(pointer, dict):
        raise ValueError("BAR reading source pointer must be tagged")
    files = register["files"]
    if pointer.get("kind") == "native_report":
        if set(pointer) != {"kind", "pointer"} or not isinstance(pointer["pointer"], str) or not pointer["pointer"].startswith("/"):
            raise ValueError("BAR reading needs a precise native JSON pointer")
        ref = row.get("adapter_report_ref")
        if ref not in files or sha_file(relative_file(root, ref)) != files[ref]:
            raise ValueError("BAR reading native report is not the current saved row report")
        report = checked_object(root, ref, "BAR reading native report")
        zones = report.get("coverage", {}).get("available_zones", [])
        if not isinstance(zones, list) or not set(zones) & {"all", "text"}:
            raise ValueError("BAR reading report has no available all/text zone")
        if not bar_native_pointer_domain(report, json_pointer_segments(pointer["pointer"]), zones):
            raise ValueError("BAR reading pointer is outside native text analysis domain")
        value = json_pointer(report, pointer["pointer"])
        if isinstance(value, str):
            valid = bool(value.strip())
        elif type(value) in (int, float):
            valid = math.isfinite(value)
        else:
            valid = type(value) is bool
        if not valid:
            raise ValueError("BAR reading pointer must reach a nonempty scalar leaf")
    elif pointer.get("kind") == "saved_text":
        if set(pointer) != {"kind", "role", "file_ref", "fragment"}:
            raise ValueError("BAR saved-text pointer has unexpected fields")
        role, ref, fragment = pointer["role"], pointer["file_ref"], pointer["fragment"]
        if (not isinstance(role, str) or role not in {"own", "peer"} or not isinstance(ref, str) or ref not in files
                or not isinstance(fragment, str) or not fragment.strip()):
            raise ValueError("BAR saved-text pointer lacks a bound passage")
        if role == "own" and (row.get("mode") != "own_and_peer" or ref != row.get("own_text_ref")):
            raise ValueError("BAR saved own passage differs from the scored row")
        if role == "peer" and ref not in row.get("peer_text_refs", []):
            raise ValueError("BAR saved peer passage differs from the scored row")
        path = relative_file(root, ref)
        if sha_file(path) != files[ref] or fragment not in path.read_text(encoding="utf-8-sig"):
            raise ValueError("BAR saved passage is absent or stale")
    else:
        raise ValueError("Unknown BAR reading source pointer kind")


def material_task_scope_checks(root, source, register, rows, inventory, gap, strict, demand=None, demand_ref=None):
    """Bind reviewed material tasks to actual BAR inputs and saved peer passages.

    The source register is a human inventory. This checks its integrity and exact
    coverage, not whether a human discovered every task or judged peers well.
    """
    scopes = source.get("scopes")
    source_files = source.get("source_files")
    if not isinstance(scopes, list) or not scopes or not isinstance(source_files, dict) or not source_files:
        raise ValueError("BAR material_task_scopes needs tasks and saved source files")
    for ref, digest in source_files.items():
        if not isinstance(digest, str) or sha_file(relative_file(root, ref)) != digest:
            raise ValueError("BAR material task source SHA-256 mismatch")
    approved_jobs = {job["landing_job_id"] for job in gap["landing_jobs"] if job["disposition"] == "create"}
    rejected_jobs = {job["landing_job_id"] for job in gap["landing_jobs"] if job["disposition"] == "rejected"}
    pending_jobs = {job["landing_job_id"]: job for job in gap["landing_jobs"] if job["disposition"] == "business_question"}
    by_id = {}
    for scope in scopes:
        if not isinstance(scope, dict) or not isinstance(scope.get("scope_id"), str) or not scope["scope_id"] or scope["scope_id"] in by_id:
            raise ValueError("BAR material task scope IDs must be unique")
        type_id, url, job_id = scope.get("type_id"), scope.get("target_url"), scope.get("landing_job_id")
        if type_id not in register.get("type_ids", []) or (bool(url) == bool(job_id) and (url is not None or job_id is not None)):
            raise ValueError("BAR material task needs a known type and one target")
        if url and url not in inventory:
            raise ValueError("BAR material task URL is absent from raw inventory")
        if job_id and job_id not in approved_jobs | rejected_jobs | set(pending_jobs):
            raise ValueError("BAR material task landing job is absent from GAP")
        if not isinstance(scope.get("task_statement"), str) or not scope["task_statement"].strip():
            raise ValueError("BAR material task needs a concrete task statement")
        extent = scope.get("required_extent")
        if extent not in {"whole_page", "section", "future_page", "future_candidate", "not_applicable"}:
            raise ValueError("BAR material task needs an explicit required extent")
        if extent != "not_applicable" and ((extent == "future_page") != bool(job_id in approved_jobs)
                                                 or (extent == "future_candidate") != bool(job_id in pending_jobs)):
            raise ValueError("BAR future-page/candidate extent must match its GAP decision")
        if not url and not job_id and extent != "not_applicable":
            raise ValueError("Only sourced absent N/A task may omit URL and landing job")
        if extent == "future_candidate":
            job = pending_jobs[job_id]
            cluster_ids = scope.get("candidate_cluster_ids")
            if (not isinstance(demand, dict) or not demand_ref or not isinstance(cluster_ids, list)
                    or not cluster_ids or len(cluster_ids) != len(set(cluster_ids))
                    or not job.get("reason") or not job.get("evidence_refs")
                    or type_id not in job.get("task_type_ids", [])):
                raise ValueError("Pending BAR candidate needs exact sourced GAP job, type and reason")
            raw_clusters = {row.get("id"): row for row in demand.get("clusters", []) if isinstance(row, dict)}
            job_clusters = job.get("cluster_ids")
            if (not isinstance(job_clusters, list) or not job_clusters or len(job_clusters) != len(set(job_clusters))
                    or any(raw_clusters.get(cluster_id, {}).get("status") != "create_candidate" for cluster_id in job_clusters)
                    or {raw_clusters[cluster_id]["type_id"] for cluster_id in job_clusters} != set(job["task_type_ids"])):
                raise ValueError("Pending BAR job contains unknown or mismatched raw demand cluster")
            expected_clusters = {cluster_id for cluster_id in job.get("cluster_ids", [])
                                 if raw_clusters.get(cluster_id, {}).get("type_id") == type_id}
            if (set(cluster_ids) != expected_clusters or not expected_clusters
                    or any(raw_clusters[c].get("status") != "create_candidate" for c in expected_clusters)):
                raise ValueError("Pending BAR candidate differs from raw demand create candidates")
        evidence = scope.get("source_evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("BAR material task needs saved source evidence")
        bound_target = False
        for item in evidence:
            if not isinstance(item, dict) or item.get("file_ref") not in source_files or not item.get("locator") or not item.get("observed_text"):
                raise ValueError("BAR material task source evidence is incomplete")
            if item["observed_text"] not in relative_file(root, item["file_ref"]).read_text(encoding="utf-8-sig"):
                raise ValueError("BAR material task source passage is absent")
            if url and item.get("source_url") == url and url in relative_file(root, item["file_ref"]).read_text(encoding="utf-8-sig"):
                bound_target = True
            if job_id and item.get("source_landing_job_id") == job_id and job_id in relative_file(root, item["file_ref"]).read_text(encoding="utf-8-sig"):
                bound_target = True
        if not bound_target and (url or job_id):
            raise ValueError("BAR material task evidence must bind the source URL or landing job")
        if extent == "future_candidate":
            if demand_ref not in source_files or not all(any(
                    item.get("file_ref") == demand_ref and item.get("source_cluster_id") == cluster_id
                    and cluster_id in relative_file(root, demand_ref).read_text(encoding="utf-8-sig")
                    for item in evidence) for cluster_id in cluster_ids):
                raise ValueError("Pending BAR candidate needs source-bound raw demand clusters")
        full_ref = scope.get("full_text_ref")
        if extent == "whole_page" and full_ref not in source_files:
            raise ValueError("Whole-page BAR scope needs saved full-page text")
        if full_ref is not None and full_ref not in source_files:
            raise ValueError("BAR full-page text lacks source SHA-256")
        queries = scope.get("queries")
        if extent not in {"not_applicable"} and (not isinstance(queries, list) or not queries or any(not isinstance(q, str) or not q.strip() for q in queries)):
            raise ValueError("Scored BAR material task needs explicit queries")
        by_id[scope["scope_id"]] = scope
    if not strict:
        return
    if {s["type_id"] for s in scopes} != set(register["type_ids"]):
        raise ValueError("BAR material tasks must cover every semantic type")
    required_weak = {(t, url) for t, urls in gap["weak_urls_by_type"].items() for url in urls}
    if not required_weak <= {(s["type_id"], s.get("target_url")) for s in scopes}:
        raise ValueError("BAR material tasks must include every weak URL")
    coverage = register.get("scope_coverage")
    if not isinstance(coverage, list) or len(coverage) != len(by_id) or {r.get("scope_id") for r in coverage if isinstance(r, dict)} != set(by_id):
        raise ValueError("BAR material task coverage must reconcile every source scope once")
    seen_rows = set()
    for entry in coverage:
        scope = by_id[entry["scope_id"]]
        index = entry.get("row_index")
        if type(index) is not int or index < 0 or index >= len(rows):
            raise ValueError("BAR scope coverage needs a real register row")
        row = rows[index]
        seen_rows.add(index)
        if row["type_id"] != scope["type_id"] or row.get("url") != scope.get("target_url"):
            raise ValueError("BAR scope target/type differs from register row")
        if scope.get("landing_job_id") and scope["landing_job_id"] not in (set(row.get("target_landing_job_ids", []))
                | set(row.get("pending_landing_job_ids", [])) | set(row.get("noncreate_job_ids", []))):
            raise ValueError("BAR scope landing job differs from register row")
        if entry.get("status") == "not_applicable":
            if (row.get("mode") != "not_applicable" or scope["required_extent"] != "not_applicable"
                    or entry.get("reason") != row["applicability"]["reason"]):
                raise ValueError("BAR task N/A needs the same sourced row reason")
            if entry.get("reading") is not None:
                raise ValueError("BAR N/A scope cannot claim a scored reading")
            continue
        if entry.get("status") != "scored" or row.get("mode") == "not_applicable":
            raise ValueError("Complete BAR task must have a scored or sourced N/A disposition")
        bar_reading_checks(root, register, entry, row)
        input_data = checked_object(root, row["input_ref"], "BAR task input")
        if not set(scope["queries"]) <= set(input_data.get("queries", [])):
            raise ValueError("BAR report queries do not cover the material task")
        if scope["required_extent"] == "whole_page":
            if (entry.get("analyzed_extent") != "whole_page" or row.get("own_text_ref") != scope["full_text_ref"]
                    or input_data.get("own_page", {}).get("text") != relative_file(root, scope["full_text_ref"]).read_text(encoding="utf-8-sig")):
                raise ValueError("A section-only BAR report cannot close whole-page scope")
        elif scope["required_extent"] == "section" and entry.get("analyzed_extent") not in {"whole_page", "section"}:
            raise ValueError("BAR section scope needs declared analyzed extent")
        elif scope["required_extent"] == "future_page" and (entry.get("analyzed_extent") != "future_page" or row.get("mode") != "peer_creation"):
            raise ValueError("BAR future-page task needs peer-only comparison")
        elif scope["required_extent"] == "future_candidate" and (entry.get("analyzed_extent") != "future_candidate"
                or row.get("mode") != "peer_creation" or row.get("page_disposition") != "candidate_pending"):
            raise ValueError("Pending BAR task needs peer-only candidate comparison without publication")
        peers = input_data.get("peers", [])
        proof = entry.get("peer_suitability")
        if not isinstance(proof, list) or len(proof) != len(peers) or len(proof) != len(row["peer_text_refs"]):
            raise ValueError("Every compared peer needs task-specific suitability evidence")
        for peer, ref, item in zip(peers, row["peer_text_refs"], proof):
            if (not isinstance(item, dict) or item.get("peer_text_ref") != ref or item.get("peer_url") != peer.get("url")
                    or not item.get("peer_url") or not item.get("basis") or not item.get("observed_text")):
                raise ValueError("BAR peer suitability does not bind the compared peer")
            if item["observed_text"] not in relative_file(root, ref).read_text(encoding="utf-8-sig"):
                raise ValueError("BAR peer task passage is absent from the compared text")
    if seen_rows != set(range(len(rows))):
        raise ValueError("BAR register row has no material task scope")


def partial_coverage_types(root, state, refs, node_id):
    """Reconcile the known source denominator even when unavailable items remain open."""
    if node_id == "semantic_types":
        demand = checked_object(root, refs["inputs"]["demand_universe"], "demand_universe")
        urls = checked_object(root, refs["inputs"]["url_universe"], "url_universe")
        data = checked_object(root, refs["outputs"]["semantic_map"], "semantic_map")
        pending = data["pending_items"]
        known_urls, _, unknown_urls = source_pages_partial(urls, pending)
        known_clusters, _ = source_clusters_partial(demand, pending)
        if demand.get("raw_query_source") is not None:
            source_query_rows(root, demand)
        elif not any(item["scope_kind"] == "source" and item["scope_id"] == "raw_query_source"
                     and item["source_ref"] is None for item in pending):
            raise ValueError("Unavailable raw query source needs addressed pending item")
        current_types = {typ for typ, _ in known_urls.values()}
        demand_types = {row["type_id"] for row in known_clusters}
        if names(urls.get("type_ids"), "partial current types", allow_empty=True) != current_types or names(demand.get("type_ids"), "partial demand types", allow_empty=True) != demand_types:
            raise ValueError("Partial semantic declared type IDs differ from known raw rows")
        evidence_map(urls.get("evidence_by_type"), current_types, "partial URL evidence")
        evidence_map(demand.get("evidence_by_type"), demand_types, "partial demand evidence")
        expected = current_types | demand_types
        if names(data.get("type_ids"), "partial semantic types", allow_empty=True) != expected:
            raise ValueError("Partial semantic map loses a known raw type")
        if data.get("presence_by_type") != {t: "existing" if t in current_types else "missing" for t in expected}:
            raise ValueError("Partial semantic presence differs from known URL rows")
        decisions = data.get("decisions")
        if not isinstance(decisions, dict) or set(decisions) != expected or any(value not in {"create", "improve", "keep", "reject", "covered", "integrate", "business_question"} for value in decisions.values()):
            raise ValueError("Partial semantic type needs explicit known-scope decision")
        evidence_map(data.get("evidence_by_type"), expected, "partial semantic evidence")
        source_intents = demand.get("intent_ids_by_type")
        intents = data.get("intent_ids_by_type")
        if not isinstance(source_intents, dict) or set(source_intents) != demand_types or not isinstance(intents, dict) or set(intents) != expected:
            raise ValueError("Partial semantic intent map differs from known demand types")
        for type_id in expected:
            required_refs = set(demand.get("evidence_by_type", {}).get(type_id, [])) | set(urls.get("evidence_by_type", {}).get(type_id, []))
            if not required_refs <= set(data["evidence_by_type"][type_id]):
                raise ValueError("Partial semantic type loses available source evidence")
            if names(intents[type_id], "partial semantic intent IDs", allow_empty=True) != names(source_intents.get(type_id, []), "partial source intent IDs", allow_empty=True):
                raise ValueError("Partial semantic known intent IDs differ from raw demand")
            if decisions[type_id] == "create" and not intents[type_id]:
                raise ValueError("Partial semantic create requires known sourced intent")
        if unknown_urls and not any(item["counted_in"] == "urls" for item in pending):
            raise ValueError("Unknown URL denominator disappeared")
        return
    semantic = previous_output(root, state, "semantic_types", "semantic_map")
    expected = names(semantic.get("type_ids"), "semantic type_ids")
    key = {"gap": "gap_register", "block_gap": "block_register", "text_bar": "text_register"}[node_id]
    data = checked_object(root, refs["outputs"][key], key)
    if names(data.get("type_ids"), key + ".type_ids") != expected:
        raise ValueError("Partial register must retain every known semantic type")
    if node_id == "gap":
        report = checked_object(root, refs["outputs"]["site_gap_report"], "site_gap_report")
        site_input = checked_object(root, refs["inputs"]["site_gap_input"], "site_gap_input")
        link = checked_object(root, refs["inputs"]["link_snapshot"], "link_snapshot")
        rank_handoff_checks(root, state, site_input, False)
        if data.get("site_gap_report_ref") != refs["outputs"]["site_gap_report"] or canonical(site_input.get("links_assessment")) != canonical(link.get("links_assessment")) or canonical(site_input.get("links")) != canonical(link.get("links")):
            raise ValueError("Partial GAP loses report or saved link source")
        raw_link_metadata_checks(root, link)
        raw_link_primary_checks(root, link)
        saved_link_rows = []
        for path, digest in link.get("links_assessment", {}).get("evidence_files", {}).items():
            if sha_file(relative_file(root, path)) != digest:
                raise ValueError("Partial GAP link raw source SHA differs")
            if link["links_assessment"].get("status") in {"available", "partial"}:
                saved = checked_object(root, path, "partial saved links")
                if not isinstance(saved.get("links"), list):
                    raise ValueError("Partial link raw source lacks normalized rows")
                saved_link_rows.extend(saved["links"])
        if link.get("links_assessment", {}).get("status") in {"available", "partial"} and canonical(saved_link_rows) != canonical(link.get("links")):
            raise ValueError("Partial link source rows differ from saved snapshot")
        spec = importlib.util.spec_from_file_location("offerist_site_gap_replay", SITE_GAP)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if canonical(report) != canonical(module.summarize(site_input)):
            raise ValueError("Partial GAP report differs from source replay")
        validate_planned_route_sources(root, site_input)
        url_source = checked_object(root, refs["inputs"]["url_universe"], "url_universe")
        demand_source = checked_object(root, refs["inputs"]["demand_universe"], "demand_universe")
        urls, url_total, unknown_urls = source_pages_partial(url_source, data["pending_items"])
        clusters, demand_total = source_clusters_partial(demand_source, data["pending_items"])
        if data.get("url_total") != url_total or data.get("demand_total") != demand_total:
            raise ValueError("Partial GAP totals differ from raw known source rows")
        input_pages = site_input.get("inventory", {}).get("pages")
        if not isinstance(input_pages, list) or len(input_pages) != url_total or {(p.get("canonical_url"), p.get("page_type")) for p in input_pages if p.get("canonical_url") in urls} != {(url, typ) for url, (typ, _) in urls.items()}:
            raise ValueError("Partial GAP inventory differs from known raw URL rows")
        input_clusters = site_input.get("demand_review", {}).get("clusters")
        if not isinstance(input_clusters, list) or len(input_clusters) != demand_total or {(row.get("id"), row.get("page_type"), row.get("status")) for row in input_clusters if row.get("id") in {c["id"] for c in clusters}} != {(row["id"], row["type_id"], row["status"]) for row in clusters}:
            raise ValueError("Partial GAP demand review differs from known raw clusters")
        allocations = data.get("page_allocations")
        if not isinstance(allocations, list) or {r.get("url") for r in allocations} != set(urls) or len(allocations) != len(urls):
            raise ValueError("Partial GAP must classify every known URL")
        if data.get("url_classified") != len(allocations) or data.get("demand_allocated") != len(clusters):
            raise ValueError("Partial GAP URL classified count is inconsistent")
        action_by_url = {page["canonical_url"]: page.get("action") for page in input_pages}
        if any(row.get("type_id") != urls[row["url"]][0] or row.get("action") != action_by_url[row["url"]]
               or not names(row.get("evidence_refs"), "partial GAP page allocation evidence") for row in allocations):
            raise ValueError("Partial GAP page allocation differs from known source action")
        weak = data.get("weak_urls_by_type")
        if not isinstance(weak, dict) or set(weak) != expected or set(url_source.get("weak_url_ids", [])) != {url for values in weak.values() for url in values}:
            raise ValueError("Partial GAP weak URL list differs from raw source")
        if any(urls.get(url, (None,))[0] != typ for typ, values in weak.items() for url in values):
            raise ValueError("Partial GAP weak URL is absent or has wrong type")
        if names(data.get("missing_intent_ids"), "partial GAP missing intents", allow_empty=True) != set(report.get("missing_intents", {}).get("supported_ids", [])):
            raise ValueError("Partial GAP missing intents differ from replay")
        jobs = data.get("landing_jobs")
        if not isinstance(jobs, list):
            raise ValueError("Partial GAP needs explicit landing jobs")
        saved_jobs = site_input.get("landing_jobs") or []
        if canonical(saved_jobs) != canonical([job for job in jobs if job.get("disposition") != "business_question"]):
            raise ValueError("Partial GAP approved jobs differ from saved replay input")
        job_by_id = {job.get("landing_job_id"): job for job in jobs if isinstance(job, dict)}
        if len(job_by_id) != len(jobs):
            raise ValueError("Partial GAP job IDs are not unique")
        pending_ids = {item["scope_id"] for item in data["pending_items"] if item["scope_kind"] == "job"}
        for job in jobs:
            if job.get("disposition") == "business_question":
                if job["landing_job_id"] not in pending_ids or job.get("action_id") or job.get("published_type_id") is not None:
                    raise ValueError("Pending job cannot claim approved creation")
            elif job.get("disposition") not in {"create", "no_new_page", "rejected"}:
                raise ValueError("Unknown GAP job disposition")
        actions = data.get("actions")
        if not isinstance(actions, list):
            raise ValueError("Partial GAP needs known action register")
        names([row.get("action_id") for row in actions], "GAP action IDs", allow_empty=True)
        dedup_keys, effects, page_urls, page_jobs = set(), set(), set(), set()
        for row in actions:
            if row.get("decision") == "create" and (not row.get("landing_job_ids") or any(job_by_id.get(j, {}).get("disposition") != "create" for j in row["landing_job_ids"])):
                raise ValueError("Create action targets pending or absent job")
            pending_targets = set(row.get("landing_job_ids") or []) & pending_ids
            if pending_targets and (row.get("decision") != "verify" or row.get("unit") == "page"
                                    or not row.get("access_condition") or not row.get("deliverable")):
                raise ValueError("Pending job permits only bounded conditional verification, not page work")
            if type(row.get("quantity")) is not int or row["quantity"] <= 0 or not row.get("unit") or not row.get("acceptance_criteria"):
                raise ValueError("Known GAP action lacks unit, quantity or acceptance")
            affected = names(row.get("affected_urls", []), "partial GAP work URLs", allow_empty=True)
            jobs_for_action = names(row.get("landing_job_ids", []), "partial GAP work jobs", allow_empty=True)
            if not row.get("dedup_key") or not row.get("effect_key") or row["dedup_key"] in dedup_keys:
                raise ValueError("Partial GAP action needs unique dedup key and effect")
            if any((url, row["effect_key"]) in effects for url in affected):
                raise ValueError("Partial GAP repeats same effect on one URL")
            effects.update((url, row["effect_key"]) for url in affected)
            dedup_keys.add(row["dedup_key"])
            if row["unit"] == "page":
                if (affected and jobs_for_action) or row["quantity"] != len(affected or jobs_for_action) or page_urls & affected or page_jobs & jobs_for_action:
                    raise ValueError("Partial GAP page unit count or target is duplicated")
                page_urls.update(affected)
                page_jobs.update(jobs_for_action)
        if pending_ids and report.get("page_plan", {}).get("approved_new_pages") is not None:
            raise ValueError("Pending landing decision keeps approved page count null")
        return
    gap = previous_output(root, state, "gap", "gap_register")
    if node_id == "block_gap":
        urls_ref = state["attempts"]["semantic_types"][-1]["inputs"]["url_universe"]["path"]
        inventory, _, _ = source_pages_partial(checked_object(root, urls_ref, "url_universe"), data["pending_items"])
        evidence = data.get("evidence_index")
        variants = data.get("variants_by_type")
        assignments = data.get("url_variant_assignments")
        rows = data.get("rows")
        if not isinstance(evidence, dict) or not isinstance(variants, dict) or set(variants) != expected or not isinstance(assignments, list) or not isinstance(rows, list) or not rows:
            raise ValueError("Partial block register needs canonical rows, evidence, variants and URL assignments")
        for evidence_id, item in evidence.items():
            if (not evidence_id or not isinstance(item, dict) or item.get("role") not in {"own", "peer"}
                    or item.get("media_type") not in {"text/html", "image/png", "image/jpeg", "image/webp", "application/json"}
                    or item.get("sha256") != sha_file(relative_file(root, item.get("path")))
                    or not relative_file(root, item["path"]).read_bytes().strip()):
                raise ValueError("Partial block evidence is malformed or stale")
        pairs = set()
        for type_id, items in variants.items():
            if not isinstance(items, list) or not items:
                raise ValueError("Partial block variant type cannot disappear")
            for variant in items:
                pair = (type_id, variant.get("variant_id"))
                if not pair[1] or pair in pairs:
                    raise ValueError("Partial block variant pair duplicated or missing")
                if variant.get("required") is not True or not isinstance(variant.get("evidence_refs"), list) or any(ref not in evidence for ref in variant["evidence_refs"]):
                    raise ValueError("Partial block variant lacks required flag or indexed evidence")
                pairs.add(pair)
        if len(assignments) != len(inventory) or {r.get("url") for r in assignments} != set(inventory):
            raise ValueError("Partial block URL assignment denominator differs from inventory")
        for assignment in assignments:
            if inventory[assignment["url"]] != (assignment.get("type_id"), assignment.get("variant_id")):
                raise ValueError("Partial block assignment differs from raw type/variant")
            refs_for_url = assignment.get("evidence_refs")
            if not isinstance(refs_for_url, list) or not refs_for_url or any(ref not in evidence or evidence[ref]["role"] != "own" or evidence[ref].get("source_url") != assignment["url"] for ref in refs_for_url):
                raise ValueError("Partial block known URL assignment lacks exact own evidence")
        seen = set()
        pending = {item["scope_id"] for item in data["pending_items"] if item["scope_kind"] == "block"}
        for row in rows:
            if not isinstance(row, dict) or not row.get("block_id") or (row.get("type_id"), row.get("variant_id")) not in pairs:
                raise ValueError("Partial block row lacks valid type/variant/block")
            ident = f"{row['type_id']}:{row['variant_id']}:{row['block_id']}"
            if ident in seen:
                raise ValueError("Duplicate partial block row")
            seen.add(ident)
            status = row.get("status")
            if status == "unknown":
                if ident not in pending or any(row.get(k) is not None for k in ("change", "unit", "acceptance")):
                    raise ValueError("Unknown block needs addressed pending item and null work")
            elif status in {"weak", "missing"}:
                if not all(row.get(k) for k in ("change", "unit", "acceptance")) or not row.get("peer_evidence_refs"):
                    raise ValueError("Known weak/missing block lacks source or work unit")
                if not isinstance(row.get("assessment_reason"), str) or not row["assessment_reason"].strip():
                    raise ValueError("Known weak/missing block needs assessment_reason")
            elif status in {"sufficient", "not_applicable"}:
                rejected = {job["landing_job_id"]: job for job in gap.get("landing_jobs", [])
                            if job.get("disposition") == "rejected"}
                noncreate_ids = row.get("noncreate_job_ids") or []
                absent_rejected = bool(noncreate_ids) and set(noncreate_ids) <= set(rejected)
                if (not row.get("assessment_reason") or not row.get("peer_evidence_refs")
                        or (not absent_rejected and not row.get("own_evidence_refs"))):
                    raise ValueError("Sufficient/N/A block needs saved own/peer proof or exact rejected task")
                if absent_rejected and (status != "not_applicable" or row.get("own_evidence_refs")
                                        or len(noncreate_ids) != 1 or row["assessment_reason"] != rejected[noncreate_ids[0]].get("reason")
                                        or set(row.get("source_evidence_refs", [])) != set(rejected[noncreate_ids[0]].get("evidence_refs", []))):
                    raise ValueError("Rejected N/A block needs exact reason and source, without own proof")
            else:
                raise ValueError("Unknown block status")
            for ref in (row.get("own_evidence_refs") or []) + (row.get("peer_evidence_refs") or []):
                if ref not in evidence:
                    raise ValueError("Block row evidence ID absent from index")
            affected = names(row.get("affected_urls", []), "partial block affected URLs", allow_empty=True)
            approved = {job["landing_job_id"] for job in gap.get("landing_jobs", []) if job.get("disposition") == "create"}
            jobs = names(row.get("landing_job_ids", []), "partial block landing jobs", allow_empty=True)
            if any(inventory.get(url) != (row["type_id"], row["variant_id"]) for url in affected) or not jobs <= approved:
                raise ValueError("Partial block work targets wrong URL/variant or pending job")
            if affected and any(evidence[ref].get("source_url") not in affected for ref in row.get("own_evidence_refs", [])):
                raise ValueError("Partial block own proof differs from affected URL")
        if {row.get("type_id") for row in rows} != expected or not pairs <= {(row["type_id"], row["variant_id"]) for row in rows}:
            raise ValueError("Partial block rows omit known type/variant denominator")
        return
    # Every saved BAR score is replayed; unavailable tasks remain addressed pending items.
    rows = data.get("rows")
    files = data.get("files")
    report = checked_object(root, refs["outputs"]["bar_report"], "bar_report")
    if not isinstance(rows, list) or not isinstance(files, dict) or not isinstance(report.get("reports"), list):
        raise ValueError("Partial BAR needs canonical rows, files and manifest")
    for path, digest in files.items():
        if sha_file(relative_file(root, path)) != digest:
            raise ValueError("Partial BAR saved file SHA mismatch")
    manifest = read_json(BAR_MANIFEST)
    method = checked_object(root, refs["inputs"]["far_method"], "far_method")
    if method.get("source") != "BAR" or method.get("version") != manifest["bar_commit"] or method.get("sha256") != manifest["bundle_sha256"]:
        raise ValueError("Partial BAR method provenance differs")
    manifest_pairs = set()
    for item in report["reports"]:
        if (not isinstance(item, dict) or item.get("input_ref") not in files or item.get("report_ref") not in files
                or item.get("input_sha256") != files[item["input_ref"]]
                or item.get("report_sha256") != files[item["report_ref"]]):
            raise ValueError("Partial BAR report manifest does not match saved input/report SHA")
        pair = (item["input_ref"], item["report_ref"])
        if pair in manifest_pairs:
            raise ValueError("Partial BAR report manifest duplicates a scored pair")
        manifest_pairs.add(pair)
    scored_pairs = set()
    gap_input_ref = state["attempts"]["gap"][-1]["inputs"]["site_gap_input"]["path"]
    gap_input = checked_object(root, gap_input_ref, "site_gap_input")
    purpose_by_url = {page["canonical_url"]: page.get("purpose") for page in gap_input.get("inventory", {}).get("pages", [])}
    scopes = checked_object(root, refs["inputs"]["material_task_scopes"], "material_task_scopes")
    if not isinstance(scopes.get("scopes"), list):
        raise ValueError("Partial BAR needs material task scopes")
    jobs_by_id = {job["landing_job_id"]: job for job in gap["landing_jobs"]}
    for row in rows:
        if not isinstance(row, dict) or row.get("type_id") not in expected:
            raise ValueError("Partial BAR row lacks semantic type")
        if row.get("mode") == "not_applicable":
            proof = row.get("applicability")
            if not isinstance(proof, dict) or not all(proof.get(k) for k in ("functional_role", "purpose", "intent", "reason_code", "reason", "basis", "evidence_files")) or not set(proof["evidence_files"]) <= set(files):
                raise ValueError("Partial BAR N/A lacks saved task-specific proof")
            if any(row.get(k) is not None for k in ("own_score", "input_ref", "adapter_report_ref", "own_text_ref")):
                raise ValueError("Partial BAR N/A invents score or input")
            url = row.get("url")
            if url is not None and (purpose_by_url.get(url) != proof["purpose"] or proof["purpose"] in {"commercial", "informational", "info", "mixed", "unknown"}
                                        or proof["functional_role"] not in {"navigation", "legal", "service_utility"} or proof["reason_code"] != "non_search_utility"):
                raise ValueError("Partial BAR N/A cannot exempt commercial/info or unknown URL")
            if url is None and (semantic["presence_by_type"][row["type_id"]] != "missing" or semantic["decisions"][row["type_id"]] != "reject"
                                or proof["functional_role"] != "rejected_unrelated_type" or proof["reason_code"] != "rejected_unrelated_demand"):
                raise ValueError("Partial BAR N/A absent type needs sourced rejected task")
        elif row.get("mode") in {"own_and_peer", "peer_creation"}:
            inp, out = row.get("input_ref"), row.get("adapter_report_ref")
            if inp not in files or out not in files or (inp, out) not in manifest_pairs:
                raise ValueError("Scored partial BAR row lacks saved input/report")
            input_data = checked_object(root, inp, "partial BAR input")
            result = checked_object(root, out, "BAR result")
            if input_data.get("page_type") != row["type_id"] or result.get("page_type") != row["type_id"] or result.get("status") != "ok" or result.get("intent") != input_data.get("intent"):
                raise ValueError("Partial BAR scored input/report type or intent differs from row")
            replay_bar(root, inp, result)
            peer_refs = row.get("peer_text_refs")
            if (not isinstance(peer_refs, list) or not peer_refs
                    or any(not isinstance(ref, str) or ref not in files for ref in peer_refs)
                    or [relative_file(root, ref).read_text(encoding="utf-8-sig") for ref in peer_refs]
                    != [peer.get("text") for peer in input_data.get("peers", [])]):
                raise ValueError("Partial BAR scored peer texts differ from saved inputs")
            if row["mode"] == "own_and_peer":
                score = row.get("own_score")
                own_ref = row.get("own_text_ref")
                own_page = input_data.get("own_page")
                if (not isinstance(score, dict)
                        or set(score) != {"key_tfidf_score", "weighted_coverage", "weighted_density"}
                        or score != result.get("own_score")
                        or result.get("mode") != "own_vs_peers" or result.get("reference_role") != "own"
                        or not result.get("report") or own_ref not in files
                        or not isinstance(own_page, dict)
                        or own_page.get("text") != relative_file(root, own_ref).read_text(encoding="utf-8-sig")
                        or row.get("url") is None or bar_input_url(input_data) != row["url"]):
                    raise ValueError("Partial BAR existing row differs from native own score, text, mode or URL")
            elif (row.get("own_score") is not None or row.get("own_text_ref") is not None
                    or row.get("url") is not None or input_data.get("own_page") is not None
                    or bar_input_url(input_data) is not None or result.get("own_score") is not None
                    or result.get("mode") != "peer_only" or result.get("reference_role") != "peer"
                    or not result.get("peer_benchmark")):
                raise ValueError("Partial BAR peer-only row invents own score, text or URL")
            scored_pairs.add((inp, out))
        elif row.get("mode") == "pending":
            if not any(item["scope_kind"] == "task" and item["scope_id"] == row.get("scope_id") for item in data["pending_items"]):
                raise ValueError("Pending BAR row needs addressed task")
            matched_scopes = [scope for scope in scopes["scopes"] if isinstance(scope, dict) and scope.get("scope_id") == row.get("scope_id")]
            if (len(matched_scopes) != 1 or matched_scopes[0].get("type_id") != row["type_id"]
                    or matched_scopes[0].get("target_url") != row.get("url")):
                raise ValueError("Pending BAR row differs from its material task scope")
            scope = matched_scopes[0]
            extent = scope.get("required_extent")
            if extent in {"whole_page", "section"}:
                if row.get("url") is None or row.get("own_text_ref") not in files:
                    raise ValueError("Pending existing-URL BAR task needs saved own text")
            elif extent in {"future_page", "future_candidate"}:
                job_id = scope.get("landing_job_id")
                job = jobs_by_id.get(job_id)
                if (row.get("url") is not None or row.get("own_text_ref") is not None or not job
                        or row["type_id"] not in job.get("task_type_ids", [])):
                    raise ValueError("Pending future BAR task needs absent own page and exact GAP job")
                if extent == "future_page":
                    disposition = "new_page" if job.get("published_type_id") == row["type_id"] else "integrated_in_job"
                    if (job.get("disposition") != "create" or row.get("page_disposition") != disposition
                            or names(row.get("target_landing_job_ids"), "pending BAR approved jobs") != {job_id}
                            or row.get("pending_landing_job_ids") not in (None, [])):
                        raise ValueError("Pending future BAR task differs from approved GAP job")
                elif (job.get("disposition") != "business_question" or row.get("page_disposition") != "candidate_pending"
                        or names(row.get("pending_landing_job_ids"), "pending BAR candidate jobs") != {job_id}
                        or row.get("target_landing_job_ids") not in (None, [])
                        or row.get("action_id") is not None or row.get("published_type_id") is not None
                        or semantic["decisions"].get(row["type_id"]) != "business_question"
                        or state["attempts"]["gap"][-1]["status"] != "partial"
                        or previous_output(root, state, "gap", "site_gap_report").get("page_plan", {}).get("approved_new_pages") is not None):
                    raise ValueError("Pending BAR candidate cannot claim approved creation")
            else:
                raise ValueError("Pending BAR row needs an existing or future material task")
            if (row.get("own_score") is not None or row.get("adapter_report_ref") is not None
                    or row.get("input_ref") is not None
                    or not names(row.get("peer_text_refs"), "pending BAR peer files", allow_empty=True) <= set(files)
                    or not names(row.get("requirements"), "pending BAR source-backed requirements")
                    or not row.get("change") or not row.get("acceptance")):
                raise ValueError("Pending BAR work needs saved own/peer text, null score and bounded change")
            peer_input, peer_report = row.get("peer_input_ref"), row.get("peer_report_ref")
            if bool(peer_input) != bool(peer_report):
                raise ValueError("Pending BAR peer benchmark needs both saved input and report")
            if peer_input:
                if peer_input not in files or peer_report not in files:
                    raise ValueError("Pending BAR peer benchmark lacks saved files")
                peer_data = checked_object(root, peer_input, "pending BAR peer input")
                peer_result = checked_object(root, peer_report, "pending BAR peer report")
                if (peer_data.get("own_page") is not None or peer_result.get("mode") != "peer_only"
                        or peer_result.get("own_score") is not None or not peer_result.get("peer_benchmark")
                        or [p.get("text") for p in peer_data.get("peers", [])] != [relative_file(root, ref).read_text(encoding="utf-8-sig") for ref in row["peer_text_refs"]]):
                    raise ValueError("Pending BAR peer-only benchmark invents own score or mismatches peers")
                replay_bar(root, peer_input, peer_result)
        else:
            raise ValueError("Unsupported partial BAR row mode")
    if manifest_pairs != scored_pairs:
        raise ValueError("Partial BAR manifest differs from actually scored rows")
    if {row["type_id"] for row in rows} != expected:
        raise ValueError("Partial BAR rows omit a known semantic type")
    required_weak = {(typ, url) for typ, urls in gap.get("weak_urls_by_type", {}).items() for url in urls}
    if not required_weak <= {(row["type_id"], row.get("url")) for row in rows}:
        raise ValueError("Partial BAR rows omit a known weak URL")
    if not isinstance(data.get("scope_coverage"), list):
        raise ValueError("Partial BAR needs material task scope coverage")
    ids = {s.get("scope_id") for s in scopes.get("scopes", [])}
    if {r.get("scope_id") for r in data["scope_coverage"]} != ids or len(data["scope_coverage"]) != len(ids):
        raise ValueError("Partial BAR scope coverage omits a known material task")
    pending_tasks = {item["scope_id"] for item in data["pending_items"] if item["scope_kind"] == "task"}
    scope_by_id = {scope["scope_id"]: scope for scope in scopes["scopes"]}
    for coverage in data["scope_coverage"]:
        scope = scope_by_id[coverage["scope_id"]]
        if coverage.get("status") == "pending":
            if coverage["scope_id"] not in pending_tasks or coverage.get("row_index") is not None:
                raise ValueError("Pending BAR scope needs addressed task and no fabricated score")
            if coverage.get("reading") is not None:
                raise ValueError("Pending BAR scope cannot claim a scored reading")
            if not any(row.get("scope_id") == coverage["scope_id"] and row.get("mode") == "pending"
                       and row.get("type_id") == scope.get("type_id") and row.get("url") == scope.get("target_url")
                       for row in rows):
                raise ValueError("Pending BAR task lacks exact pending row")
        elif coverage.get("status") in {"scored", "not_applicable"}:
            index = coverage.get("row_index")
            if type(index) is not int or index < 0 or index >= len(rows):
                raise ValueError("Known partial BAR scope needs real row")
            row = rows[index]
            if row["type_id"] != scope["type_id"] or row.get("url") != scope.get("target_url"):
                raise ValueError("Partial BAR scope row differs from source task")
            if coverage["status"] == "not_applicable" and row.get("mode") != "not_applicable":
                raise ValueError("Partial BAR scope N/A lacks sourced N/A row")
            if coverage["status"] == "not_applicable" and coverage.get("reading") is not None:
                raise ValueError("Partial BAR N/A scope cannot claim a scored reading")
            if coverage["status"] == "scored" and row.get("mode") not in {"own_and_peer", "peer_creation"}:
                raise ValueError("Partial BAR scored scope lacks native scored row")
            if coverage["status"] == "scored":
                bar_reading_checks(root, data, coverage, row)
        else:
            raise ValueError("Unknown partial BAR scope disposition")
    urls_ref = state["attempts"]["semantic_types"][-1]["inputs"]["url_universe"]["path"]
    inventory, _, _ = source_pages_partial(checked_object(root, urls_ref, "url_universe"), data["pending_items"])
    demand_ref = state["attempts"]["semantic_types"][-1]["inputs"]["demand_universe"]["path"]
    material_task_scope_checks(root, scopes, data, rows, inventory, gap, False,
                               checked_object(root, demand_ref, "demand_universe"), demand_ref)


def coverage_types(root, state, refs, node_id, strict):
    """Validate source union, stage registers, and links to real run artifacts."""
    if node_id not in {"semantic_types", "gap", "block_gap", "text_bar"}:
        return
    if not strict:
        partial_coverage_types(root, state, refs, node_id)
        return
    if node_id == "semantic_types":
        demand = checked_object(root, refs["inputs"]["demand_universe"], "demand_universe")
        source_query_rows(root, demand)
        urls = checked_object(root, refs["inputs"]["url_universe"], "url_universe")
        required = names(demand.get("type_ids"), "demand type_ids", allow_empty=True)
        current_types = names(urls.get("type_ids"), "current type_ids", allow_empty=True)
        actual_required = {row["type_id"] for row in source_clusters(demand)}
        actual_pages = source_pages(urls)
        actual_current = {type_id for type_id, _ in actual_pages.values()}
        if required != actual_required or current_types != actual_current:
            raise ValueError("Declared type_ids differ from raw URL pages or demand clusters")
        if not current_types and (urls.get("empty_verified") is not True or not urls.get("empty_evidence")):
            raise ValueError("Empty URL universe needs empty_verified and empty_evidence")
        expected = required | current_types
        if not expected:
            raise ValueError("semantic source union is empty")
        evidence_map(demand.get("evidence_by_type"), required, "demand evidence")
        evidence_map(urls.get("evidence_by_type"), current_types, "URL evidence")
        output = checked_object(root, refs["outputs"]["semantic_map"], "semantic_map")
        if names(output.get("type_ids"), "semantic type_ids") != expected:
            raise ValueError("semantic_map must cover source union current+required, including missing site types")
        evidence_map(output.get("evidence_by_type"), expected, "semantic evidence")
        for type_id in expected:
            source_refs = set(demand.get("evidence_by_type", {}).get(type_id, [])) | set(urls.get("evidence_by_type", {}).get(type_id, []))
            if not source_refs <= set(output["evidence_by_type"][type_id]):
                raise ValueError(f"semantic type {type_id} loses source evidence")
        presence = output.get("presence_by_type")
        if not isinstance(presence, dict) or set(presence) != expected or any(presence[t] != ("existing" if t in current_types else "missing") for t in expected):
            raise ValueError("semantic presence must match current URL types")
        decisions = output.get("decisions", {})
        if set(decisions) != expected or any(x not in {"create", "improve", "keep", "reject", "covered", "integrate", "business_question"} for x in decisions.values()):
            raise ValueError("Every semantic type needs an explicit decision")
        if any(decisions[t] in {"keep", "improve"} and t not in current_types for t in expected):
            raise ValueError("A missing semantic type cannot claim existing URL work or keep")
        integration_basis = output.get("integration_basis_by_type", {})
        if not isinstance(integration_basis, dict) or set(integration_basis) != {t for t in expected if decisions[t] == "integrate"}:
            raise ValueError("Integrated semantic types need explicit sourced basis")
        for item in integration_basis.values():
            if not isinstance(item, dict) or not item.get("reason") or not names(item.get("evidence_refs"), "semantic integration evidence"):
                raise ValueError("Integrated semantic type lacks reason or evidence")
        source_intents = demand.get("intent_ids_by_type")
        if not isinstance(source_intents, dict) or set(source_intents) != required:
            raise ValueError("Demand needs intent IDs for every required type")
        for values in source_intents.values():
            names(values, "demand intent IDs", allow_empty=True)
        intents = output.get("intent_ids_by_type")
        if not isinstance(intents, dict) or set(intents) != expected:
            raise ValueError("Semantic map needs intent IDs for every type")
        for type_id in expected:
            if names(intents[type_id], "semantic intent IDs", allow_empty=True) != set(source_intents.get(type_id, [])):
                raise ValueError("Semantic intent IDs differ from demand source")
            if decisions[type_id] == "create" and not intents[type_id]:
                raise ValueError("Create decision needs sourced missing intent IDs")
        return
    semantic = previous_output(root, state, "semantic_types", "semantic_map")
    expected = names(semantic.get("type_ids"), "semantic type_ids")
    output_key = {"gap": "gap_register", "block_gap": "block_register", "text_bar": "text_register"}[node_id]
    data = checked_object(root, refs["outputs"][output_key], output_key)
    if names(data.get("type_ids"), output_key + ".type_ids") != expected:
        raise ValueError(f"{node_id} must cover every semantic type")
    if node_id == "gap":
        if data.get("site_gap_report_ref") != refs["outputs"]["site_gap_report"]:
            raise ValueError("GAP register must bind site_gap_report")
        report = checked_object(root, refs["outputs"]["site_gap_report"], "site_gap_report")
        site_input = checked_object(root, refs["inputs"]["site_gap_input"], "site_gap_input")
        link_snapshot = checked_object(root, refs["inputs"]["link_snapshot"], "link_snapshot")
        rank_handoff_checks(root, state, site_input, True)
        if (canonical(link_snapshot.get("links_assessment")) != canonical(site_input.get("links_assessment"))
                or canonical(link_snapshot.get("links")) != canonical(site_input.get("links"))):
            raise ValueError("site_gap link source differs from saved link_snapshot")
        raw_link_metadata_checks(root, link_snapshot)
        raw_link_primary_checks(root, link_snapshot)
        assessment = site_input.get("links_assessment")
        if not isinstance(assessment, dict) or assessment.get("status") not in {"available", "partial", "unavailable"}:
            raise ValueError("Complete GAP needs explicit available/partial/unavailable link source assessment")
        saved_link_rows = []
        for path, expected_sha in assessment.get("evidence_files", {}).items():
            if sha_file(relative_file(root, path)) != expected_sha:
                raise ValueError("Link source evidence file SHA-256 mismatch")
            if assessment["status"] in {"available", "partial"}:
                saved = checked_object(root, path, "saved link source")
                rows = saved.get("links")
                if not isinstance(rows, list):
                    raise ValueError("Saved link source needs normalized links rows")
                saved_link_rows.extend(rows)
        if assessment["status"] in {"available", "partial"} and canonical(saved_link_rows) != canonical(link_snapshot.get("links")):
            raise ValueError("Link snapshot rows differ from saved source rows")
        spec = importlib.util.spec_from_file_location("offerist_site_gap_replay", SITE_GAP)
        site_gap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(site_gap)
        computed = site_gap.summarize(site_input)
        if canonical(report) != canonical(computed):
            raise ValueError("Saved site_gap_report differs from recomputed site_gap input")
        validate_planned_route_sources(root, site_input)
        demand_source = checked_object(root, refs["inputs"]["demand_universe"], "demand_universe")
        source_cluster_rows = source_clusters(demand_source)
        input_clusters = site_input.get("demand_review", {}).get("clusters")
        if not isinstance(input_clusters, list):
            raise ValueError("GAP needs source demand clusters and site_gap demand_review clusters")
        def cluster_key(row, type_key):
            return (row.get("id"), row.get(type_key), row.get("status"))
        if sorted(cluster_key(row, "type_id") for row in source_cluster_rows) != sorted(cluster_key(row, "page_type") for row in input_clusters):
            raise ValueError("site_gap input clusters differ from source demand clusters")
        source_by_cluster = {row["id"]: row for row in source_cluster_rows}
        for row in input_clusters:
            source_row = source_by_cluster[row["id"]]
            if ("disposition" in row or "disposition" in source_row) and canonical(row.get("disposition")) != canonical(source_row.get("disposition")):
                raise ValueError("site_gap cluster disposition differs from source demand register")
            if "missing_intent_ids" in source_row and names(source_row["missing_intent_ids"], "source cluster missing intent IDs", allow_empty=True) != names(row.get("missing_intent_ids"), "site_gap cluster missing intent IDs", allow_empty=True):
                raise ValueError("site_gap cluster intent IDs differ from source demand register")
        declared_intents = demand_source.get("intent_ids_by_type", {})
        for intent in site_input.get("missing_intents", []):
            if intent.get("id") not in declared_intents.get(intent.get("page_type"), []):
                raise ValueError("site_gap missing intent is absent from source demand IDs")
        if report.get("assessment", {}).get("whole_site_gap_complete") is not True or report.get("type_gap_totals", {}).get("type_allocation_complete") is not True or data.get("allocation_unknown") != []:
            raise ValueError("Complete GAP requires site_gap assessment and type allocation complete, with no unknown allocation")
        if data.get("url_total") != report["inventory"]["canonical_pages_observed"] or data.get("demand_total") != report["assessment"]["demand_review"]["clusters_observed"]:
            raise ValueError("GAP totals differ from recomputed site_gap input")
        source_urls = checked_object(root, refs["inputs"]["url_universe"], "url_universe")
        raw_pages = source_pages(source_urls)
        input_pages = site_input["inventory"]["pages"]
        if not isinstance(input_pages, list) or len(input_pages) != len(raw_pages) or {(p.get("canonical_url"), p.get("page_type")) for p in input_pages} != {(url, typ) for url, (typ, _) in raw_pages.items()}:
            raise ValueError("site_gap inventory differs from source URL universe")
        for total, covered in (("url_total", "url_classified"), ("demand_total", "demand_allocated")):
            if not isinstance(data.get(total), int) or data[total] < 0 or data.get(covered) != data[total]:
                raise ValueError(f"GAP {covered} must reconcile to {total}")
        weak = data.get("weak_urls_by_type")
        if not isinstance(weak, dict) or set(weak) != expected:
            raise ValueError("GAP weak_urls_by_type must cover every type")
        for values in weak.values():
            names(values, "weak URLs", allow_empty=True)
        if set(source_urls.get("weak_url_ids", [])) != {url for values in weak.values() for url in values}:
            raise ValueError("GAP weak URLs differ from source URL universe")
        if any(raw_pages.get(url, (None,))[0] != type_id for type_id, values in weak.items() for url in values):
            raise ValueError("GAP weak URL belongs to another type or is absent from inventory")
        input_page_actions = {page["canonical_url"]: page["action"] for page in input_pages}
        allocations = data.get("page_allocations")
        if not isinstance(allocations, list) or len(allocations) != len(raw_pages):
            raise ValueError("GAP needs one page allocation per canonical URL, separate from work units")
        allocated = {}
        for allocation in allocations:
            if not isinstance(allocation, dict) or allocation.get("url") not in raw_pages or allocation["url"] in allocated:
                raise ValueError("Duplicate or unknown GAP page allocation")
            url = allocation["url"]
            if allocation.get("type_id") != raw_pages[url][0] or allocation.get("action") != input_page_actions[url] or not names(allocation.get("evidence_refs"), "page allocation evidence"):
                raise ValueError("Page allocation must match typed source inventory and reviewed URL action")
            allocated[url] = allocation
        actions = data.get("actions")
        if not isinstance(actions, list):
            raise ValueError("GAP needs an explicit action list, even when no work is approved")
        action_ids, action_by_id, improved_urls, billed_pages = set(), {}, set(), set()
        dedup_keys, effects_by_url = set(), set()
        for row in actions:
            if not isinstance(row, dict) or not row.get("action_id") or row.get("type_id") not in expected or not row.get("unit") or not isinstance(row.get("quantity"), int) or isinstance(row["quantity"], bool) or row["quantity"] <= 0 or not names(row.get("evidence_refs"), "GAP work evidence"):
                raise ValueError("GAP work item lacks type, unit, quantity or evidence")
            if row["action_id"] in action_ids:
                raise ValueError("Duplicate GAP action_id")
            if row.get("decision") not in {"improve", "create"}:
                raise ValueError("GAP action needs improve or create decision")
            if not isinstance(row.get("dedup_key"), str) or not row["dedup_key"].strip() or row["dedup_key"] in dedup_keys or not isinstance(row.get("effect_key"), str) or not row["effect_key"].strip() or not names(row.get("acceptance_criteria"), "GAP work acceptance"):
                raise ValueError("GAP work item needs unique dedup key, effect and acceptance criteria")
            target_types = names(row.get("target_type_ids"), "GAP work target types")
            if row["type_id"] not in target_types:
                raise ValueError("GAP work primary type must be one of its target types")
            if row["decision"] == "improve":
                affected = names(row.get("affected_urls"), "improve affected URLs")
                if any(input_page_actions.get(url) not in {"optimize", "rework"} for url in affected) or {raw_pages[url][0] for url in affected} != target_types:
                    raise ValueError("Improve work targets must match typed optimize/rework page allocations")
                if len(target_types) > 1 and (not row.get("cross_type_basis") or not names(row.get("cross_type_evidence_refs"), "cross-type work evidence")):
                    raise ValueError("Shared cross-type work needs explicit basis and evidence")
                if any((url, row["effect_key"]) in effects_by_url for url in affected):
                    raise ValueError("Repeated work effect on the same URL is not billable twice")
                effects_by_url.update((url, row["effect_key"]) for url in affected)
                if row["unit"] == "page" and row["quantity"] != len(affected):
                    raise ValueError("Improve page quantity must equal its affected URLs")
                if row["unit"] == "page" and billed_pages & affected:
                    raise ValueError("Existing page cannot be billed by multiple page-unit actions")
                if row["unit"] == "page":
                    billed_pages.update(affected)
                improved_urls.update(affected)
            else:
                if row["unit"] != "page" or row.get("affected_urls") or not names(row.get("landing_job_ids"), "create work landing jobs"):
                    raise ValueError("Create work counts approved landing jobs as pages, not existing URLs")
            action_ids.add(row["action_id"])
            action_by_id[row["action_id"]] = row
            dedup_keys.add(row["dedup_key"])
        planned_urls = {url for url, action in input_page_actions.items() if action in {"optimize", "rework"}}
        if improved_urls != planned_urls:
            raise ValueError("Every optimize/rework URL needs a GAP improve action; keep URLs need none")
        missing = names(data.get("missing_intent_ids"), "missing_intent_ids", allow_empty=True)
        expected_missing = set(report["missing_intents"]["supported_ids"])
        if missing != expected_missing:
            raise ValueError("GAP missing intents must reconcile to recomputed site_gap report")
        candidates = data.get("create_candidates")
        if not isinstance(candidates, list):
            raise ValueError("GAP needs create_candidates, even when empty")
        jobs = data.get("landing_jobs")
        if not isinstance(jobs, list):
            raise ValueError("GAP needs an explicit landing_jobs register, even when empty")
        if canonical(site_input.get("landing_jobs")) != canonical(jobs):
            raise ValueError("site_gap input landing jobs differ from approved GAP register")
        if report.get("page_plan", {}).get("decision_status") != "resolved":
            raise ValueError("Complete GAP needs resolved page-job decisions; pending is not zero pages")
        if report["page_plan"].get("projection_status") != "resolved":
            raise ValueError("Complete GAP needs source-bound canonical routes and distinct create identities")
        covered_candidates, intent_to_job, cluster_to_job = set(), {}, {}
        source_candidates = {row["id"]: row for row in source_cluster_rows if row.get("status") == "create_candidate"}
        report_rows = {row["cluster_id"]: row for row in report["demand_candidate_reconciliation"]["rows"]}
        site_clusters = {row["id"]: row for row in input_clusters}
        job_by_id, created_by_action = {}, {}
        for job in jobs:
            if not isinstance(job, dict) or not isinstance(job.get("landing_job_id"), str) or not job["landing_job_id"].strip() or job["landing_job_id"] in job_by_id:
                raise ValueError("Landing job needs unique ID")
            if job.get("disposition") not in {"create", "no_new_page", "rejected"}:
                raise ValueError("Landing job needs resolved create/no_new_page/rejected disposition; business question stays partial")
            cluster_ids = names(job.get("cluster_ids"), "landing job cluster IDs")
            intent_ids = names(job.get("missing_intent_ids"), "landing job intent IDs", allow_empty=True)
            task_types = names(job.get("task_type_ids"), "landing job task types")
            if not cluster_ids <= set(source_candidates) or task_types != {source_candidates[c]["type_id"] for c in cluster_ids}:
                raise ValueError("Landing job task types must exactly match sourced clusters")
            sourced_intents = set().union(*(set(semantic["intent_ids_by_type"][kind]) for kind in task_types))
            if not intent_ids <= sourced_intents or not names(job.get("evidence_refs"), "landing job evidence"):
                raise ValueError("Landing job intents or evidence are unsourced")
            if job["disposition"] == "create":
                action = action_by_id.get(job.get("action_id"))
                published_type = job.get("published_type_id")
                if published_type not in expected or published_type not in task_types or not action or action["decision"] != "create" or published_type not in set(action["target_type_ids"]) or job["landing_job_id"] not in set(action["landing_job_ids"]) or not intent_ids:
                    raise ValueError("Approved landing job needs published type, create page action and supported intent")
                created_by_action[job["action_id"]] = created_by_action.get(job["action_id"], 0) + 1
            else:
                if job.get("action_id") or job.get("published_type_id") is not None or not job.get("reason"):
                    raise ValueError("Reasoned noncreate landing job cannot carry a create action")
                if job["disposition"] == "no_new_page":
                    covered_urls = names(job.get("covered_by_urls"), "no_new_page covered URLs")
                    if not covered_urls <= set(raw_pages):
                        raise ValueError("No-new-page basis must name existing inventory URLs")
            job_by_id[job["landing_job_id"]] = job
        for row in candidates:
            if not isinstance(row, dict) or row.get("cluster_id") not in source_candidates or row["cluster_id"] in covered_candidates:
                raise ValueError("Create candidate needs unique source cluster_id")
            job = job_by_id.get(row.get("landing_job_id"))
            candidate_intents = names(row.get("missing_intent_ids"), "create candidate intents", allow_empty=True)
            cluster_id = row["cluster_id"]
            task_type = source_candidates[cluster_id]["type_id"]
            if not job or row.get("task_type_id") != task_type or task_type not in set(job["task_type_ids"]) or candidate_intents != set(report_rows[cluster_id]["missing_intent_ids"]):
                raise ValueError("Create candidate needs sourced task type, landing job and exact cluster intents")
            if row.get("disposition") == "create":
                source_disposition = site_clusters[cluster_id].get("disposition")
                if (job["disposition"] != "create" or task_type != job["published_type_id"]
                        or row.get("action_id") != job["action_id"] or not candidate_intents
                        or (source_disposition is not None and (not isinstance(source_disposition, dict)
                            or source_disposition.get("decision") != "create"))):
                    raise ValueError("Create candidate must bind approved landing job and page action")
            elif row.get("disposition") == "integrated_in_job":
                source_disposition = site_clusters[cluster_id].get("disposition")
                if (job["disposition"] != "create" or row.get("action_id") or not row.get("reason") or not isinstance(source_disposition, dict)
                        or source_disposition.get("decision") != "integrated_in_job" or source_disposition.get("covered_by_landing_job_id") != job["landing_job_id"]
                        or not source_disposition.get("basis") or not names(source_disposition.get("evidence"), "integrated task source evidence")
                        or not set(source_disposition["evidence"]) <= names(row.get("evidence_refs"), "integrated task coverage evidence")
                        or not set(source_disposition["evidence"]) <= set(job["evidence_refs"])):
                    raise ValueError("Integrated task needs sourced future-job coverage and no separate page")
            elif row.get("disposition") == "do_not_create":
                source_disposition = site_clusters[cluster_id].get("disposition")
                if job["disposition"] == "create" or row.get("action_id") or row.get("reason") != job["reason"] or not isinstance(source_disposition, dict) or not source_disposition.get("basis") or source_disposition.get("decision") not in {"covered", "no_new_page", "rejected"}:
                    raise ValueError("Reasoned do_not_create needs matching sourced landing decision")
                source_evidence = names(source_disposition.get("evidence"), "noncreate source evidence")
                if job["disposition"] == "rejected" and source_disposition["decision"] != "rejected":
                    raise ValueError("Rejected landing job must match source business disposition")
                if job["disposition"] == "no_new_page" and source_disposition["decision"] not in {"covered", "no_new_page"}:
                    raise ValueError("No-new-page job must match source coverage disposition")
                if job["disposition"] == "no_new_page" and names(source_disposition.get("covered_by_urls"), "source covered URLs") != set(job["covered_by_urls"]):
                    raise ValueError("No-new-page decision must preserve exact sourced existing URLs")
                if not source_evidence <= set(job["evidence_refs"]):
                    raise ValueError("Noncreate landing job loses source evidence")
            else:
                raise ValueError("Create candidate needs create or reasoned do_not_create")
            cluster_to_job[cluster_id] = job["landing_job_id"]
            for intent_id in candidate_intents:
                if intent_id in intent_to_job and intent_to_job[intent_id] != job["landing_job_id"]:
                    raise ValueError("Missing intent is allocated to multiple landing jobs")
                intent_to_job[intent_id] = job["landing_job_id"]
            covered_candidates.add(row["cluster_id"])
        if covered_candidates != set(source_candidates):
            raise ValueError("Every raw demand create_candidate needs GAP disposition")
        if set(intent_to_job) != expected_missing:
            raise ValueError("Every confirmed missing intent needs a landing job disposition")
        for job in jobs:
            cluster_ids = {c for c, job_id in cluster_to_job.items() if job_id == job["landing_job_id"]}
            intent_ids = {i for i, job_id in intent_to_job.items() if job_id == job["landing_job_id"]}
            if cluster_ids != set(job["cluster_ids"]) or intent_ids != set(job["missing_intent_ids"]):
                raise ValueError("Landing job must reconcile its exact source clusters and intents")
            if job["disposition"] == "create" and not any(row["landing_job_id"] == job["landing_job_id"] and row["disposition"] == "create" for row in candidates):
                raise ValueError("Approved landing job needs a primary create candidate of its published type")
        approved_jobs = {job["landing_job_id"]: job for job in jobs if job["disposition"] == "create"}
        for action in actions:
            if action["decision"] == "create":
                target_jobs = names(action["landing_job_ids"], "create action landing jobs")
                if (action["quantity"] != created_by_action.get(action["action_id"], 0)
                        or target_jobs != {job_id for job_id, job in approved_jobs.items() if job["action_id"] == action["action_id"]}
                        or {approved_jobs[job_id]["published_type_id"] for job_id in target_jobs} != set(action["target_type_ids"])):
                    raise ValueError("Create page quantity and targets must equal approved landing jobs")
        approved_by_type = {kind: sum(job["published_type_id"] == kind for job in approved_jobs.values()) for kind in expected}
        plan = report["page_plan"]
        if (plan.get("approved_new_pages") != len(approved_jobs)
                or set(plan.get("approved_new_page_job_ids") or []) != set(approved_jobs)
                or plan.get("approved_new_pages_by_published_type") != {kind: count for kind, count in approved_by_type.items() if count}
                or plan.get("gross_observed_plus_approved_jobs") != len(raw_pages) + len(approved_jobs)):
            raise ValueError("site_gap approved page plan differs from source-bound landing jobs")
        for type_id, decision in semantic["decisions"].items():
            type_urls = {url for url, (kind, _) in raw_pages.items() if kind == type_id}
            type_work = type_urls & planned_urls
            type_created = approved_by_type[type_id] > 0
            type_integrated = any(row["task_type_id"] == type_id and row["disposition"] == "integrated_in_job" for row in candidates)
            type_covered = any(row["task_type_id"] == type_id and row["disposition"] == "do_not_create" and job_by_id[row["landing_job_id"]]["disposition"] == "no_new_page" for row in candidates)
            if decision == "improve" and not type_work:
                raise ValueError("Semantic improve type needs at least one optimize/rework URL; other URLs may keep")
            if decision == "keep" and (type_work or type_created or type_integrated or any(input_page_actions[url] != "keep" for url in type_urls)):
                raise ValueError("Semantic keep type cannot hide URL work or create jobs")
            if decision == "create" and not type_created:
                raise ValueError("Semantic create type needs an approved landing job")
            if decision == "integrate" and (not type_integrated or type_created):
                raise ValueError("Integrated semantic task needs sourced coverage in a future approved page")
            if decision == "covered" and (not type_covered or type_created):
                raise ValueError("Covered semantic task needs sourced existing-page coverage")
            if decision == "reject" and (type_created or type_integrated):
                raise ValueError("Rejected semantic type cannot create a landing job")
            if decision == "business_question":
                raise ValueError("Open business question keeps GAP partial")
        return
    gap = previous_output(root, state, "gap", "gap_register")
    if node_id == "block_gap":
        urls = checked_object(root, state["attempts"]["semantic_types"][-1]["inputs"]["url_universe"]["path"], "url_universe")
        inventory = source_pages(urls)
        approved_jobs = {job["landing_job_id"]: job for job in gap["landing_jobs"] if job["disposition"] == "create"}
        noncreate_jobs = {job["landing_job_id"]: job for job in gap["landing_jobs"] if job["disposition"] != "create"}
        evidence_index = data.get("evidence_index")
        if not isinstance(evidence_index, dict) or not evidence_index:
            raise ValueError("block_gap needs evidence_index of saved HTML/render files")
        for evidence_id, item in evidence_index.items():
            if not evidence_id or not isinstance(item, dict) or item.get("role") not in {"own", "peer"} or item.get("media_type") not in {"text/html", "image/png", "image/jpeg", "image/webp", "application/json"}:
                raise ValueError("block evidence entry lacks ID, role or saved media type")
            path = item.get("path")
            if not isinstance(path, str) or item.get("sha256") != sha_file(relative_file(root, path)):
                raise ValueError("block evidence file SHA-256 mismatch")
            if not relative_file(root, path).read_bytes().strip():
                raise ValueError("block evidence file is empty")
        def resolved(refs, role=None):
            if not isinstance(refs, list) or not refs:
                return False
            return all(ref in evidence_index and (role is None or evidence_index[ref]["role"] == role) for ref in refs)
        def own_urls(refs):
            return {evidence_index[ref].get("source_url") for ref in refs if ref in evidence_index and evidence_index[ref]["role"] == "own"}
        variants = data.get("variants_by_type")
        if not isinstance(variants, dict) or set(variants) != expected:
            raise ValueError("block_gap needs variants for each type")
        covered_pairs, variant_jobs, variant_noncreate_pairs = set(), {}, set()
        for type_id, rows in variants.items():
            if not isinstance(rows, list) or not rows:
                raise ValueError("block_gap has an empty type")
            for variant in rows:
                if not isinstance(variant, dict) or not variant.get("variant_id") or not resolved(variant.get("evidence_refs")) or variant.get("required") is not True or variant.get("observed") not in {True, False}:
                    raise ValueError("block variant needs ID and evidence")
                pair = (type_id, variant["variant_id"])
                if pair in covered_pairs:
                    raise ValueError("Duplicate block type/variant pair")
                jobs = names(variant.get("landing_job_ids", []), "block variant landing job IDs", allow_empty=True)
                noncreate = names(variant.get("noncreate_job_ids", []), "block variant noncreate job IDs", allow_empty=True)
                if any(job_id not in approved_jobs or type_id not in approved_jobs[job_id]["task_type_ids"] for job_id in jobs):
                    raise ValueError("Block variant landing job must be approved and cover this task type")
                if any(job_id not in noncreate_jobs or type_id not in noncreate_jobs[job_id]["task_type_ids"] for job_id in noncreate):
                    raise ValueError("Block variant noncreate job must cover this sourced task type")
                if variant["observed"] is False and (semantic["presence_by_type"][type_id] != "missing" or variant.get("url_refs") or jobs or not noncreate):
                    raise ValueError("Unobserved block variant is only for a sourced absent noncreate task")
                if semantic["presence_by_type"][type_id] == "existing" and not variant.get("url_refs") and not jobs:
                    raise ValueError("Existing block variant needs URL refs or approved landing jobs")
                covered_pairs.add(pair)
                variant_jobs[pair] = jobs
                variant_noncreate_pairs.update((job_id, type_id) for job_id in noncreate)
        assignments = data.get("url_variant_assignments")
        if not isinstance(assignments, list) or len(assignments) != len(inventory):
            raise ValueError("Every canonical URL needs one variant assignment")
        assigned = {}
        for assignment in assignments:
            if not isinstance(assignment, dict) or assignment.get("url") not in inventory or assignment["url"] in assigned:
                raise ValueError("Unknown or duplicate URL variant assignment")
            url = assignment["url"]
            if (assignment.get("type_id"), assignment.get("variant_id")) != inventory[url] or assignment.get("status") != "covered" or not resolved(assignment.get("evidence_refs"), "own") or url not in own_urls(assignment["evidence_refs"]):
                raise ValueError("URL variant assignment is unknown or lacks saved own evidence")
            if (assignment["type_id"], assignment["variant_id"]) not in covered_pairs:
                raise ValueError("URL variant missing from block variant register")
            assigned[url] = assignment
        if set(assigned) != set(inventory):
            raise ValueError("Block variants omit an inventory URL")
        declared_urls = []
        for type_id, variant_rows in variants.items():
            for variant in variant_rows:
                for url in variant.get("url_refs", []):
                    if inventory.get(url) != (type_id, variant["variant_id"]):
                        raise ValueError("Variant URL ref disagrees with raw inventory type/variant")
                    declared_urls.append(url)
        if len(declared_urls) != len(inventory) or set(declared_urls) != set(inventory):
            raise ValueError("Variant URL refs differ from full inventory")
        rows = data.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValueError("block_gap needs block rows")
        seen_pairs, row_job_pairs, row_noncreate_pairs = set(), set(), set()
        for row in rows:
            if not isinstance(row, dict) or not row.get("block_id") or row.get("status") not in {"sufficient", "weak", "missing", "not_applicable"}:
                raise ValueError("Unknown or evidence-free block cannot complete")
            if not resolved(row.get("peer_evidence_refs"), "peer") or not row.get("date"):
                raise ValueError("Block row lacks peer evidence or date")
            if row["status"] in {"weak", "missing"}:
                if not row.get("change") or not row.get("unit") or not row.get("acceptance"):
                    raise ValueError("Weak/missing block needs change, unit and acceptance")
                if not isinstance(row.get("assessment_reason"), str) or not row["assessment_reason"].strip():
                    raise ValueError("Weak/missing block needs assessment_reason")
            elif not row.get("assessment_reason") or any(row.get(field) is not None for field in ("change", "unit", "acceptance")):
                raise ValueError("Sufficient/not_applicable block needs reason and no invented change, unit or acceptance")
            if row.get("evidence_refs") and not resolved(row["evidence_refs"]):
                raise ValueError("Block row evidence refs are unresolved")
            affected = names(row.get("affected_urls", []), "block affected URLs", allow_empty=True)
            jobs = names(row.get("landing_job_ids", []), "block landing job IDs", allow_empty=True)
            noncreate = names(row.get("noncreate_job_ids", []), "block noncreate job IDs", allow_empty=True)
            if not affected and not jobs and not noncreate:
                raise ValueError("Block row lacks affected URLs, approved jobs or sourced noncreate task")
            pair = (row.get("type_id"), row.get("variant_id"))
            if pair not in covered_pairs or any(inventory.get(url) != pair for url in affected):
                raise ValueError("Block affected URL must match inventory type and variant")
            if any(job_id not in approved_jobs or row["type_id"] not in approved_jobs[job_id]["task_type_ids"] or job_id not in variant_jobs[pair] for job_id in jobs):
                raise ValueError("Block landing job must be approved for its type and variant")
            if noncreate:
                if len(noncreate) != 1 or affected or jobs or not noncreate <= set(noncreate_jobs) or not any((job_id, row["type_id"]) in variant_noncreate_pairs for job_id in noncreate):
                    raise ValueError("Evidence-only block row needs one sourced noncreate task of this type/variant")
                source_job = noncreate_jobs[next(iter(noncreate))]
                expected_status = "not_applicable" if source_job["disposition"] == "rejected" else "sufficient"
                if row["status"] != expected_status or row.get("assessment_reason") != source_job["reason"] or names(row.get("source_evidence_refs"), "block noncreate source refs") != set(source_job["evidence_refs"]):
                    raise ValueError("Noncreate block row must preserve exact disposition, reason and source refs")
                if source_job["disposition"] == "no_new_page":
                    if (names(row.get("covered_by_urls"), "block existing coverage URLs") != set(source_job["covered_by_urls"])
                            or not resolved(row.get("own_evidence_refs"), "own")
                            or not set(source_job["covered_by_urls"]) <= own_urls(row["own_evidence_refs"])):
                        raise ValueError("Covered noncreate task needs real existing URL and own proof")
                elif row.get("covered_by_urls") or row.get("own_evidence_refs"):
                    raise ValueError("Rejected absent task cannot invent own page evidence")
            if affected and (not resolved(row.get("own_evidence_refs"), "own") or not affected <= own_urls(row["own_evidence_refs"])):
                raise ValueError("Existing block row needs own evidence")
            if row.get("own_evidence_refs") and not resolved(row["own_evidence_refs"], "own"):
                raise ValueError("Block row own evidence refs are unresolved")
            seen_pairs.add(pair)
            row_job_pairs.update((job_id, row["type_id"]) for job_id in jobs)
            row_noncreate_pairs.update((job_id, row["type_id"]) for job_id in noncreate)
        if seen_pairs != covered_pairs:
            raise ValueError("block rows must cover every type and variant")
        approved_pairs = {(job_id, type_id) for job_id, job in approved_jobs.items() for type_id in job["task_type_ids"]}
        noncreate_pairs = {(job_id, type_id) for job_id, job in noncreate_jobs.items() for type_id in job["task_type_ids"]}
        declared_approved_pairs = {(job_id, type_id) for (type_id, _), ids in variant_jobs.items() for job_id in ids}
        if declared_approved_pairs != approved_pairs or row_job_pairs != approved_pairs or variant_noncreate_pairs != noncreate_pairs or row_noncreate_pairs != noncreate_pairs:
            raise ValueError("Every landing decision and task type needs a block variant and row target")
        return
    if node_id == "text_bar":
        if data.get("report_ref") != refs["outputs"]["bar_report"]:
            raise ValueError("BAR report_ref must bind the report output")
        aggregate = checked_object(root, data["report_ref"], "bar_report")
        method = checked_object(root, refs["inputs"]["far_method"], "far_method")
        manifest = read_json(BAR_MANIFEST)
        if method.get("source") != "BAR" or method.get("version") != manifest["bar_commit"] or method.get("sha256") != manifest["bundle_sha256"]:
            raise ValueError("BAR method provenance is incomplete")
        files = data.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError("BAR needs saved text, input, report files and SHA-256")
        for path, digest in files.items():
            if sha_file(relative_file(root, path)) != digest:
                raise ValueError("BAR saved input/report/text SHA-256 mismatch")
        rows = data.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValueError("BAR needs rows")
        manifest_rows = aggregate.get("reports")
        scored_rows = [row for row in rows if isinstance(row, dict) and row.get("mode") != "not_applicable"]
        if not isinstance(manifest_rows, list) or len(manifest_rows) != len(scored_rows):
            raise ValueError("BAR report manifest must bind every scored row only")
        manifest_pairs = set()
        for item in manifest_rows:
            if not isinstance(item, dict) or item.get("input_ref") not in files or item.get("report_ref") not in files:
                raise ValueError("BAR report manifest needs saved input/report")
            if item.get("input_sha256") != files[item["input_ref"]] or item.get("report_sha256") != files[item["report_ref"]]:
                raise ValueError("BAR report manifest input/report hashes disagree")
            manifest_pairs.add((item["input_ref"], item["report_ref"]))
        seen_types, seen_weak, seen_existing_coverage = set(), set(), set()
        site_input = checked_object(root, state["attempts"]["gap"][-1]["inputs"]["site_gap_input"]["path"], "site_gap_input")
        purpose_by_url = {page["canonical_url"]: page["purpose"] for page in site_input["inventory"]["pages"]}
        source_urls = checked_object(root, state["attempts"]["semantic_types"][-1]["inputs"]["url_universe"]["path"], "url_universe")
        inventory = source_pages(source_urls)
        for row in rows:
            if not isinstance(row, dict) or row.get("type_id") not in expected:
                raise ValueError("BAR row needs a semantic type")
            type_id = row["type_id"]
            if row.get("mode") == "not_applicable":
                applicability = row.get("applicability")
                if not isinstance(applicability, dict) or not all(applicability.get(k) for k in ("functional_role", "purpose", "intent", "reason_code", "reason", "basis", "evidence_files")):
                    raise ValueError("BAR not_applicable needs source-backed role, purpose, intent, reason and basis")
                evidence_files = names(applicability["evidence_files"], "BAR applicability evidence files")
                if not evidence_files <= set(files) or any(not relative_file(root, path).read_bytes().strip() for path in evidence_files):
                    raise ValueError("BAR not_applicable proof needs saved nonempty files and SHA-256")
                if any(row.get(k) is not None for k in ("own_score", "input_ref", "adapter_report_ref", "own_text_ref")) or row.get("peer_text_refs") not in (None, []):
                    raise ValueError("BAR not_applicable must keep own/input/report metrics and refs null")
                if row.get("requirements") not in (None, []):
                    raise ValueError("BAR not_applicable cannot invent text requirements")
                url = row.get("url")
                if url is None:
                    if (semantic["presence_by_type"][type_id] != "missing" or semantic["decisions"][type_id] != "reject"
                            or applicability["functional_role"] != "rejected_unrelated_type" or applicability["reason_code"] != "rejected_unrelated_demand"
                            or applicability["purpose"] != "absent"):
                        raise ValueError("BAR not_applicable absent type needs a sourced rejected semantic decision")
                    rejected_jobs = {job["landing_job_id"] for job in gap["landing_jobs"] if job["disposition"] == "rejected" and type_id in job["task_type_ids"]}
                    if names(row.get("noncreate_job_ids"), "BAR rejected task jobs", allow_empty=True) != rejected_jobs:
                        raise ValueError("BAR rejected type must preserve its noncreate task jobs")
                elif (inventory.get(url, (None,))[0] != type_id or purpose_by_url.get(url) != applicability["purpose"]
                      or purpose_by_url[url] in {"commercial", "informational", "info", "mixed", "unknown"}
                      or applicability["functional_role"] not in {"navigation", "legal", "service_utility"}
                      or applicability["reason_code"] != "non_search_utility"):
                    raise ValueError("BAR not_applicable cannot exempt an existing commercial/info landing or unknown URL")
                if url is not None:
                    if (type_id, url) in seen_weak:
                        raise ValueError("BAR URL appears more than once")
                    seen_weak.add((type_id, url))
                seen_types.add(type_id)
                continue
            if not row.get("peer_text_refs") or not row.get("requirements"):
                raise ValueError("BAR scored row needs peer text and requirements; unavailable peers block complete")
            if any(ref not in files for ref in row["peer_text_refs"]) or row.get("input_ref") not in files or row.get("adapter_report_ref") not in files:
                raise ValueError("BAR peer/input/report reference lacks a saved file")
            if (row["input_ref"], row["adapter_report_ref"]) not in manifest_pairs:
                raise ValueError("BAR row is not in report manifest")
            input_data = checked_object(root, row["input_ref"], "BAR input")
            result = checked_object(root, row["adapter_report_ref"], "BAR adapter report")
            if input_data.get("page_type") != type_id or not input_data.get("intent") or not input_data.get("queries"):
                raise ValueError("BAR input type, intent or queries do not match row")
            if result.get("status") != "ok" or result.get("page_type") != type_id or result.get("intent") != input_data["intent"]:
                raise ValueError("BAR adapter report is not an ok result for this type/intent")
            replay_bar(root, row["input_ref"], result)
            provenance = result.get("provenance", {})
            if provenance.get("bar_commit") != manifest["bar_commit"] or provenance.get("bundle_sha256") != manifest["bundle_sha256"] or provenance.get("dictionary_sha256") != manifest["dictionary_sha256"]:
                raise ValueError("BAR adapter provenance differs from bundled manifest")
            if not isinstance(result.get("coverage"), dict) or result["coverage"].get("available_zones") != ["all", "text"]:
                raise ValueError("BAR coverage and available zones are missing")
            peer_texts = [relative_file(root, ref).read_text(encoding="utf-8-sig") for ref in row["peer_text_refs"]]
            if peer_texts != [p.get("text") for p in input_data.get("peers", [])]:
                raise ValueError("BAR peer texts differ from saved inputs")
            seen_types.add(type_id)
            if semantic["presence_by_type"][type_id] == "missing":
                target_jobs = {job["landing_job_id"]: job for job in gap["landing_jobs"] if job["disposition"] == "create" and type_id in job["task_type_ids"]}
                covered_jobs = {job["landing_job_id"]: job for job in gap["landing_jobs"] if job["disposition"] == "no_new_page" and type_id in job["task_type_ids"]}
                pending_jobs = {job["landing_job_id"]: job for job in gap["landing_jobs"] if job["disposition"] == "business_question" and type_id in job["task_type_ids"]}
                if row.get("page_disposition") == "covered_by_existing_url":
                    url = row.get("url")
                    actual_type = inventory.get(url, (None,))[0]
                    expected_jobs = {job_id for job_id, job in covered_jobs.items() if url in job["covered_by_urls"]}
                    if (not expected_jobs or row.get("actual_type_id") != actual_type
                            or names(row.get("noncreate_job_ids"), "BAR existing-coverage jobs") != expected_jobs):
                        raise ValueError("BAR covered missing task needs exact sourced existing URL, actual type and noncreate jobs")
                    seen_existing_coverage.update((job_id, type_id, url) for job_id in expected_jobs)
                    score = row.get("own_score")
                    if (row.get("mode") != "own_and_peer" or not isinstance(score, dict)
                            or set(score) != {"key_tfidf_score", "weighted_coverage", "weighted_density"}
                            or score != result.get("own_score") or result.get("mode") != "own_vs_peers"
                            or result.get("reference_role") != "own" or not result.get("report")
                            or row.get("own_text_ref") not in files
                            or input_data.get("own_page", {}).get("text") != relative_file(root, row["own_text_ref"]).read_text(encoding="utf-8-sig")):
                        raise ValueError("BAR covered existing URL needs actual own-and-peer report and saved text")
                else:
                    if row.get("mode") != "peer_creation" or row.get("own_score") is not None or row.get("own_text_ref") is not None or row.get("url") is not None or input_data.get("own_page") is not None or result.get("own_score") is not None or result.get("mode") != "peer_only" or result.get("reference_role") != "peer" or not result.get("peer_benchmark"):
                        raise ValueError("Missing new-page type cannot invent own text, URL or score")
                    if row.get("page_disposition") == "candidate_pending":
                        gap_site_input = checked_object(root, state["attempts"]["gap"][-1]["inputs"]["site_gap_input"]["path"], "site_gap_input")
                        gap_site_report = previous_output(root, state, "gap", "site_gap_report")
                        if (not pending_jobs or target_jobs or semantic["decisions"][type_id] != "business_question"
                                or state["attempts"]["gap"][-1]["status"] != "partial"
                                or names(row.get("pending_landing_job_ids"), "BAR pending candidate jobs") != set(pending_jobs)
                                or row.get("target_landing_job_ids") not in (None, [])
                                or row.get("action_id") is not None or row.get("published_type_id") is not None
                                or gap_site_report.get("page_plan", {}).get("approved_new_pages") is not None
                                or any(job.get("landing_job_id") in pending_jobs for job in (gap_site_input.get("landing_jobs") or []))):
                            raise ValueError("BAR pending candidate cannot claim an approved page or unsourced decision")
                    else:
                        if (pending_jobs or not target_jobs
                                or names(row.get("target_landing_job_ids"), "BAR missing-type landing jobs") != set(target_jobs)):
                            raise ValueError("BAR missing task must bind every approved future page covering it")
                        expected_disposition = "integrated_in_job" if all(job["published_type_id"] != type_id for job in target_jobs.values()) else "new_page" if all(job["published_type_id"] == type_id for job in target_jobs.values()) else "mixed"
                        if row.get("page_disposition") != expected_disposition:
                            raise ValueError("BAR task type and published page disposition disagree")
            else:
                score = row.get("own_score")
                if row.get("mode") != "own_and_peer" or not isinstance(score, dict) or set(score) != {"key_tfidf_score", "weighted_coverage", "weighted_density"} or score != result.get("own_score") or result.get("mode") != "own_vs_peers" or result.get("reference_role") != "own" or not result.get("report") or not row.get("own_text_ref"):
                    raise ValueError("Existing type requires actual separate BAR metrics and own/peer report")
                if row["own_text_ref"] not in files or input_data.get("own_page", {}).get("text") != relative_file(root, row["own_text_ref"]).read_text(encoding="utf-8-sig"):
                    raise ValueError("BAR own text reference lacks a saved file")
            if row.get("url"):
                if bar_input_url(input_data) != row["url"]:
                    raise ValueError("BAR input URL differs from row URL")
                seen_weak.add((type_id, row["url"]))
        required_weak = {(t, url) for t, urls in gap["weak_urls_by_type"].items() for url in urls}
        expected_existing_coverage = {(job["landing_job_id"], type_id, url) for job in gap["landing_jobs"]
                                      if job["disposition"] == "no_new_page" for type_id in job["task_type_ids"]
                                      for url in job["covered_by_urls"]}
        scored_pairs = {(row["input_ref"], row["adapter_report_ref"]) for row in scored_rows}
        if manifest_pairs != scored_pairs or seen_types != expected or not required_weak <= seen_weak or seen_existing_coverage != expected_existing_coverage:
            raise ValueError("BAR must cover every type and weak URL")
        demand_ref = state["attempts"]["semantic_types"][-1]["inputs"]["demand_universe"]["path"]
        material_task_scope_checks(root, checked_object(root, refs["inputs"]["material_task_scopes"], "material_task_scopes"),
                                   data, rows, inventory, gap, strict, checked_object(root, demand_ref, "demand_universe"), demand_ref)


def review_binding(root, state, refs, node_id):
    if node_id not in {"structure_review", "client_review"}:
        return
    receipt = read_json(relative_file(root, refs["inputs"]["review_receipt"]))
    if node_id == "structure_review":
        expected = state["attempts"]["decision_map"][-1]["outputs"]["decision_map"]["sha256"]
    else:
        if refs["inputs"]["review_receipt"] != "review.json":
            raise ValueError("Client review must use canonical review.json")
        packet = previous_output(root, state, "proposal", "client_packet")
        expected = packet["packet_sha256"]
        if receipt.get("packet_sha256") != expected or receipt.get("proposal_sha256") != packet["proposal_sha256"]:
            raise ValueError("Client review proposal/packet lineage differs from workflow")
        if receipt.get("context_mode") != "fresh":
            raise ValueError("Client review needs fresh context declaration")
    for key in ("reviewer_id", "producer_id", "context_id", "producer_context_id", "candidate_sha256", "verdict"):
        if not receipt.get(key):
            raise ValueError(f"Review receipt lacks {key}")
    if receipt["reviewer_id"] == receipt["producer_id"] or receipt["context_id"] == receipt["producer_context_id"]:
        raise ValueError("Review receipt does not show a distinct reviewer/context")
    if receipt["candidate_sha256"] != expected:
        raise ValueError("Review receipt is stale against its reviewed artifact")
    host = checked_object(root, refs["inputs"]["host_receipt"], "host_receipt")
    if receipt.get("independence_status") != "host_evidenced" or host.get("source") != "host_run_receipt" or not host.get("source_reference"):
        raise ValueError("Complete review needs host-evidenced provenance, not IDs alone")
    if host.get("candidate_sha256") != expected or host.get("reviewer_id") != receipt["reviewer_id"] or host.get("producer_id") != receipt["producer_id"] or host.get("reviewer_context_id") != receipt["context_id"] or host.get("producer_context_id") != receipt["producer_context_id"]:
        raise ValueError("Host receipt does not bind reviewer, contexts and candidate")
    if node_id == "structure_review":
        if receipt["verdict"] != "ready_to_draft":
            raise ValueError("Structure review must be ready_to_draft")
    else:
        if receipt.get("content_verdict") != "accept":
            raise ValueError("Client content verdict must accept the complete packet")
        commercial = previous_output(root, state, "estimate", "estimate_sheet")["commercial_status"]
        if commercial == "estimate_pending_human" and receipt.get("verdict") == "accept":
            raise ValueError("Pending human estimate cannot imply purchase acceptance")


def material_sources(root, state):
    """Every material finding receives an explicit strategy disposition."""
    gap = previous_output(root, state, "gap", "gap_register")
    audit = previous_output(root, state, "audit", "audit_findings")
    block = previous_output(root, state, "block_gap", "block_register")
    bar = previous_output(root, state, "text_bar", "text_register")
    commercial = previous_output(root, state, "commercial", "commercial_findings")
    ids = [f"gap:{r['action_id']}" for r in gap["actions"]]
    ids += [f"gap_job:{r['landing_job_id']}" for r in gap["landing_jobs"] if r["disposition"] != "create"]
    ids += [f"audit:{r['finding_id']}" for r in audit["findings"]]
    ids += [f"block:{r['type_id']}:{r['variant_id']}:{r['block_id']}" for r in block["rows"] if r["status"] in {"weak", "missing"}]
    ids += sorted({f"bar:{r['type_id']}:{r.get('url') or 'creation'}" for r in bar["rows"]})
    ids += [f"bar_scope:{r['scope_id']}" for r in bar["scope_coverage"]]
    ids += [f"commercial:{r['observation_id']}" for r in commercial["observations"]]
    if read_json(relative_file(root, state["scope_ref"])).get("geo") == "yes":
        geo = previous_output(root, state, "geo", "geo_findings")
        ids += [f"geo:{r['finding_id']}" for r in geo["findings"] if r["materiality"] == "material"]
    if len(ids) != len(set(ids)):
        raise ValueError("Material source IDs collide; deduplicate explicitly before strategy")
    return set(ids)


def ledger_projection(strategy):
    return {row["source_id"]: {key: row[key] for key in ("disposition", "action_id", "reason", "target_urls", "target_landing_job_ids", "source_change", "acceptance", "evidence_refs", "scope_reason") if key in row}
            for row in strategy["decision_ledger"]}


def bar_reading_projection(root, state, strategy):
    """Carry the analyst's immutable scoped reading through the existing map."""
    register = previous_output(root, state, "text_bar", "text_register")
    ledger = ledger_projection(strategy)
    return [{"scope_id": item["scope_id"], "reading": item["reading"],
             "strategy_disposition": ledger[f"bar_scope:{item['scope_id']}"]}
            for item in sorted(register["scope_coverage"], key=lambda value: value["scope_id"])
            if item["status"] == "scored"]


def material_source_requirements(root, state):
    """Concrete targets and acceptance supplied by material specialist findings."""
    required = {}
    audit = previous_output(root, state, "audit", "audit_findings")
    block = previous_output(root, state, "block_gap", "block_register")
    bar = previous_output(root, state, "text_bar", "text_register")
    commercial = previous_output(root, state, "commercial", "commercial_findings")
    for row in audit["findings"]:
        required[f"audit:{row['finding_id']}"] = ({row["url"]}, set(), row["action"], row["acceptance"], {row["source_ref"]}, None)
    for row in block["rows"]:
        if row["status"] in {"weak", "missing"}:
            required[f"block:{row['type_id']}:{row['variant_id']}:{row['block_id']}"] = (
                set(row.get("affected_urls", [])), set(row.get("landing_job_ids", [])), row["change"], row["acceptance"],
                set(row.get("evidence_refs", [])) | set(row.get("own_evidence_refs", [])) | set(row.get("peer_evidence_refs", [])), row["type_id"])
    for row in bar["rows"]:
        if row.get("mode") != "not_applicable":
            source_id = f"bar:{row['type_id']}:{row.get('url') or 'creation'}"
            prior = required.get(source_id)
            if row.get("mode") == "pending":
                evidence = {row["own_text_ref"]} | set(row.get("peer_text_refs", []))
                if row.get("peer_report_ref"):
                    evidence.add(row["peer_report_ref"])
                change, acceptance = row["change"], row["acceptance"]
            else:
                evidence = {row["adapter_report_ref"]}
                change, acceptance = None, None
            evidence |= prior[4] if prior else set()
            required[source_id] = (
                {row["url"]} if row.get("url") else set(), set(row.get("target_landing_job_ids", [])), change, acceptance,
                evidence, row["type_id"])
    for coverage in bar["scope_coverage"]:
        if coverage["status"] != "scored":
            continue
        row = bar["rows"][coverage["row_index"]]
        required[f"bar_scope:{coverage['scope_id']}"] = (
            {row["url"]} if row.get("url") else set(), set(row.get("target_landing_job_ids", [])), None, None,
            {row["adapter_report_ref"]}, row["type_id"])
    for row in commercial["observations"]:
        required[f"commercial:{row['observation_id']}"] = ({row["url"]}, set(), row["change"], None, {row["evidence_ref"]}, None)
    if read_json(relative_file(root, state["scope_ref"])).get("geo") == "yes":
        geo = previous_output(root, state, "geo", "geo_findings")
        for row in geo["findings"]:
            if row["materiality"] == "material":
                required[f"geo:{row['finding_id']}"] = (set(), set(), row["action"], row["acceptance"], set(row["evidence_refs"]), None)
    return required


def phase_projection(actions):
    """Compute work horizons from the selected action schedule, never from prose."""
    phases = ("B3", "B6", "B12", "later")
    groups = {phase: [] for phase in phases}
    for row in actions:
        if row.get("phase") not in groups:
            raise ValueError("Selected strategy action needs a canonical phase")
        groups[row["phase"]].append(row)
    priorities = [{"phase": phase, "work": sorted(row["action_id"] for row in groups[phase])}
                  for phase in phases]
    cumulative, ids, targets = {}, [], {}
    for phase in phases[:3]:
        for row in groups[phase]:
            ids.append(row["action_id"])
            cumulative[row["unit"]] = cumulative.get(row["unit"], 0) + row["quantity"]
        targets[phase] = {"action_ids": sorted(ids), "volumes_by_unit": dict(cumulative)}
    return {"priorities": priorities, "b_targets": targets}


def volume_checks(root, state, refs, node_id, strict):
    if node_id not in {"strategy", "implementation", "team", "estimate"}:
        return
    if node_id == "strategy":
        gap = previous_output(root, state, "gap", "gap_register")
        strategy = checked_object(root, refs["outputs"]["strategy_map"], "strategy_map")
        available = {row["action_id"]: row for row in gap["actions"]}
        selected = names(strategy.get("selected_action_ids"), "selected_action_ids")
        actions = strategy.get("actions")
        if not isinstance(actions, list) or {row.get("action_id") for row in actions} != selected or len(actions) != len(selected):
            raise ValueError("Strategy action catalog must match selected IDs")
        action_by_id, strategy_dedup, strategy_effects, billed_urls, billed_jobs = {}, set(), set(), set(), set()
        for row in actions:
            if row.get("action_id") in available and any(row.get(k) != available[row["action_id"]].get(k) for k in ("type_id", "unit", "quantity", "affected_urls", "landing_job_ids", "target_type_ids", "dedup_key", "effect_key", "acceptance_criteria")):
                raise ValueError("Strategy action disagrees with GAP work scope, quantities or acceptance")
            if not row.get("type_id") or not row.get("unit") or not isinstance(row.get("quantity"), int) or isinstance(row["quantity"], bool) or row["quantity"] <= 0:
                raise ValueError("Strategy action needs type, unit and quantity")
            urls = names(row.get("affected_urls", []), "strategy work URLs", allow_empty=True)
            jobs = names(row.get("landing_job_ids", []), "strategy work jobs", allow_empty=True)
            if not urls and not jobs or not names(row.get("target_type_ids"), "strategy work target types") or not names(row.get("acceptance_criteria"), "strategy work acceptance") or not row.get("dedup_key") or not row.get("effect_key") or row["dedup_key"] in strategy_dedup:
                raise ValueError("Strategy work needs unique dedup key, real targets and acceptance")
            if any((url, row["effect_key"]) in strategy_effects for url in urls):
                raise ValueError("Strategy repeats the same work effect on one URL")
            strategy_effects.update((url, row["effect_key"]) for url in urls)
            if row["unit"] == "page":
                if (urls and jobs) or row["quantity"] != len(urls or jobs) or billed_urls & urls or billed_jobs & jobs:
                    raise ValueError("Strategy page units must count unique existing URLs or approved new jobs")
                billed_urls.update(urls)
                billed_jobs.update(jobs)
            strategy_dedup.add(row["dedup_key"])
            action_by_id[row["action_id"]] = row
        ledger = strategy.get("decision_ledger")
        required = material_sources(root, state)
        bar_register = previous_output(root, state, "text_bar", "text_register")
        block_register = previous_output(root, state, "block_gap", "block_register")
        unknown_blocks = {f"{row['type_id']}:{row['variant_id']}:{row['block_id']}" for row in block_register["rows"]
                          if row.get("status") == "unknown"}
        pending_jobs = {job["landing_job_id"] for job in gap["landing_jobs"] if job["disposition"] == "business_question"}
        if not strict:
            pending_items = strategy.get("pending_items", [])
            declared_blocks = {item["scope_id"] for item in pending_items if item["scope_kind"] == "block"}
            declared_jobs = {item["scope_id"] for item in pending_items if item["scope_kind"] == "job"}
            if not unknown_blocks <= declared_blocks or not pending_jobs <= declared_jobs:
                raise ValueError("Partial strategy needs explicit pending block/job source IDs")
        for action in actions:
            if set(action.get("landing_job_ids", [])) & pending_jobs and (action.get("unit") == "page"
                    or not action.get("access_condition") or not action.get("deliverable")):
                raise ValueError("Pending landing decision permits only bounded verification work")
        na_sources = {f"bar:{row['type_id']}:{row.get('url') or 'creation'}": row["applicability"]["reason"]
                      for row in bar_register["rows"] if row.get("mode") == "not_applicable"}
        na_sources.update({f"bar_scope:{coverage['scope_id']}": coverage["reason"]
                           for coverage in bar_register["scope_coverage"] if coverage["status"] == "not_applicable"})
        noncreate_jobs = {f"gap_job:{job['landing_job_id']}": job["reason"] for job in gap["landing_jobs"] if job["disposition"] != "create"}
        source_requirements = material_source_requirements(root, state)
        bar_scopes = {f"bar_scope:{item['scope_id']}": item for item in bar_register["scope_coverage"]}
        allocations = {row["url"]: row["action"] for row in gap["page_allocations"]}
        if not isinstance(ledger, list) or len(ledger) != len(required) or {row.get("source_id") for row in ledger} != required:
            raise ValueError("Strategy ledger must dispose every GAP/audit/block/BAR/commercial/GEO finding exactly once")
        selected_from_ledger = set()
        for row in ledger:
            if row["source_id"] in na_sources:
                if row.get("disposition") != "not_applicable" or row.get("reason") != na_sources[row["source_id"]] or row.get("action_id") is not None:
                    raise ValueError("BAR not_applicable source needs reasoned not_applicable ledger disposition without action")
                continue
            if row["source_id"] in noncreate_jobs:
                if row.get("disposition") != "rejected_with_reason" or row.get("reason") != noncreate_jobs[row["source_id"]] or row.get("action_id") is not None:
                    raise ValueError("Noncreate landing job needs the exact reasoned strategy disposition without page action")
                continue
            if row.get("disposition") == "selected":
                if row.get("action_id") not in selected:
                    raise ValueError("Selected ledger source needs cataloged action")
                action = action_by_id[row["action_id"]]
                source_id = row["source_id"]
                if source_id in bar_scopes:
                    bar_scope = bar_scopes[source_id]
                    if bar_scope["status"] != "scored":
                        raise ValueError("Only scored BAR scope can select text work")
                    bar_row = bar_register["rows"][bar_scope["row_index"]]
                    proposed = bar_scope["reading"].get("proposed_text_change")
                    pointer = bar_scope["reading"]["source_pointer"]
                    cited_text = ({pointer["file_ref"]} if pointer["kind"] == "saved_text" else set())
                    if (bar_row.get("page_disposition") == "candidate_pending" or not proposed
                            or row.get("source_change") != proposed
                            or row.get("acceptance") not in action["acceptance_criteria"]
                            or not cited_text <= set(row.get("evidence_refs", []))):
                        raise ValueError("Selected BAR scope needs its recorded proposed text change and source")
                if source_id.startswith("gap:"):
                    if source_id != f"gap:{row['action_id']}":
                        raise ValueError("Selected GAP work source must bind its own action")
                elif source_id in source_requirements:
                    urls, jobs, change, acceptance, evidence, task_type = source_requirements[source_id]
                    if (names(row.get("target_urls", []), "selected source URLs", allow_empty=True) != urls
                            or names(row.get("target_landing_job_ids", []), "selected source jobs", allow_empty=True) != jobs
                            or not urls <= names(action.get("affected_urls", []), "selected action URLs", allow_empty=True)
                            or not jobs <= names(action.get("landing_job_ids", []), "selected action jobs", allow_empty=True)
                            or (task_type and not jobs and task_type not in set(action["target_type_ids"]))):
                        raise ValueError("Selected source does not match action URL/job/type scope")
                    if (change is not None and row.get("source_change") != change) or (acceptance is not None and (row.get("acceptance") != acceptance or acceptance not in set(action["acceptance_criteria"]))) or not evidence <= names(row.get("evidence_refs"), "selected source evidence"):
                        raise ValueError("Selected source loses change, acceptance or evidence")
                    if source_id.startswith("block:") and any(allocations.get(url) == "keep" for url in urls):
                        raise ValueError("Selected weak block on keep URL requires revised page allocation")
                    if not urls and not jobs and (not row.get("scope_reason") or not set(row["evidence_refs"]) & set(action.get("evidence_refs", []))):
                        raise ValueError("Targetless selected finding needs explicit scope and shared evidence")
                selected_from_ledger.add(row["action_id"])
            elif row.get("disposition") not in {"deferred_with_reason", "rejected_with_reason", "blocked"} or not row.get("reason"):
                raise ValueError("Nonselected finding needs explicit reasoned disposition")
        selected_ids = {item["source_id"] for item in ledger if item["disposition"] == "selected"}
        for bar_row in bar_register["rows"]:
            row_source = f"bar:{bar_row['type_id']}:{bar_row.get('url') or 'creation'}"
            if row_source in selected_ids and not any(
                    f"bar_scope:{item['scope_id']}" in selected_ids
                    for item in bar_register["scope_coverage"]
                    if item.get("row_index") is not None
                    and bar_register["rows"][item["row_index"]] is bar_row):
                raise ValueError("Selected BAR row needs a justified selected text scope")
        if selected_from_ledger != selected:
            raise ValueError("Every selected action needs ledger source")
        projection = phase_projection(actions)
        priorities = strategy.get("priorities")
        if (not isinstance(priorities, list)
                or [{"phase": row.get("phase"), "work": row.get("work")} for row in priorities
                    if isinstance(row, dict)] != projection["priorities"]
                or len(priorities) != len(projection["priorities"])):
            raise ValueError("Strategy priorities must project canonical action phases")
        if strategy.get("b_targets") != projection["b_targets"]:
            raise ValueError("Strategy B targets must project cumulative selected work; keep effect forecasts separate")
        return
    if node_id == "implementation":
        strategy = previous_output(root, state, "strategy", "strategy_map")
        source = {row["action_id"]: row for row in strategy["actions"]}
        plan = checked_object(root, refs["outputs"]["implementation_plan"], "implementation_plan")
        rows = plan.get("actions")
        if not isinstance(rows, list) or not rows:
            raise ValueError("Implementation needs action rows")
        seen, totals = set(), {}
        increments = {phase: {} for phase in ("B3", "B6", "B12", "later")}
        for row in rows:
            if not isinstance(row, dict) or row.get("action_id") not in source or row["action_id"] in seen:
                raise ValueError("Implementation action missing, unknown or duplicated")
            source_row = source[row["action_id"]]
            if any(row.get(k) != source_row.get(k) for k in ("type_id", "unit", "quantity", "affected_urls", "landing_job_ids", "target_type_ids", "dedup_key", "effect_key", "acceptance_criteria")):
                raise ValueError("Implementation action units/quantity disagree with GAP")
            if row.get("phase") not in {"B3", "B6", "B12", "later"} or not row.get("acceptance") or not row.get("owner_dependency"):
                raise ValueError("Implementation needs phase, acceptance and dependency")
            if row["phase"] != source_row.get("phase"):
                raise ValueError("Implementation phase disagrees with canonical strategy action")
            work = row.get("work_scope")
            required_work = {"before", "after", "deliverable", "acceptance", "operation_kind"}
            if (not isinstance(work, dict) or not required_work <= set(work)
                    or set(work) - required_work - {"checkpoint_receipts"}
                    or any(not isinstance(work[key], str) or not work[key].strip() for key in required_work)
                    or work["operation_kind"] not in {"page_content", "page_creation", "navigation_cms", "integration", "diagnostic", "other"}
                    or (row["unit"] == "page" and work["operation_kind"] not in {"page_content", "page_creation"})
                    or (row["unit"] != "page" and work["operation_kind"] in {"page_content", "page_creation"})
                    or ("checkpoint_receipts" in work and
                        (not isinstance(work["checkpoint_receipts"], list)
                         or any(not isinstance(item, str) or not item.strip() for item in work["checkpoint_receipts"])
                         or len(work["checkpoint_receipts"]) != len(set(work["checkpoint_receipts"]))))
                    or work["acceptance"] != row["acceptance"]):
                raise ValueError("Implementation action needs bounded before/after, deliverable and acceptance")
            dependency = row.get("phase_dependency")
            if row["phase"] != "B3" and (row.get("affected_urls") or row.get("landing_job_ids")):
                if (not isinstance(dependency, dict)
                        or set(dependency) != {"source_ref", "owner", "condition", "effect"}
                        or any(not isinstance(dependency[key], str) or not dependency[key].strip() for key in dependency)):
                    raise ValueError("Later selected work needs an explicit phase dependency")
                relative_file(root, dependency["source_ref"])
            seen.add(row["action_id"])
            totals[row["unit"]] = totals.get(row["unit"], 0) + row["quantity"]
            phase = increments[row["phase"]]
            phase[row["unit"]] = phase.get(row["unit"], 0) + row["quantity"]
        if seen != set(strategy["selected_action_ids"]) or plan.get("volumes_by_unit") != totals:
            raise ValueError("Implementation must cover selected actions and reconcile volumes")
        if plan.get("incremental_by_phase") != increments:
            raise ValueError("Implementation phases must reconcile to action increments")
        running, cumulative = {}, {}
        for phase in ("B3", "B6", "B12"):
            for unit, amount in increments[phase].items():
                running[unit] = running.get(unit, 0) + amount
            cumulative[phase] = dict(running)
        if plan.get("cumulative_by_phase") != cumulative:
            raise ValueError("B3/B6/B12 cumulative volumes disagree with increments")
        return
    if node_id == "team":
        team = checked_object(root, refs["outputs"]["team_needs"], "team_needs")
        required = {"content", "content_management", "development", "design", "analytics"}
        if not isinstance(team.get("need_by_role"), dict) or not required <= set(team["need_by_role"]) or any(not isinstance(team["need_by_role"][r], bool) for r in required):
            raise ValueError("Team needs all required roles")
        providers = team.get("provider_by_role")
        if not isinstance(providers, dict) or set(providers) != set(team["need_by_role"]) or any(value not in {None, ""} for value in providers.values()):
            raise ValueError("Team provider fields must stay blank for a human")
        return
    plan = previous_output(root, state, "implementation", "implementation_plan")
    estimate = checked_object(root, refs["outputs"]["estimate_sheet"], "estimate_sheet")
    if estimate.get("volumes_by_unit") != plan["volumes_by_unit"]:
        raise ValueError("Estimate volumes must reconcile with implementation")
    estimate_line_checks(plan, estimate)
    commercial = estimate.get("commercial_status")
    if commercial not in {"estimate_pending_human", "human_confirmed"}:
        raise ValueError("Estimate needs commercial_status")
    rates, sums = estimate.get("rates_by_unit"), estimate.get("sums_by_unit")
    if commercial == "estimate_pending_human":
        if rates not in ({}, None) or sums not in ({}, None) or estimate.get("terms") not in (None, ""):
            raise ValueError("Pending human estimate must leave rates, sums and terms blank")
    else:
        if not isinstance(rates, dict) or set(rates) != set(plan["volumes_by_unit"]) or not isinstance(sums, dict) or set(sums) != set(rates):
            raise ValueError("Confirmed estimate needs all rates and sums")
        for unit, count in plan["volumes_by_unit"].items():
            rate = rates[unit]
            if not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate < 0 or sums[unit] != count * rate:
                raise ValueError("Confirmed estimate arithmetic does not reconcile")
        if not estimate.get("terms") or not estimate.get("human_source_ref"):
            raise ValueError("Confirmed estimate needs human terms and source")


def acceptance(root, receipt):
    value = receipt.get("user_acceptance")
    if value == "unknown":
        return "unknown"
    if not isinstance(value, dict) or value.get("status") not in {"accepted", "rejected"}:
        raise ValueError("user_acceptance must be unknown or an evidenced decision")
    quote, source = value.get("user_quote"), value.get("source_ref")
    if not isinstance(quote, str) or not quote.strip() or not isinstance(source, str):
        raise ValueError("User acceptance needs source and quote")
    if quote not in relative_file(root, source).read_text(encoding="utf-8-sig"):
        raise ValueError("User acceptance quote not found in source")
    return value["status"]


def readiness(root, state, states, nodes, order, scope):
    required = [n for n in order if active(nodes[n], scope)]
    content_complete = scope.get("geo") != "undecided" and all(states[n]["state"] == "complete" for n in required)
    commercial = "estimate_pending_human"
    if states["estimate"]["state"] == "complete":
        estimate = previous_output(root, state, "estimate", "estimate_sheet")
        commercial = estimate["commercial_status"]
    user = "unknown"
    user_claim = None
    if states["release_gate"]["state"] == "complete":
        receipt = previous_output(root, state, "release_gate", "release_receipt")
        claim = acceptance(root, receipt)
        if claim != "unknown":
            user_claim = claim  # A run-local file cannot prove user origin.
    reviews = {}
    for node_id in ("structure_review", "client_review"):
        if states[node_id]["state"] == "complete":
            ref = state["attempts"][node_id][-1]["inputs"]["review_receipt"]["path"]
            receipt = checked_object(root, ref, "review_receipt")
            reviews[node_id] = receipt.get("independence_status", "declared")
        else:
            reviews[node_id] = "unverified"
    return content_complete, commercial, user, user_claim, reviews


def record(args):
    root, state = root_and_state(args.run)
    raw, nodes, order = contract()
    if args.node not in nodes:
        raise ValueError("Unknown node")
    node = nodes[args.node]
    scope, states, _ = current(root, state, raw, nodes, order)
    if not active(node, scope):
        raise ValueError("Node is outside selected scope")
    if args.status not in VALID_STATUSES:
        raise ValueError("Invalid status")
    for parent in required_parents(node, nodes, scope):
        if states[parent]["state"] not in {"complete", "partial", "skipped"}:
            raise ValueError(f"Dependency {parent} is {states[parent]['state']}; resume cannot bypass it")
    if args.status == "complete" and args.node in {"strategy", "implementation"} and states["gap"]["state"] != "complete":
        gap_data = previous_output(root, state, "gap", "gap_register")
        if any(job.get("disposition") == "business_question" for job in gap_data.get("landing_jobs", [])):
            raise ValueError("Complete strategy/implementation cannot approve a pending GAP candidate")
    if args.actor != node["actor"]:
        raise ValueError(f"Actor for {args.node} must be {node['actor']}")
    inputs, outputs = pairs(args.input), pairs(args.output)
    checks = set(args.check)
    if args.status in {"complete", "partial"}:
        if set(inputs) != set(node["inputs"]) or set(outputs) != set(node["outputs"]):
            raise ValueError("Required named inputs and outputs do not match contract")
        if checks != set(node["checks"]):
            raise ValueError("Every named structural check must be recorded")
    elif args.status == "blocked":
        if not args.reason or not args.blocker:
            raise ValueError("Blocked requires reason and blocker")
    elif args.status == "skipped":
        exceptions = scope.get("exceptions", [])
        def supported_exception(e):
            if e.get("node") != args.node or e.get("allow_skip") is not True:
                return False
            quote, source = e.get("user_quote"), e.get("source_ref")
            if not isinstance(quote, str) or not quote.strip() or not isinstance(source, str):
                return False
            if source == state["scope_ref"]:
                return False
            try:
                return quote in relative_file(root, source).read_text(encoding="utf-8-sig")
            except (ValueError, UnicodeError):
                return False
        matched = next((e for e in exceptions if supported_exception(e)), None)
        if matched is None:
            raise ValueError("Skipped mandatory node requires explicit user scope exception")
        inputs["scope_exception"] = matched["source_ref"]
        if not args.reason:
            raise ValueError("Skipped requires reason")
    if args.status == "partial" and (not args.reason or not args.blocker):
        raise ValueError("Partial requires reason and blocker")
    if args.status == "complete" and args.blocker:
        raise ValueError("Complete cannot have blockers")
    manifest_ref = getattr(args, "manifest_ref", None)
    if manifest_ref:
        inputs["workflow_manifest"] = manifest_ref
    refs = {"inputs": inputs, "outputs": outputs}
    if args.status in {"complete", "partial"}:
        stage_artifact_checks(root, refs)
        strict = args.status == "complete"
        if strict:
            primary = checked_object(root, outputs[PARTIAL_OUTPUT[args.node]], PARTIAL_OUTPUT[args.node])
            if primary.get("pending_items") not in (None, []):
                raise ValueError("Complete attempt cannot retain unresolved pending_items")
        pending_items = pending_item_checks(root, refs, args.node, node, state) if not strict else []
        stage_content_checks(root, state, refs, args.node, scope, strict)
        coverage_types(root, state, refs, args.node, strict)
        volume_checks(root, state, refs, args.node, strict)
        consumed_sources = consumed_source_bindings(root, refs, args.node)
        if args.node == "text_bar" and args.status in {"complete", "partial"}:
            source = checked_object(root, inputs["material_task_scopes"], "material_task_scopes")
            for index, path in enumerate(sorted(source.get("source_files", {}))):
                inputs[f"material_source_{index}"] = path
        if args.node == "semantic_types" and strict:
            demand = checked_object(root, inputs["demand_universe"], "demand_universe")
            inputs["raw_query_source"] = demand["raw_query_source"]["path"]
        if args.node == "text_bar" and strict:
            text_data = checked_object(root, outputs["text_register"], "text_register")
            for index, path in enumerate(sorted(text_data["files"])):
                inputs[f"bar_artifact_{index}"] = path
        if args.node == "block_gap" and strict:
            block_data = checked_object(root, outputs["block_register"], "block_register")
            for index, item in enumerate(sorted(block_data["evidence_index"].values(), key=lambda x: x["path"])):
                inputs[f"block_evidence_{index}"] = item["path"]
        if args.node == "gap" and strict:
            link_data = checked_object(root, inputs["link_snapshot"], "link_snapshot")
            for index, path in enumerate(sorted(link_data["links_assessment"]["evidence_files"])):
                inputs[f"link_artifact_{index}"] = path
        if args.node == "audit":
            for index, path in enumerate(audit_artifact_refs(root, inputs["audit_sources"])):
                inputs[f"audit_artifact_{index}"] = path
        if args.node == "decision_map":
            decision_data = checked_object(root, outputs["decision_map"], "decision_map")
            for index, path in enumerate(measurement_receipt_checks(root, decision_data["measurement_plan"])):
                inputs[f"measurement_artifact_{index}"] = path
        if args.node == "proposal" and strict:
            packet_data = checked_object(root, outputs["client_packet"], "client_packet")
            receipt_data = checked_object(root, packet_data["packet_receipt_ref"], "packet-receipt")
            manifest_data = checked_object(root, packet_data["packet_manifest_ref"], "packet manifest")
            inputs["packet_receipt"] = packet_data["packet_receipt_ref"]
            inputs["packet_manifest"] = packet_data["packet_manifest_ref"]
            for index, name in enumerate(sorted(manifest_data["files"])):
                inputs[f"packet_file_{index}"] = str(Path(receipt_data["path"]) / name).replace("\\", "/")
            handoff_data = checked_object(root, str(Path(receipt_data["path"]) / "client-handoff.json"), "client handoff")
            origins = set()
            for claim in handoff_data.get("material_claims", []):
                if claim.get("status") not in {"observed", "estimated"}:
                    continue
                source_table = checked_object(root, str(Path(receipt_data["path"]) / claim["source_table_ref"]), "client numeric table")
                for row in source_table.get("rows", []):
                    if row.get("row_id") in claim.get("row_ids", []) and row.get("origin", {}).get("status") == "available":
                        origins.add(row["origin"]["source_ref"])
            for index, path in enumerate(sorted(origins)):
                inputs[f"numeric_primary_{index}"] = path
        if args.status == "complete":
            review_binding(root, state, refs, args.node)
            if args.node == "release_gate":
                predecessors = [n for n in order if n != "release_gate" and active(nodes[n], scope)]
                if any(states[n]["state"] != "complete" for n in predecessors):
                    raise ValueError("Complete release requires full dependency closure and coverage")
                release = checked_object(root, outputs["release_receipt"], "release_receipt")
                commercial = previous_output(root, state, "estimate", "estimate_sheet")["commercial_status"]
                if release.get("content_scope_complete") is not True or release.get("commercial_status") != commercial:
                    raise ValueError("Release receipt must match content and commercial status")
                acceptance(root, release)
                if isinstance(release.get("user_acceptance"), dict):
                    inputs["user_acceptance_source"] = release["user_acceptance"]["source_ref"]
    bound = {kind: {k: {"path": p, "sha256": sha_file(relative_file(root, p))} for k, p in entries.items()} for kind, entries in refs.items()}
    deps = {p: dependency_signature(root, state, p, args.node, states[p]["signature"])
            for p in required_parents(node, nodes, scope)}
    candidate = {"run_id": state["run_id"], "node": args.node,
                 "contract_version": raw["contract_version"],
                 "node_contract_sha256": sha_bytes(canonical(node)),
                 "shared_policy_sha256": policy_sha(raw),
                 "compute_sha256": compute_sha(args.node),
                 "scope_sha256": scope_fingerprint(args.node, scope),
                 "dependencies": deps, "inputs": bound["inputs"], "outputs": bound["outputs"],
                 "status": args.status, "checks": sorted(checks), "actor": args.actor,
                 "reason": args.reason or "", "blockers": args.blocker}
    if args.status in {"complete", "partial"}:
        candidate["consumed_sources"] = consumed_sources
        candidate["validation"] = {
            "known_scope_valid": True,
            "coverage": "complete" if args.status == "complete" else "partial",
            "availability": {"state": "available" if not pending_items else "mixed_or_unavailable",
                             "pending_ids": [item["pending_id"] for item in pending_items if item["state"] == "pending"],
                             "unknown_ids": [item["pending_id"] for item in pending_items if item["state"] == "unknown"],
                             "unavailable_ids": [item["pending_id"] for item in pending_items if item["state"] == "unavailable"]},
            "completion": args.status,
            "pending_items": pending_items,
        }
    if args.status == "skipped":
        candidate["scope_exception_verification"] = "unverified_user_origin"
    candidate["signature"] = sha_bytes(canonical(candidate))
    old = state["attempts"].get(args.node, [])
    if old and old[-1]["signature"] == candidate["signature"] and args.node not in state.get("invalidated", {}):
        return {"node": args.node, "state": old[-1]["status"], "reused": True, "signature": candidate["signature"]}
    candidate.update(attempt=len(old) + 1, recorded_at=stamp())
    state["attempts"].setdefault(args.node, []).append(candidate)
    state.setdefault("invalidated", {}).pop(args.node, None)
    write_json(root / STATE_NAME, state)
    return {"node": args.node, "state": args.status, "attempt": candidate["attempt"], "signature": candidate["signature"]}


def descendants(seed, nodes):
    found = {seed}
    changed = True
    while changed:
        size = len(found)
        found.update(n for n, node in nodes.items() if set(node["depends_on"]) & found)
        changed = len(found) != size
    return found


def structure_evidence_refs(root, state, decision):
    """Close permitted typed proof relationships from the current decision."""
    refs = {}
    pins = {}
    validated_paths = {}
    file_facts = {}
    origin_sets = {}
    # A revised decision must not inherit pins from an obsolete proposal or
    # review. Only the decision and its transitive input stages own its proof
    # boundary; conflicting pins within that boundary still fail closed.
    _, nodes, _ = contract()
    upstream = {"decision_map"}
    pending = ["decision_map"]
    while pending:
        for parent in nodes[pending.pop()]["depends_on"]:
            if parent not in upstream:
                upstream.add(parent)
                pending.append(parent)
    for node_id in sorted(upstream):
        attempts = state.get("attempts", {}).get(node_id, [])
        if not attempts:
            continue
        attempt = attempts[-1]
        for entry in [*attempt.get("inputs", {}).values(), *attempt.get("outputs", {}).values(),
                      *attempt.get("consumed_sources", [])]:
            key = entry["path"].replace("\\", "/")
            pins.setdefault(key, set()).add(entry["sha256"])
    def add(ref, origin, expected_sha=None):
        if not isinstance(ref, str) or not ref or "://" in ref or ref.startswith("#"):
            return
        first = ref.replace("\\", "/").split("/", 1)[0]
        if ":" in first and not Path(ref).drive:
            return  # Logical evidence ID, not a file claim.
        if not ("/" in ref or "\\" in ref or Path(ref).suffix or ref.startswith(".") or ref in pins):
            return
        path = Path(ref.replace("\\", "/"))
        if path.is_absolute() or path.drive or any(part in {"..", "."} or ":" in part for part in path.parts):
            raise ValueError("Unpermitted structure evidence path: " + ref)
        lowered = {part.lower() for part in path.parts}
        if (lowered & {"history", "run-history", "self-review", "self_review", "secrets", ".secrets", "tokens", "credentials", ".git", ".offerist", "private", "qa", "deliverables", "client-packet", "structure-packets"}
                or any(part.startswith(".env") for part in lowered)):
            raise ValueError("Unpermitted structure evidence source: " + ref)
        spelling = path.as_posix()
        if spelling not in validated_paths:
            target = root / path
            if any(item.is_symlink() for item in (target, *target.parents) if item != root and item.is_relative_to(root)):
                raise ValueError("Symlink in structure evidence path: " + ref)
            validated_paths[spelling] = relative_file(root, spelling)
        target = validated_paths[spelling]
        key = target.relative_to(root).as_posix()
        if key not in file_facts:
            file_facts[key] = (sha_file(target), target.stat().st_size)
        digest, size = file_facts[key]
        recorded = pins.get(key)
        if recorded is not None and recorded != {digest}:
            raise ValueError("Recorded structure evidence changed or has conflicting pins: " + ref)
        if expected_sha is not None and expected_sha != digest:
            raise ValueError("Structure evidence SHA mismatch: " + ref)
        if key not in refs:
            refs[key] = {"path": key, "bytes": size, "sha256": digest, "origins": []}
            origin_sets[key] = set()
        entry = refs[key]
        if origin not in origin_sets[key]:
            entry["origins"].append(origin)
            origin_sets[key].add(origin)

    add(state["attempts"]["decision_map"][-1]["outputs"]["decision_map"]["path"],
        "recorded_decision_map")
    # These buyer-facing dependencies are mandatory parts of the decision, even
    # if the author omitted them from its free-form evidence_refs list.
    for node, output in (("team", "team_needs"), ("estimate", "estimate_sheet"),
                         ("company_cases", "company_case_pack")):
        attempts = state.get("attempts", {}).get(node, [])
        if attempts:
            entry = attempts[-1].get("outputs", {}).get(output)
            if entry:
                add(entry["path"], "recorded_" + output, entry["sha256"])
    outline = decision.get("structure_outline") or {}
    add(outline.get("section_briefs_ref"), "decision_map.structure_outline.section_briefs_ref",
        outline.get("section_briefs_sha256"))
    if not isinstance(decision.get("evidence_refs", []), list):
        raise ValueError("Decision-map evidence refs must be a list")
    for index, ref in enumerate(decision.get("evidence_refs", [])):
        add(ref, f"decision_map.evidence_refs[{index}]")
    for index, row in enumerate(decision.get("decisions", [])):
        for key in ("a_evidence_ref", "gap_evidence_ref"):
            add(row.get(key), f"decision_map.decisions[{index}].{key}")
    dispositions = decision.get("source_dispositions", {})
    if not isinstance(dispositions, dict):
        raise ValueError("Decision-map source dispositions must be a map")
    for source_id, row in dispositions.items():
        if not isinstance(row, dict):
            raise ValueError("Decision-map source disposition must be an object")
        add(row.get("source_ref"), f"decision_map.source_dispositions.{source_id}.source_ref")
        for ref in row.get("source_refs", []):
            add(ref, f"decision_map.source_dispositions.{source_id}.source_refs")
        for ref in row.get("evidence_refs", []):
            add(ref, f"decision_map.source_dispositions.{source_id}")
    bar_rows = decision.get("bar_readings", {})
    if not isinstance(bar_rows, (dict, list)):
        raise ValueError("Decision-map BAR readings must be rows")
    bar_items = bar_rows.items() if isinstance(bar_rows, dict) else enumerate(bar_rows)
    for scope_id, row in bar_items:
        if not isinstance(row, dict):
            raise ValueError("Decision-map BAR reading must be an object")
        pointer = (row.get("reading") or {}).get("source_pointer") or {}
        add(pointer.get("file_ref"),
            f"decision_map.bar_readings.{scope_id}.source_pointer")
        for ref in (row.get("strategy_disposition") or {}).get("evidence_refs", []):
            add(ref, f"decision_map.bar_readings.{scope_id}.strategy_disposition")
    for row in (decision.get("structure_outline") or {}).get("seo_state_rows", []):
        add(row.get("source_ref"), f"decision_map.seo_state_rows.{row.get('finding_id')}")
    for ref in measurement_receipt_checks(root, decision["measurement_plan"]):
        add(ref, "decision_map.measurement_plan")
    gap = previous_output(root, state, "gap", "site_gap_report")
    link_files = (gap.get("links_assessment") or {}).get("evidence_files", {})
    if not isinstance(link_files, dict):
        raise ValueError("Named link evidence files need path-to-SHA map")
    for ref, digest in link_files.items():
        add(ref, "site_gap_report.links_assessment.evidence_files", digest)
    visited = set()
    while set(refs) - visited:
        key = sorted(set(refs) - visited)[0]
        visited.add(key)
        if Path(key).suffix.lower() != ".json":
            continue
        document = read_json(relative_file(root, key))
        for ref, digest, origin in typed_evidence_refs(document):
            before = set(refs)
            add(ref, key + origin, digest)
            for added in set(refs) - before:
                if added not in pins and digest is None:
                    raise ValueError("Nested structure evidence needs a recorded source SHA: " + added)
    return [refs[key] for key in sorted(refs)]


def client_material_refs(root, state, decision):
    """Keep proof closure without sending author planning to the final reviewer."""
    planning = {state["attempts"]["decision_map"][-1]["outputs"]["decision_map"]["path"],
                (decision.get("structure_outline") or {}).get("section_briefs_ref")}
    planning = {str(Path(ref.replace("\\", "/"))).replace("\\", "/")
                for ref in planning if isinstance(ref, str)}
    return [row for row in structure_evidence_refs(root, state, decision) if row["path"] not in planning]


def structure_export(root, state, scope, states, out):
    if states["decision_map"]["state"] not in {"complete", "partial"}:
        raise ValueError("Structure export needs a current recorded decision map")
    candidate = state["attempts"]["decision_map"][-1]["outputs"]["decision_map"]
    decision = checked_object(root, candidate["path"], "decision_map")
    if candidate["sha256"] != sha_file(relative_file(root, candidate["path"])):
        raise ValueError("Recorded decision-map candidate changed")
    refs = structure_evidence_refs(root, state, decision)
    out_path = root / out
    if (not out or Path(out).is_absolute() or ".." in Path(out).parts
            or not out_path.resolve().is_relative_to(root) or out_path.exists()):
        raise ValueError("Structure export target must be a new run-relative directory")
    out_path.mkdir(parents=True)
    try:
        for entry in refs:
            target = out_path / "packet" / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / entry["path"], target)
            if sha_file(target) != entry["sha256"]:
                raise ValueError("Structure evidence changed during copy: " + entry["path"])
        write_json(out_path / "evidence-index.json", {"files": refs})
        manifest = {"schema_version": 1, "candidate_sha256": candidate["sha256"],
                    "candidate_path": candidate["path"], "workflow_state": states["decision_map"]["state"],
                    "handoff_files": refs}
        write_json(out_path / "manifest.json", manifest)
    except Exception:
        shutil.rmtree(out_path)
        raise
    return {"path": out, "candidate_sha256": candidate["sha256"],
            "manifest_sha256": sha_file(out_path / "manifest.json"), "handoff_files": len(refs)}


def command(args):
    raw, nodes, order = contract()
    if args.action == "init":
        root = Path(args.run).resolve()
        root.mkdir(parents=True, exist_ok=True)
        if (root / STATE_NAME).exists():
            raise ValueError("Workflow already initialized")
        scope = read_json(relative_file(root, args.scope))
        if scope.get("geo") not in {"yes", "no", "undecided"}:
            raise ValueError("Scope needs explicit geo yes/no/undecided")
        state = {"schema_version": 1, "run_id": args.run_id, "created_at": stamp(),
                 "scope_ref": args.scope, "attempts": {}, "invalidated": {}}
        write_json(root / STATE_NAME, state)
        return {"run_id": args.run_id, "nodes": len(nodes), "contract_sha256": sha_file(CONTRACT)}
    if args.action == "record":
        manifest = getattr(args, "manifest", None)
        if manifest:
            direct = ("node", "status", "actor", "input", "output", "check", "reason", "blocker")
            if any(getattr(args, name, None) for name in direct):
                raise ValueError("Use either --manifest or direct record declarations")
            return record(manifest_args(args.run, manifest))
        if not all(getattr(args, name, None) for name in ("node", "status", "actor")):
            raise ValueError("Direct record needs --node, --status, and --actor")
        return record(args)
    root, state = root_and_state(args.run)
    scope, states, _ = current(root, state, raw, nodes, order)
    if args.action == "structure-export":
        return structure_export(root, state, scope, states, args.out)
    if args.action == "scaffold":
        if args.node not in nodes:
            raise ValueError("Unknown node")
        if not active(nodes[args.node], scope):
            raise ValueError("Node is outside selected scope")
        path = root / args.out
        if not args.out or Path(args.out).is_absolute() or not path.resolve().is_relative_to(root):
            raise ValueError("Scaffold path must be run-relative")
        if path.exists():
            raise ValueError("Scaffold target already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        template = scaffold_template(nodes[args.node])
        with path.open("x", encoding="utf-8") as stream:
            json.dump(template, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        result = {"manifest": args.out, "plan": node_plan(args.node, nodes, scope, states, state),
                  "recordable": False}
        if args.node == "decision_map" and all(states[parent]["state"] in {"complete", "partial"}
                                               for parent in required_parents(nodes["decision_map"], nodes, scope)):
            result["release_contract_scaffold"] = release_contract_scaffold(root, state)
            result["scope_handoff_scaffold"] = scope_handoff_scaffold(root, state)
        if args.node == "gap" and states["research"]["state"] in {"complete", "partial"}:
            result["rank_handoff_scaffold"] = rank_handoff_scaffold(root, state)
        if args.node == "gap":
            result["raw_link_scope_scaffold"] = {"chosen_source": None,
                "primary_field_map_scope": None,
                "metadata_scope_binding": {"metadata_ref": None, "row_source_ref": None,
                    "source_row_ids": [], "pointer": None, "value": None},
                "target_grain_is_separate": True}
        return result
    if args.action == "next":
        actionable = [n for n in order if active(nodes[n], scope) and states[n]["state"] in
                      {"pending", "invalidated", "blocked", "partial"}]
        plans = [node_plan(n, nodes, scope, states, state) for n in actionable]
        return {"run_id": state["run_id"], "geo_decision": scope["geo"],
                "ready_now": [p for p in plans if not p["blocking_dependencies"]],
                "waiting": [p for p in plans if p["blocking_dependencies"]],
                "geo_decision_pending": scope["geo"] == "undecided"}
    if args.action == "invalidate":
        if args.node not in nodes:
            raise ValueError("Unknown node")
        affected = descendants(args.node, nodes)
        for node_id in affected:
            state["invalidated"][node_id] = {"root": args.node, "reason": args.reason, "at": stamp()}
        write_json(root / STATE_NAME, state)
        return {"root": args.node, "invalidated": [n for n in order if n in affected]}
    if args.action == "status":
        content, commercial, user, user_claim, reviews = readiness(root, state, states, nodes, order, scope)
        review_data = previous_output(root, state, "client_review", "client_verdict") if states["client_review"]["state"] == "complete" else {}
        blockers = ["review_independence_unverified"]
        if commercial == "estimate_pending_human":
            blockers.append("estimate_pending_human")
        if not content:
            blockers.append("content_scope_incomplete")
        return {"run_id": state["run_id"], "content_scope_complete": content,
                "geo_decision": scope["geo"],
                "commercial_status": commercial, "user_acceptance": user,
                "user_acceptance_claim": user_claim,
                "review_provenance": reviews,
                "review_independence_verified": False,
                "release_readiness": False, "release_blockers": blockers,
                "content_verdict": review_data.get("content_verdict", "unknown"),
                "purchase_verdict": review_data.get("verdict", "unknown"),
                "scope_exceptions_unverified": [n for n in order if states[n]["state"] == "skipped"],
                "nodes": {n: states[n]["state"] for n in order},
                "known_scope_valid": {n: states[n].get("validation", {}).get("known_scope_valid", False)
                                      for n in order},
                "validation": {n: states[n]["validation"] for n in order if "validation" in states[n]},
                "reasons": {n: states[n]["reason"] for n in order if states[n]["state"] == "invalidated"}}
    if args.action == "resume":
        ready = []
        for n in order:
            if states[n]["state"] in {"pending", "invalidated", "blocked"} and active(nodes[n], scope):
                if all(states[p]["state"] in {"complete", "partial", "skipped"} for p in required_parents(nodes[n], nodes, scope)):
                    ready.append(n)
        content, commercial, user, user_claim, reviews = readiness(root, state, states, nodes, order, scope)
        remaining = [n for n in order if active(nodes[n], scope) and states[n]["state"] not in {"complete", "partial", "skipped"}]
        if scope["geo"] == "undecided":
            remaining.append("geo")
        return {"ready_now": ready, "remaining": remaining,
                "geo_decision": scope["geo"],
                "content_scope_complete": content, "commercial_status": commercial, "user_acceptance": user,
                "user_acceptance_claim": user_claim, "review_provenance": reviews,
                "review_independence_verified": False}
    raise ValueError("Unknown action")


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init")
    init.add_argument("--run", required=True)
    init.add_argument("--run-id", required=True)
    init.add_argument("--scope", required=True, help="Run-relative JSON with geo yes/no/undecided")
    rec = sub.add_parser("record")
    rec.add_argument("--run", required=True)
    rec.add_argument("--manifest", help="Run-relative JSON declaration; checked exactly like direct record")
    rec.add_argument("--node")
    rec.add_argument("--status")
    rec.add_argument("--actor")
    rec.add_argument("--input", action="append", default=[])
    rec.add_argument("--output", action="append", default=[])
    rec.add_argument("--check", action="append", default=[])
    rec.add_argument("--reason")
    rec.add_argument("--blocker", action="append", default=[])
    for name in ("status", "resume", "next"):
        sub.add_parser(name).add_argument("--run", required=True)
    scaffold = sub.add_parser("scaffold")
    scaffold.add_argument("--run", required=True)
    scaffold.add_argument("--node", required=True)
    scaffold.add_argument("--out", required=True, help="New run-relative manifest path")
    inv = sub.add_parser("invalidate")
    inv.add_argument("--run", required=True)
    inv.add_argument("--node", required=True)
    inv.add_argument("--reason", required=True)
    export = sub.add_parser("structure-export", help="Export only typed proof for the current decision map")
    export.add_argument("--run", required=True)
    export.add_argument("--out", required=True, help="New run-relative directory")
    return p


def main(argv=None):
    try:
        result = command(parser().parse_args(argv))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, KeyError, json.JSONDecodeError) as error:
        payload = {"error": str(error)}
        if isinstance(error, BARReplayFailure):
            payload["details"] = error.details
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
