"""Offline, source-specific landing-page segmentation with explicit coverage.

No API access and no inferred URL intent. Feed additive counts, audited URL
classifications and the collection receipt; preserve unknowns and missingness.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path

GROUPS = ("informational", "commercial", "mixed", "other", "unknown")
METRICS = {"gsc": ("clicks", "impressions"), "metrika": ("visits", "goal_visits")}


def count(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or int(value) != value:
        raise ValueError("Additive counts must be nonnegative finite integers or null")
    return int(value)


def analyze(data):
    if not isinstance(data, dict):
        raise ValueError("Input must be a JSON object")
    source = data.get("source")
    if source not in METRICS:
        raise ValueError("source must be gsc or metrika; do not mix clicks and visits")
    cohort = data.get("cohort")
    if not isinstance(cohort, dict) or any(not cohort.get(k) for k in ("property", "filters", "timezone", "attribution")):
        raise ValueError("Explicit property, filters, timezone and attribution required")
    mapping = data.get("classifications", [])
    if not isinstance(mapping, list) or any(not isinstance(item, dict) for item in mapping):
        raise ValueError("classifications must be a list of objects")
    classes = {}
    for item in mapping:
        url, group = item.get("url"), item.get("group")
        if not isinstance(url, str) or not url or group not in GROUPS or not item.get("basis"):
            raise ValueError("Classification needs exact URL, valid group and inspected basis")
        if url in classes:
            raise ValueError("Overlapping/duplicate classification; use mixed or resolve intent")
        classes[url] = group
    periods = data.get("periods")
    if not isinstance(periods, list) or not periods:
        raise ValueError("At least one period required")
    metrics = METRICS[source]
    reports, period_ids, grain_fields = [], set(), None
    for period in periods:
        if not isinstance(period, dict):
            raise ValueError("Each period must be an object")
        ident = period.get("id")
        if not isinstance(ident, str) or not ident or ident in period_ids:
            raise ValueError("Distinct nonempty period IDs required")
        period_ids.add(ident)
        if period.get("cohort", cohort) != cohort:
            raise ValueError("Periods must share the same cohort, filters and attribution")
        coverage = period.get("coverage", {})
        if not isinstance(coverage, dict):
            raise ValueError("coverage must be an object")
        for flag in ("pagination_complete", "site_complete", "provider_limited", "sampled"):
            if not isinstance(coverage.get(flag), bool):
                raise ValueError("Coverage requires explicit boolean " + flag)
        rows = period.get("rows")
        if not isinstance(rows, list):
            raise ValueError("rows must be a list; no topN substitution")
        groups = {g: {"urls": set(), **{m: 0 for m in metrics}} for g in GROUPS}
        seen, missing = set(), False
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Each source row must be an object")
            foreign = set(METRICS["gsc" if source == "metrika" else "metrika"])
            if foreign.intersection(row):
                raise ValueError("Mixed source metrics; keep GSC clicks separate from Metrika visits")
            url = row.get("url")
            if not isinstance(url, str) or not url:
                raise ValueError("Every row requires an exact URL")
            dimensions = row.get("dimensions", {})
            if not isinstance(dimensions, dict):
                raise ValueError("dimensions must describe the source grain")
            fields = tuple(sorted(dimensions))
            if grain_fields is None:
                grain_fields = fields
            elif grain_fields != fields:
                raise ValueError("Mixed source grain; do not combine URL totals with URL x device rows")
            if any(not isinstance(k, str) or not isinstance(v, (str, int, float, bool, type(None))) for k, v in dimensions.items()):
                raise ValueError("Dimension values must be scalar source values")
            key = json.dumps([url, dimensions], sort_keys=True, ensure_ascii=False)
            if key in seen:
                raise ValueError("Duplicate source grain; URL x device rows must not be counted twice")
            seen.add(key)
            group = groups[classes.get(url, "unknown")]
            group["urls"].add(url)
            values = {m: count(row.get(m)) for m in metrics}
            if source == "gsc" and all(v is not None for v in values.values()) and values["clicks"] > values["impressions"]:
                raise ValueError("GSC clicks exceed impressions")
            if source == "metrika" and all(v is not None for v in values.values()) and values["goal_visits"] > values["visits"]:
                raise ValueError("Goal visits exceed visits; use goal visits, not number of events")
            for metric, value in values.items():
                if value is None:
                    missing = True
                    group[metric] = None
                elif group[metric] is not None:
                    group[metric] += value
        sums = {m: None if any(g[m] is None for g in groups.values()) else sum(g[m] for g in groups.values()) for m in metrics}
        totals = period.get("totals", {})
        if not isinstance(totals, dict):
            raise ValueError("totals must be an object")
        if set(METRICS["gsc" if source == "metrika" else "metrika"]).intersection(totals):
            raise ValueError("Mixed source metrics in totals; keep sources separate")
        totals = {m: count(totals.get(m)) for m in metrics}
        metric_reconciled = {m: totals[m] is not None and sums[m] == totals[m] for m in metrics}
        metric_reconciliation = {m: 'unavailable' if totals[m] is None or sums[m] is None
                                  else 'reconciled' if metric_reconciled[m] else 'mismatch' for m in metrics}
        reconciled = all(metric_reconciled.values())
        coverage_full = (coverage["pagination_complete"] and coverage["site_complete"] and not coverage["provider_limited"] and not coverage["sampled"])
        # GSC documents that the API exposes top rows, not guaranteed all rows.
        if source == "gsc":
            coverage_full = False
        metric_complete = {m: coverage_full and metric_reconciled[m] and sums[m] is not None for m in metrics}
        full = all(metric_complete.values())
        for group in groups.values():
            group["url_count"] = len(group.pop("urls"))
            numerator, denominator = ("clicks", "impressions") if source == "gsc" else ("goal_visits", "visits")
            a, b = group[numerator], group[denominator]
            group["ctr" if source == "gsc" else "goal_visit_rate"] = a / b if a is not None and b else None
            primary = "clicks" if source == "gsc" else "visits"
            group["site_share"] = group[primary] / totals[primary] if metric_complete[primary] and group[primary] is not None and totals[primary] else None
        reasons = []
        if not coverage["pagination_complete"]: reasons.append("collection_incomplete")
        if coverage["provider_limited"] or source == "gsc": reasons.append("provider_limited")
        if coverage["sampled"]: reasons.append("sampled")
        if missing: reasons.append("missing_metrics")
        # Missing goal data does not imply that observed visit rows fail to sum.
        if any(value == 'mismatch' for value in metric_reconciliation.values()):
            reasons.append("row_totals_not_reconciled")
        if any(value == 'unavailable' for value in metric_reconciliation.values()):
            reasons.append("metric_totals_unavailable")
        if not coverage["site_complete"]: reasons.append("site_coverage_unconfirmed")
        reports.append({"period": ident, "rows": len(rows), "unique_urls": len({r["url"] for r in rows}), "coverage": coverage,
                        "site_complete": full, "totals_reconciled": reconciled, "limitations": reasons,
                        "metric_reconciliation": metric_reconciliation,
                        "metric_complete": metric_complete,
                        "purpose_classification_complete": groups["unknown"]["url_count"] == 0,
                        "intent_complete": groups["unknown"]["url_count"] == 0,
                        "available_row_totals": sums, "source_totals": totals, "groups": groups})
    mapping_hash = hashlib.sha256(json.dumps(mapping, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    primary = "clicks" if source == "gsc" else "visits"
    comparisons = []
    for before, after in zip(reports, reports[1:]):
        comparable = before["site_complete"] and after["site_complete"]
        metric_comparable = {m: before["metric_complete"][m] and after["metric_complete"][m] for m in metrics}
        intent_comparable = before["intent_complete"] and after["intent_complete"]
        comparisons.append({"before": before["period"], "after": after["period"], "site_comparable": comparable,
                            "metric_comparable": metric_comparable,
                            "purpose_comparable": intent_comparable,
                            "intent_comparable": intent_comparable,
                            "changes": {g: {m: after["groups"][g][m] - before["groups"][g][m] if metric_comparable[m] and intent_comparable else None for m in metrics} for g in GROUPS}})
    return {"schema_version": 1, "source": source, "cohort": cohort, "classification_sha256": mapping_hash,
            "status": "complete" if all(r["site_complete"] and r["intent_complete"] for r in reports) else "partial", "periods": reports,
            "comparisons": comparisons, "primary_metric": primary,
            "field_definitions": {
                "purpose_classification_complete": "Every observed entrance URL has a reviewed landing-page purpose; visitor query intent is not measured here.",
                "intent_complete": "Legacy alias of purpose_classification_complete; does not establish visitor search intent.",
                "intent_comparable": "Legacy alias of purpose_comparable.",
                "totals_reconciled": "All declared metrics reconcile; consult metric_reconciliation for each available, missing or mismatched metric."},
            "definition": "Goal visits are visits with a configured goal, not CRM leads or sales; clicks are not visits. No missing URL row is imputed as zero."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = analyze(json.loads(args.input.read_text(encoding="utf-8-sig")))
        result["input_sha256"] = hashlib.sha256(args.input.read_bytes()).hexdigest()
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, allow_nan=False)
        print(json.dumps({"output": str(args.out.resolve()), "status": result["status"]}))
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(2, "Segmentation error: " + str(error) + "\n")


if __name__ == "__main__":
    main()
