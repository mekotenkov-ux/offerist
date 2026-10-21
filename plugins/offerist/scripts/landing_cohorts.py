"""Group landing URLs by engine and structural type using a declared working heuristic."""
import argparse
from collections import Counter
import csv
import importlib.util
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

SPEC = importlib.util.spec_from_file_location("page_type_metrics", Path(__file__).with_name("page_type_metrics.py"))
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)
FIELDS = ("url", "engine", "top5", "top10", "top50", "source", "region", "period")
PRIORITY_FIELDS = ("business_priority", "priority_source", "commercial_cluster")
DECISIONS = {"in_work", "protect", "specific_check", "defer"}


def normalized_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use absolute HTTP(S) landing URLs without embedded credentials")
    return metrics.identity(value)


def analyze(inventory, rankings):
    types, priorities, observations, contexts = {}, {}, {}, {}
    for row in inventory:
        if row.get("page_type") not in metrics.TYPES or not row.get("url"):
            raise ValueError("Inventory needs URL and one of the seven structural page types")
        url = normalized_url(row["url"])
        if url in types and types[url] != row["page_type"]:
            raise ValueError("Conflicting structural types for normalized URL: " + url)
        types[url] = row["page_type"]
        priority = {name: str(row.get(name) or "").strip() for name in PRIORITY_FIELDS}
        if priority["business_priority"] not in {"", "P0", "P1", "P2"}:
            raise ValueError("Business priority must be P0, P1, P2 or unknown (empty)")
        if (priority["business_priority"] or priority["commercial_cluster"]) and not priority["priority_source"]:
            raise ValueError("Business priority/cluster needs its source, not an inferred traffic score")
        if url in priorities and priorities[url] != priority:
            raise ValueError("Conflicting business priorities for normalized URL: " + url)
        priorities[url] = priority
    for raw in rankings:
        if any(raw.get(name) is None or not str(raw[name]).strip() for name in FIELDS):
            raise ValueError("Every ranking needs URL, engine, top5/top10/top50, source, region and period")
        row = {name: str(raw[name]).strip() for name in FIELDS}
        url, engine = normalized_url(row["url"]), row["engine"].lower()
        if (url, engine) in observations:
            raise ValueError("Rankings need one row per normalized URL/engine")
        if any(not re.fullmatch(r"[0-9]+", row[name]) for name in ("top5", "top10", "top50")):
            raise ValueError("Ranking counts must be nonnegative integers")
        t5, t10, t50 = (int(row[name]) for name in ("top5", "top10", "top50"))
        if not 0 <= t5 <= t10 <= t50:
            raise ValueError("Ranking counts must satisfy 0 <= top5 <= top10 <= top50")
        context = tuple(row[name] for name in ("source", "region", "period"))
        if engine in contexts and contexts[engine] != context:
            raise ValueError("Use one source/region/period per engine; analyze snapshots separately")
        contexts[engine] = context
        observations[url, engine] = (t5, t10, t50)
    if not contexts:
        raise ValueError("At least one ranking row is needed to identify the engines and source context")
    detail, counts, coverage = [], Counter(), []
    for engine, (source, region, period) in sorted(contexts.items()):
        urls = set(types) | {url for url, candidate in observations if candidate == engine}
        observed, missing, zero, outside = 0, 0, 0, 0
        for url in sorted(urls):
            values = observations.get((url, engine))
            t5, t10, t50 = values if values is not None else (None, None, None)
            cohort = "no_rank_data"
            if t50 is not None and 0 < t50 < 3:
                cohort = "insufficient_data"
            elif t50:
                cohort = "strong" if t5 * 2 > t50 else "quick_win" if t10 * 2 > t50 else "substantial_candidate"
            kind = types.get(url, "unknown")
            detail.append({"url": url, "engine": engine, "page_type": kind, "cohort": cohort,
                "top5": t5, "top10": t10, "top50": t50,
                "top6_10": t10 - t5 if values is not None else None,
                "top11_50": t50 - t10 if values is not None else None,
                "low_sample": t50 < 3 if t50 is not None else None,
                "rank_data": "unobserved" if values is None else "zero_in_source" if t50 == 0 else "observed_positive",
                "in_inventory": url in types, "source": source, "region": region, "period": period,
                **priorities.get(url, dict.fromkeys(PRIORITY_FIELDS, ""))})
            counts[engine, kind, cohort] += 1
            observed += values is not None
            missing += values is None
            zero += t50 == 0
            outside += url not in types
        coverage.append({"engine": engine, "source": source, "region": region, "period": period,
            "inventory_urls": len(types), "observed_urls": observed, "unobserved_urls": missing,
            "zero_in_source_urls": zero, "source_only_urls": outside, "total_urls": len(urls),
            "keyword_coverage": "unknown; source sample is not the complete search demand"})
    summary = {"method": "working heuristic, not a reference requirement or a verified scope of work",
        "rules": {"strong": "top50 >= 3 and top5/top50 > 0.5",
                  "quick_win": "otherwise top50 >= 3 and top10/top50 > 0.5",
                  "substantial_candidate": "otherwise top50 >= 3",
                  "insufficient_data": "0 < top50 < 3; preserve position bands without a strength classification",
                  "no_rank_data": "top50 = 0 or URL unobserved in this engine",
                  "low_sample": "observed top50 < 3; unobserved is unknown",
                  "boundary": "exactly 50% falls into the next conservative cohort"},
        "limitations": ["Position distribution does not prove a defect, required rewrite or implementation scope.",
                        "No brand/frequency classification is inferred; engines and snapshots are kept separate."],
        "counts": [{"engine": e, "page_type": p, "cohort": c, "urls": n} for (e, p, c), n in sorted(counts.items())],
        "coverage": coverage}
    return detail, summary


def validate_program(detail, decisions, page_plan):
    """Join a short decision queue to measured states; never turn a cohort into a rewrite order."""
    if not decisions:
        raise ValueError("Program needs address decisions, not only a cohort matrix")

    def key(row):
        return (normalized_url(row["url"]), str(row["engine"]).strip().lower(),
                str(row["region"]).strip(), str(row["period"]).strip())

    observed = {key(row): row for row in detail}
    actions = {}
    for row in page_plan:
        action_id = str(row.get("action_id") or "").strip()
        if not action_id or action_id in actions:
            raise ValueError("Page plan needs unique nonempty action_id values")
        actions[action_id] = row
    if not any(row.get("business_priority") or row.get("commercial_cluster") for row in detail):
        raise ValueError("Declare sourced business priorities/clusters before validating a program")
    selected, queue = {}, []
    for raw in decisions:
        required = ("url", "engine", "region", "period", "decision", "rationale", "evidence")
        if any(not str(raw.get(name) or "").strip() for name in required):
            raise ValueError("Every decision needs URL/engine/region/period, decision, rationale and evidence")
        identity = key(raw)
        if identity not in observed:
            raise ValueError("Decision has no matching URL/engine/region/period observation: " + identity[0])
        if identity in selected:
            raise ValueError("Use one decision per URL/engine/region/period")
        decision = str(raw["decision"]).strip()
        if decision not in DECISIONS:
            raise ValueError("Decision must be in_work, protect, specific_check or defer")
        action_id = str(raw.get("action_id") or "").strip()
        condition = str(raw.get("decision_condition") or "").strip()
        if decision in {"in_work", "specific_check"}:
            if action_id not in actions:
                raise ValueError("Work/check decision needs an existing page-plan action_id")
            action = actions[action_id]
            for field in ("deliverable", "acceptance", "first_batch", "calendar", "resource"):
                if not str(action.get(field) or "").strip():
                    raise ValueError("Linked action needs deliverable, acceptance, release, calendar and resource")
            if action.get("action") == "improve_page" and str(action.get("scope_count")) == "1":
                if normalized_url(str(action.get("target") or "")) != identity[0]:
                    raise ValueError("Single-page action target must match its decision URL")
            if decision == "specific_check" and (action.get("action") != "research" or not condition):
                raise ValueError("Specific check needs a research action and a concrete decision_condition")
        elif decision == "defer" and not condition:
            raise ValueError("Deferral needs a concrete reconsideration condition")
        selected[identity] = decision
        queue.append({**observed[identity], "decision": decision, "rationale": raw["rationale"],
                      "evidence": raw["evidence"], "action_id": action_id, "decision_condition": condition})
    # All P0 and business-priority quick wins need an address decision; other material clusters use representatives.
    required_clusters, covered_clusters, missing = set(), set(), []
    for identity, row in observed.items():
        priority, cohort, cluster = (row.get(name, "") for name in ("business_priority", "cohort", "commercial_cluster"))
        material = cohort in {"quick_win", "substantial_candidate"}
        address_required = priority == "P0" or (priority and cohort == "quick_win") or (priority and material and not cluster)
        if address_required and identity not in selected:
            missing.append(identity[0] + " [" + "/".join(identity[1:]) + "]")
        if cluster:
            group = (cluster, *identity[1:], cohort)
            required_clusters.add(group)
            if identity in selected:
                covered_clusters.add(group)
    if missing or required_clusters - covered_clusters:
        raise ValueError("Program omits priority URLs or material cluster/state decisions: "
                         + json.dumps({"urls": missing, "cluster_states": sorted(required_clusters - covered_clusters)}, ensure_ascii=False))
    order = {"quick_win": 0, "substantial_candidate": 1, "insufficient_data": 2, "no_rank_data": 3, "strong": 4}
    queue.sort(key=lambda row: ({"P0": 0, "P1": 1, "P2": 2}.get(row["business_priority"], 3),
                                order[row["cohort"]], row["commercial_cluster"], row["url"], row["engine"]))
    counts = Counter((row.get("business_priority") or "unknown", row["cohort"],
                      selected.get(identity, "not_selected")) for identity, row in observed.items())
    return queue, {"status": "structural checks passed; rationale, business coverage and evidence still require review",
                   "counts": [{"business_priority": p, "cohort": c, "decision": d, "url_engine_rows": n}
                              for (p, c, d), n in sorted(counts.items())]}


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path, rows):
    with path.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--rankings", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--decisions", type=Path, help="Short address decision queue; requires --page-plan")
    parser.add_argument("--page-plan", type=Path, help="Work actions linked by action_id; requires --decisions")
    args = parser.parse_args()
    try:
        if args.out.exists():
            raise ValueError("Output folder already exists; choose a NEW folder")
        if bool(args.decisions) != bool(args.page_plan):
            raise ValueError("Use --decisions and --page-plan together")
        detail, summary = analyze(read_csv(args.inventory), read_csv(args.rankings))
        program = validate_program(detail, read_csv(args.decisions), read_csv(args.page_plan)) if args.decisions else None
        args.out.mkdir(parents=True, exist_ok=False)
        write_csv(args.out / "landing-cohorts.csv", detail)
        write_csv(args.out / "summary.csv", summary["counts"])
        with (args.out / "summary.json").open("x", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
        if program:
            write_csv(args.out / "priority-program.csv", program[0])
            with (args.out / "program-check.json").open("x", encoding="utf-8") as handle:
                json.dump(program[1], handle, ensure_ascii=False, indent=2)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"out": str(args.out.resolve()), "url_engine_rows": len(detail)}))


if __name__ == "__main__":
    main()
