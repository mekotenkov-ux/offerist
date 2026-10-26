#!/usr/bin/env python3
"""Aggregate a reviewed whole-site GAP input without inferring missing evidence.

Usage: python scripts/site_gap.py --input site-gap-input.json --out site-gap-report.json
The input contract is documented in offerist-gap/references/page-map.md.
"""

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median
from urllib.parse import urlsplit


PURPOSES = {"commercial", "informational", "info", "mixed", "other", "unknown"}
ACTIONS = {"keep", "optimize", "rework", "unknown"}
SCOPE_KEYS = ("definition", "source", "period", "engine", "region", "device", "segment", "rank_threshold")
LINK_KEYS = ("provider", "date", "scope")
RAW_LINK_METRICS = ("raw_referring_domains", "raw_backlinks")


def canonical_identity(url, client_domain):
    """Compare declared client canonical URLs without equating unrelated hosts."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("canonical identity needs a nonempty URL")
    if url.startswith("/") and not url.startswith("//"):
        parsed = urlsplit(url)
    else:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname != client_domain:
            raise ValueError("planned canonical URL must be on the client site")
    if parsed.fragment or not parsed.path.startswith("/"):
        raise ValueError("planned canonical URL needs a path without fragment")
    return parsed.path.rstrip("/") or "/", parsed.query


def require_nonnegative(value, label):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value)) or value < 0):
        raise ValueError(f"{label} must be a nonnegative number")
    return value


def require_nonnegative_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def require_positive(value, label):
    require_nonnegative(value, label)
    if value == 0:
        raise ValueError(f"{label} must be positive")
    return value


def raw_link_projection(links, assessment, competitors, client_domain):
    """Describe saved raw domain aggregates without treating them as live links."""
    evidence = assessment.get("evidence_files") or {}
    bindings = assessment.get("raw_metric_basis", [])
    primary_sources = assessment.get("raw_primary_sources", [])
    if not isinstance(bindings, list):
        raise ValueError("raw_metric_basis must be a list")
    if not isinstance(primary_sources, list):
        raise ValueError("raw_primary_sources must be a list")
    verified_rows = set()
    primary_scope_rows = set()
    for source in primary_sources:
        if not isinstance(source, dict) or source.get("status") not in {"verified", "unverified", "unsupported"}:
            raise ValueError("Raw primary source needs a declared verification status")
        if source["status"] == "verified":
            row_ref, ids = source.get("row_source_ref"), source.get("included_row_ids")
            if not isinstance(row_ref, str) or not row_ref or not isinstance(ids, list):
                raise ValueError("Verified raw primary source needs included row IDs")
            for row_id in ids:
                if not isinstance(row_id, str) or not row_id or (row_ref, row_id) in verified_rows:
                    raise ValueError("Ambiguous verified raw primary row")
                verified_rows.add((row_ref, row_id))
                if isinstance(source.get("field_map"), dict) and "scope" in source["field_map"]:
                    primary_scope_rows.add((row_ref, row_id))
    known_rows = {}
    for row in links:
        for metric in RAW_LINK_METRICS:
            if row.get(metric) is not None:
                require_nonnegative_int(row[metric], metric)
        if row.get("source_row_id") is not None:
            identity = (row.get("source_ref"), row["source_row_id"])
            if identity in known_rows:
                raise ValueError("Duplicate raw link source row ID")
            known_rows[identity] = row
    basis_by_row_metric = {}
    metadata_scope_rows = set()
    for binding in bindings:
        if not isinstance(binding, dict) or not isinstance(binding.get("fields"), dict):
            raise ValueError("Raw link metadata binding needs fields")
        fields = binding["fields"]
        required = {"provider", "date", "target_grain"}
        if not required.issubset(fields) or not any(metric in fields for metric in RAW_LINK_METRICS):
            raise ValueError("Raw link metadata needs provider, date, grain and a metric definition")
        if set(fields) - required - set(RAW_LINK_METRICS) - {"scope"}:
            raise ValueError("Unexpected raw link metadata field")
        if (not isinstance(binding.get("metadata_ref"), str) or not binding["metadata_ref"]
                or not isinstance(binding.get("metadata_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", binding["metadata_sha256"])):
            raise ValueError("Raw link metadata needs saved source reference and SHA")
        row_ref = binding.get("row_source_ref")
        row_ids = binding.get("source_row_ids")
        if (not isinstance(row_ref, str) or row_ref not in evidence or not isinstance(row_ids, list)
                or not row_ids or any(not isinstance(x, str) or not x for x in row_ids)
                or len(row_ids) != len(set(row_ids))):
            raise ValueError("Raw link metadata needs distinct saved row IDs")
        for role, field in fields.items():
            if (not isinstance(field, dict) or not isinstance(field.get("pointer"), str)
                    or not field["pointer"].startswith("/") or not isinstance(field.get("value"), str)
                    or not field["value"].strip()):
                raise ValueError("Raw link metadata field needs a nonempty pointer and value")
        for row_id in row_ids:
            row = known_rows.get((row_ref, row_id))
            if row is None:
                raise ValueError("Raw link metadata names an absent saved row")
            if row.get("provider") != fields["provider"]["value"] or row.get("date") != fields["date"]["value"]:
                raise ValueError("Raw link row provider/date differs from bound metadata")
            if "scope" in fields:
                if row.get("scope") != fields["scope"]["value"]:
                    raise ValueError("Raw link row scope differs from bound metadata")
                metadata_scope_rows.add((row_ref, row_id))
            for metric in RAW_LINK_METRICS:
                if metric in fields:
                    key = (row_ref, row_id, metric)
                    if key in basis_by_row_metric:
                        raise ValueError("Raw link row has ambiguous metadata definition")
                    basis_by_row_metric[key] = binding

    relevant = {peer["domain"] for peer in competitors if peer["relevant"] is True}
    groups = {}
    row_cohorts = {}
    observations = []
    for row in links:
        statuses = {}
        for metric in RAW_LINK_METRICS:
            value = row.get(metric)
            binding = basis_by_row_metric.get((row.get("source_ref"), row.get("source_row_id"), metric))
            if value is None:
                reason = "value_unknown"
            elif row.get("complete") is not True:
                reason = "aggregate_slice_incomplete"
            elif row.get("domain") != client_domain and row.get("domain") not in relevant:
                reason = "peer_not_relevant"
            elif (row.get("source_ref"), row.get("source_row_id")) not in verified_rows:
                reason = "primary_unverified"
            elif binding is None:
                reason = "definition_or_grain_unbound"
            elif (not isinstance(row.get("scope"), str) or not row["scope"].strip()
                    or (row.get("source_ref"), row.get("source_row_id")) not in primary_scope_rows | metadata_scope_rows):
                reason = "scope_unbound"
            elif assessment.get("status") not in {"available", "partial"}:
                reason = "source_not_available"
            else:
                reason = "eligible"
            statuses[metric] = reason
            if reason == "eligible":
                fields = binding["fields"]
                key = json.dumps([metric, row.get("provider"), row.get("date"), row.get("scope"),
                                  fields[metric]["value"], fields["target_grain"]["value"]],
                                 ensure_ascii=False, sort_keys=True)
                group = groups.setdefault(key, {"metric": metric, "provider": row.get("provider"),
                    "date": row.get("date"), "scope": row.get("scope"),
                    "definition": fields[metric]["value"], "target_grain": fields["target_grain"]["value"],
                    "rows": {}})
                if row.get("domain") in group["rows"]:
                    raise ValueError("Duplicate domain in one raw link metric cohort")
                group["rows"][row.get("domain")] = row
                row_cohorts[(id(row), metric)] = key
        observations.append({"domain": row.get("domain"), "provider": row.get("provider"),
            "date": row.get("date"), "scope": row.get("scope"), "complete": row.get("complete"),
            "source_ref": row.get("source_ref"), "source_row_id": row.get("source_row_id"),
            "raw_referring_domains": row.get("raw_referring_domains"), "raw_backlinks": row.get("raw_backlinks"),
            "metric_status": statuses})

    benchmarks = []
    for key in sorted(groups):
        group = groups[key]
        own = group["rows"].get(client_domain)
        peers = [row for domain, row in sorted(group["rows"].items()) if domain != client_domain]
        values = [row[group["metric"]] for row in peers]
        own_value = own[group["metric"]] if own else None
        # A lone unmatched peer remains visible, but cannot imply a cross-row comparison.
        comparable_values = values if own is not None or len(values) > 1 else []
        peer_mean = mean(comparable_values) if comparable_values else None
        peer_median = median(comparable_values) if comparable_values else None
        benchmarks.append({"basis": "saved_raw_domain_aggregate", "metric": group["metric"],
            "provider": group["provider"], "date": group["date"], "scope": group["scope"],
            "definition": group["definition"], "target_grain": group["target_grain"],
            "limitations": assessment.get("limitations"), "client_value": own_value,
            "client_source_ref": own.get("source_ref") if own else None,
            "client_source_row_id": own.get("source_row_id") if own else None,
            "peer_count": len(peers), "peer_domains": [row["domain"] for row in peers],
            "peer_values": [{"domain": row["domain"], "value": row[group["metric"]],
                             "source_ref": row.get("source_ref"), "source_row_id": row.get("source_row_id")}
                            for row in peers],
            "peer_mean": peer_mean, "peer_median": peer_median,
            "mean_difference_from_client": mean([value - own_value for value in comparable_values]) if comparable_values and own_value is not None else None,
            "median_difference_from_client": median([value - own_value for value in comparable_values]) if comparable_values and own_value is not None else None,
            "cohort_eligibility": "comparable_raw" if own and peers else "peer_only" if peers else "own_only",
            "reason": "single peer without own comparator" if own is None and len(peers) == 1 else None})

    pairs = {}
    for row in links:
        if row.get("domain") == client_domain:
            continue
        pair = {"peer_source_ref": row.get("source_ref"), "peer_source_row_id": row.get("source_row_id")}
        own_sources = []
        for metric, label in (("raw_referring_domains", "referring_domains"), ("raw_backlinks", "backlinks")):
            cohort = groups.get(row_cohorts.get((id(row), metric)))
            own = cohort["rows"].get(client_domain) if cohort else None
            peer_value = row.get(metric)
            own_value = own.get(metric) if own else None
            pair[f"client_{metric}"] = own_value
            pair[f"peer_{metric}"] = peer_value
            pair[f"{label}_difference"] = peer_value - own_value if peer_value is not None and own_value is not None else None
            if own:
                own_sources.append((own.get("source_ref"), own.get("source_row_id")))
        pair["cohort_match"] = bool(own_sources)
        pair["client_source_ref"] = own_sources[0][0] if own_sources and len(set(own_sources)) == 1 else None
        pair["client_source_row_id"] = own_sources[0][1] if own_sources and len(set(own_sources)) == 1 else None
        pairs[id(row)] = pair
    primary_complete = (all(isinstance(row.get("scope"), str) and row["scope"].strip()
        and (row.get("source_ref"), row.get("source_row_id")) in verified_rows & (primary_scope_rows | metadata_scope_rows)
        for row in links if any(row.get(metric) is not None for metric in RAW_LINK_METRICS))
        and all(source.get("status") == "verified" for source in primary_sources))
    return observations, benchmarks, pairs, primary_complete


def scope_key(scope):
    return tuple(scope.get(key) for key in SCOPE_KEYS)


def page_type(value):
    if value is None:
        return "unknown"
    if not isinstance(value, str) or not value.strip():
        raise ValueError("page_type must be a nonempty string")
    return value.strip().lower()


def weighted_rank(rows, source):
    weighted = weight = 0
    urls = set()
    censored = 0
    observation_ids = set()
    for row in rows:
        observation_id = row.get("observation_id")
        if observation_id is not None:
            if observation_id in observation_ids:
                raise ValueError(f"duplicate {source} observation_id: {observation_id}")
            observation_ids.add(observation_id)
        url = row.get("canonical_url")
        if not url:
            raise ValueError(f"{source} row needs canonical_url")
        position = row.get("position")
        row_weight = row.get("impressions") if source == "gsc" else row.get("weight", 1)
        if row_weight is not None:
            require_nonnegative(row_weight, f"{source} weight")
        if position is None:
            censored += 1
            continue
        require_positive(position, f"{source} position")
        if row_weight is None:
            raise ValueError(f"{source} row needs weight")
        if row_weight:
            weighted += position * row_weight
            weight += row_weight
            urls.add(url)
    require_nonnegative(weighted, f"{source} weighted rank total")
    require_nonnegative(weight, f"{source} weight total")
    average = weighted / weight if weight else None
    if average is not None:
        require_positive(average, f"{source} average position")
    return {
        "average_position": average,
        "weight_total": weight,
        "weight_rule": "impressions" if source == "gsc" else "declared weight; default 1 per observed SERP row",
        "distinct_observed_ranking_urls": len(urls),
        "censored_or_missing_rows": censored,
        "source": source,
    }


def rank_cohort_comparison(own_rows, peer_rows, own_scope, peer_scope, source):
    """Compare unique query pairs with identical weights in an aligned slice.

    Marginal means are descriptive even when a paired comparison is unknown.
    A separate weighted mean on each side is never itself a comparative delta.
    """
    result = {"status": "unknown_scope", "own_query_ids": [], "peer_query_ids": [],
              "matched_query_ids": [], "ours": None, "peer": None, "peer_minus_ours": None,
              "reason": None, "own_scope": own_scope, "peer_scope": peer_scope,
              "marginal_ours": weighted_rank(own_rows, source),
              "marginal_peer": weighted_rank(peer_rows, source)}

    def known(value):
        return (isinstance(value, str) and value.strip().lower() not in
                {"", "unknown", "unspecified", "n/a"})

    def known_threshold(value):
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value) and value > 0)

    def context_value(scope, key):
        if not isinstance(scope, dict):
            return None
        if key == "period":
            return scope.get("period") or scope.get("date")
        if key == "threshold":
            return scope["rank_threshold"] if "rank_threshold" in scope else scope.get("threshold")
        return scope.get(key)

    def observed(rows):
        weight_key = "impressions" if source == "gsc" else "weight"
        return [row for row in rows if row.get("position") is not None
                and row.get(weight_key, 1 if source == "serp" else 0) > 0]

    own_observed, peer_observed = observed(own_rows), observed(peer_rows)
    own_ids = sorted({row.get("query_id") for row in own_observed
                      if isinstance(row.get("query_id"), str) and row["query_id"].strip()})
    peer_ids = sorted({row.get("query_id") for row in peer_observed
                       if isinstance(row.get("query_id"), str) and row["query_id"].strip()})
    shared = sorted(set(own_ids) & set(peer_ids))
    result.update(own_query_ids=own_ids, peer_query_ids=peer_ids, matched_query_ids=shared)
    required = ("source", "engine", "region", "device", "period", "threshold")
    optional = ("definition", "method", "segment")
    for key in required + optional:
        own_value, peer_value = context_value(own_scope, key), context_value(peer_scope, key)
        if key in optional and own_value is None and peer_value is None:
            continue
        valid = known_threshold if key == "threshold" else known
        if not valid(own_value) or not valid(peer_value):
            result["reason"] = f"context_unknown:{key}"
            return result
        if (own_value != peer_value if key == "threshold" else own_value.strip() != peer_value.strip()):
            result["reason"] = f"context_mismatch:{key}"
            return result
    if not own_observed or not peer_observed:
        result["status"] = "unknown_no_observed_pair"
        result["reason"] = "no_observed_pair"
        return result
    if any(not isinstance(row.get("query_id"), str) or not row["query_id"].strip()
           for row in own_observed + peer_observed):
        result["status"] = "unknown_query_ids"
        result["reason"] = "missing_observed_query_id"
        return result
    if not shared:
        result["status"] = "unknown_no_matched_query_ids"
        result["reason"] = "no_exact_shared_query_id"
        return result
    pairs = []
    for query_id in shared:
        own_matches = [row for row in own_rows if row.get("query_id") == query_id]
        peer_matches = [row for row in peer_rows if row.get("query_id") == query_id]
        if len(own_matches) != 1 or len(peer_matches) != 1:
            result["status"] = "unknown_nonunique_query_grain"
            result["reason"] = f"nonunique_query_grain:{query_id}"
            return result
        own_row, peer_row = own_matches[0], peer_matches[0]
        own_weight = own_row.get("impressions") if source == "gsc" else own_row.get("weight", 1)
        peer_weight = peer_row.get("impressions") if source == "gsc" else peer_row.get("weight", 1)
        if own_weight != peer_weight:
            result["status"] = "unknown_weight_mismatch"
            result["reason"] = f"weight_mismatch:{query_id}"
            return result
        pairs.append((own_row, peer_row, own_weight))
    common_weight = sum(weight for _, _, weight in pairs)
    require_positive(common_weight, "paired rank weight total")
    own_mean = sum(own["position"] * weight for own, _, weight in pairs) / common_weight
    peer_mean = sum(peer["position"] * weight for _, peer, weight in pairs) / common_weight
    require_positive(own_mean, "paired own mean position")
    require_positive(peer_mean, "paired peer mean position")
    result.update(status="matched_query_cohort", matched_query_ids=shared,
                  reason="exact_unique_pairs_same_weights", ours=own_mean, peer=peer_mean,
                  peer_minus_ours=peer_mean - own_mean)
    return result


def external_comparison(demand, competitors, client_domain):
    """Check declared external bindings; source contents still require author review."""
    comparison = demand.get("comparison")
    if comparison is None:
        return {"mode": "internal_only", "external_basis_present": False,
                "source_slices_complete": False, "linked_cluster_ids": [], "reason": "external_comparison_not_reported"}
    if not isinstance(comparison, dict) or comparison.get("mode") not in {"search", "structure"}:
        raise ValueError("demand comparison needs mode search or structure")
    sources = demand.get("sources", [])
    if not isinstance(sources, list) or any(not isinstance(ref, str) or not ref.strip() for ref in sources):
        raise ValueError("demand sources must list nonempty saved source references")
    peers = comparison.get("peers", [])
    if not isinstance(peers, list):
        raise ValueError("demand comparison peers must be a list")
    normalize = lambda domain: domain.lower().rstrip(".").removeprefix("www.")
    relevant = {normalize(peer["domain"]) for peer in competitors if peer.get("relevant") is True}
    peer_sources, slices_complete = {}, bool(peers)
    for peer in peers:
        domain = peer.get("domain") if isinstance(peer, dict) else None
        if not isinstance(domain, str) or not domain.strip():
            raise ValueError("external peer needs a domain")
        domain = normalize(domain)
        if domain == normalize(client_domain) or domain not in relevant or domain in peer_sources:
            raise ValueError("external comparison needs distinct relevant competitors, not the client itself")
        refs = peer.get("source_refs", [])
        if (not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in sources for ref in refs)):
            raise ValueError("external peer source_refs must bind saved demand sources")
        peer_sources[domain] = set(refs)
        slices_complete = slices_complete and peer.get("complete") is True
    linked = []
    for cluster in demand.get("clusters", []):
        refs, urls = cluster.get("source_refs", []), cluster.get("peer_urls", [])
        if (not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in sources for ref in refs)
                or not isinstance(urls, list) or any(not isinstance(url, str) for url in urls)):
            raise ValueError("demand cluster needs saved source_refs and a list of peer URLs")
        for url in urls:
            parsed = urlsplit(url)
            domain = normalize(parsed.hostname or "")
            if (parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password
                    and domain in peer_sources and peer_sources[domain].intersection(refs)):
                linked.append(cluster["id"])
                break
    return {"mode": comparison["mode"], "external_basis_present": bool(linked),
            "source_slices_complete": slices_complete, "linked_cluster_ids": linked,
            "reason": "source_bound_external_comparison" if linked else "no_source_bound_peer_cluster"}


def summarize(data):
    inventory = data["inventory"]
    pages = inventory["pages"]
    complete = inventory.get("complete")
    if not isinstance(complete, bool):
        raise ValueError("inventory.complete must be boolean")
    inventory_scope = inventory["scope"]
    if not all(inventory_scope.get(key) is not None for key in SCOPE_KEYS[:7]):
        raise ValueError("inventory.scope needs definition/source/period/engine/region/device/segment")
    by_canonical = {}
    observed_identities = set()
    for page in pages:
        canonical = page.get("canonical_url")
        if not canonical:
            raise ValueError("every page needs canonical_url")
        if canonical in by_canonical:
            raise ValueError(f"duplicate canonical_url: {canonical}; reconcile before counting")
        identity = canonical_identity(canonical, data["client_domain"])
        if identity in observed_identities:
            raise ValueError("duplicate client canonical identity; reconcile URL forms before counting")
        observed_identities.add(identity)
        purpose, action = page.get("purpose"), page.get("action")
        if purpose not in PURPOSES or action not in ACTIONS:
            raise ValueError(f"invalid purpose/action for {canonical}")
        page_type(page.get("page_type"))
        by_canonical[canonical] = page

    purpose_counts = defaultdict(lambda: Counter({action: 0 for action in ACTIONS}))
    for page in pages:
        purpose_counts[page["purpose"]][page["action"]] += 1
    actions = Counter({action: 0 for action in ACTIONS})
    actions.update(page["action"] for page in pages)
    type_purpose = defaultdict(lambda: Counter())
    type_actions = defaultdict(lambda: Counter({action: 0 for action in ACTIONS}))
    for page in pages:
        kind = page_type(page.get("page_type"))
        type_purpose[kind][page["purpose"]] += 1
        type_actions[kind][page["action"]] += 1

    # Discovery completeness does not establish reviewed actions or demand coverage.
    assessed = len(pages) - actions["unknown"]
    empty_verified = inventory.get('empty_verified') is True and bool(inventory.get('empty_evidence'))
    action_complete = (bool(pages) or empty_verified) and assessed == len(pages)
    demand = data.get("demand_review", {})
    demand_complete_declared = demand.get("complete", False)
    if not isinstance(demand_complete_declared, bool):
        raise ValueError("demand_review.complete must be boolean")
    demand_clusters = demand.get("clusters", [])
    demand_ids = set()
    demand_counts = Counter()
    demand_statuses = {"covered", "create_candidate", "rejected", "needs_business_answer", "unknown"}
    for cluster in demand_clusters:
        ident = cluster.get("id")
        if not ident or ident in demand_ids:
            raise ValueError("demand cluster ids must be unique and nonempty")
        demand_ids.add(ident)
        status = cluster.get("status")
        if status not in demand_statuses:
            raise ValueError("invalid demand cluster status")
        if status != "unknown" and (not cluster.get("evidence") or not cluster.get("basis")):
            raise ValueError("reviewed demand clusters need evidence and basis")
        demand_counts[status] += 1
    comparison = external_comparison(demand, data.get("competitors", []), data["client_domain"])
    reviewed_demand = (bool(demand_clusters) and demand_complete_declared and bool(demand.get("scope"))
                       and comparison["external_basis_present"] and comparison["source_slices_complete"]
                       and not demand_counts["unknown"] and not demand_counts["needs_business_answer"])
    demand_complete = reviewed_demand and comparison["mode"] == "search"
    structural_complete = reviewed_demand and comparison["mode"] == "structure"

    missing = data.get("missing_intents", [])
    intent_ids = set()
    supported = []
    unresolved = []
    for intent in missing:
        ident = intent.get("id")
        if not ident or ident in intent_ids:
            raise ValueError("missing intent ids must be unique and nonempty")
        intent_ids.add(ident)
        kind = page_type(intent.get("page_type"))
        confirmed = bool(intent.get("evidence")) and all(intent.get(key) is True for key in ("supported", "existing_urls_checked", "independent"))
        (supported if confirmed else unresolved).append(ident)

    # Demand can discover a page type that does not exist on the current site.
    # Keep that type visible even before a business decision permits creation.
    demand_by_type = defaultdict(Counter)
    demand_cluster_ids_by_type = defaultdict(list)
    candidate_reconciliation = []
    unmapped_candidates = []
    for cluster in demand_clusters:
        kind = page_type(cluster.get('page_type'))
        if kind != 'unknown' or cluster['status'] in {'create_candidate', 'needs_business_answer', 'unknown'}:
            demand_by_type[kind][cluster['status']] += 1
            demand_cluster_ids_by_type[kind].append(cluster['id'])
        if cluster['status'] != 'create_candidate':
            continue
        linked = cluster.get('missing_intent_ids', [])
        if not isinstance(linked, list) or any(not isinstance(x, str) for x in linked) or len(set(linked)) != len(linked):
            raise ValueError('create candidate missing_intent_ids must be distinct string IDs')
        dangling = [ident for ident in linked if ident not in intent_ids]
        mismatched = [ident for ident in linked if ident in intent_ids and kind != 'unknown'
                      and page_type(next(x for x in missing if x['id'] == ident).get('page_type')) != kind]
        disposition = cluster.get('disposition', {})
        no_new_page = (isinstance(disposition, dict)
                       and disposition.get('decision') in {'covered', 'rejected', 'no_new_page', 'integrated_in_job'}
                       and bool(disposition.get('basis')) and bool(disposition.get('evidence')))
        reconciled = (bool(linked) and not dangling and not mismatched) or no_new_page
        if not reconciled:
            unmapped_candidates.append(cluster['id'])
        candidate_reconciliation.append({'cluster_id': cluster['id'], 'page_type': kind,
                                         'missing_intent_ids': linked, 'dangling_ids': dangling,
                                         'mismatched_type_ids': mismatched, 'reconciled': bool(reconciled),
                                         'no_new_page_disposition': bool(no_new_page)})

    competitors = []
    for peer in data.get("competitors", []):
        count = peer.get("count")
        if count is not None:
            require_nonnegative_int(count, "competitor count")
        relevant = peer.get("relevant") is True
        comparable = relevant and peer.get("complete") is True and complete and scope_key(peer.get("scope", {})) == scope_key(inventory_scope)
        delta = count - len(pages) if comparable and count is not None else None
        peer_types = peer.get("type_counts")
        if peer_types is None and peer.get("pages") is not None:
            peer_urls = set()
            peer_types = Counter()
            for peer_page in peer["pages"]:
                canonical = peer_page.get("canonical_url")
                if not canonical or canonical in peer_urls:
                    raise ValueError(f"competitor {peer['domain']} pages need unique canonical_url")
                peer_urls.add(canonical)
                peer_types[page_type(peer_page.get("page_type"))] += 1
        if peer_types is not None:
            if not isinstance(peer_types, dict):
                raise ValueError("competitor type_counts must be an object")
            peer_types = {page_type(kind): require_nonnegative_int(value, "competitor type count") for kind, value in peer_types.items()}
        type_comparable = comparable and peer.get("type_counts_complete") is True and peer_types is not None
        if type_comparable and count is not None and sum(peer_types.values()) != count:
            raise ValueError(f"competitor {peer['domain']} type_counts must sum to count")
        competitors.append({
            "domain": peer["domain"],
            "count": count,
            "comparable": bool(comparable),
            "relevant": peer.get("relevant"),
            "type_counts_comparable": bool(type_comparable),
            "type_counts": peer_types if type_comparable else None,
            "delta_pages": delta,
            "relative_delta": delta / len(pages) if delta is not None and pages else None,
            "scope": peer.get("scope"),
            "coverage": peer.get("coverage"),
        })

    ranks = data.get("rankings", {})
    gsc = weighted_rank(ranks.get("gsc", []), "gsc")
    serp = weighted_rank(ranks.get("serp", []), "serp")
    by_purpose_rank = {}
    for purpose in sorted(PURPOSES):
        group_urls = {url for url, page in by_canonical.items() if page["purpose"] == purpose}
        if group_urls:
            by_purpose_rank[purpose] = {
                "gsc": weighted_rank([row for row in ranks.get("gsc", []) if row["canonical_url"] in group_urls], "gsc"),
                "serp": weighted_rank([row for row in ranks.get("serp", []) if row["canonical_url"] in group_urls], "serp"),
            }
    def own_observed_type(row):
        canonical_page = by_canonical.get(row.get("canonical_url"), {})
        return page_type(row.get("observed_page_type", canonical_page.get("page_type")))

    observed_rank_types = {own_observed_type(row) for source in ("gsc", "serp") for row in ranks.get(source, [])}
    by_type_rank = {}
    for kind in sorted(set(type_actions) | observed_rank_types):
        by_type_rank[kind] = {
            "gsc": weighted_rank([row for row in ranks.get("gsc", []) if own_observed_type(row) == kind], "gsc"),
            "serp": weighted_rank([row for row in ranks.get("serp", []) if own_observed_type(row) == kind], "serp"),
        }
    rank_urls = {row["canonical_url"] for row in ranks.get("gsc", []) if row.get("position") is not None and row.get("impressions", 0) > 0}
    rank_urls |= {row["canonical_url"] for row in ranks.get("serp", []) if row.get("position") is not None and row.get("weight", 1) > 0}

    supported_intents_by_type = Counter(page_type(intent.get("page_type")) for intent in missing if intent["id"] in supported and page_type(intent.get("page_type")) != "unknown")
    untyped_supported = [intent["id"] for intent in missing if intent["id"] in supported and page_type(intent.get("page_type")) == "unknown"]
    unresolved_by_type = Counter(page_type(intent.get("page_type")) for intent in missing if intent["id"] in unresolved)
    kinds = set(type_actions) | set(supported_intents_by_type) | set(unresolved_by_type) | set(demand_by_type) | observed_rank_types
    raw_jobs = data.get("landing_jobs")
    approved_jobs_by_type = Counter()
    approved_job_ids = []
    job_decisions_resolved = False
    if raw_jobs is not None:
        if not isinstance(raw_jobs, list):
            raise ValueError("landing_jobs must be an explicit list or absent while decisions are pending")
        seen_job_ids, assigned_clusters, assigned_intents = set(), set(), set()
        candidates_by_id = {row["id"]: row for row in demand_clusters if row["status"] == "create_candidate"}
        candidate_ids = set(candidates_by_id)
        for job in raw_jobs:
            if not isinstance(job, dict) or not isinstance(job.get("landing_job_id"), str) or not job["landing_job_id"].strip() or job["landing_job_id"] in seen_job_ids:
                raise ValueError("landing job decision needs a unique nonempty ID")
            seen_job_ids.add(job["landing_job_id"])
            cluster_ids = job.get("cluster_ids")
            intent_ids_for_job = job.get("missing_intent_ids")
            if not isinstance(cluster_ids, list) or not cluster_ids or len(cluster_ids) != len(set(cluster_ids)) or not isinstance(intent_ids_for_job, list) or len(intent_ids_for_job) != len(set(intent_ids_for_job)):
                raise ValueError("landing job needs distinct source cluster and intent IDs")
            if assigned_clusters.intersection(cluster_ids) or assigned_intents.intersection(intent_ids_for_job):
                raise ValueError("source cluster or missing intent is allocated to multiple landing jobs")
            if not set(cluster_ids) <= candidate_ids:
                raise ValueError("landing job references an unsourced create candidate")
            linked_intents = {intent_id for cluster_id in cluster_ids for intent_id in candidates_by_id[cluster_id].get("missing_intent_ids", [])}
            if set(intent_ids_for_job) != linked_intents or not linked_intents <= set(supported):
                raise ValueError("landing job intents differ from supported source cluster links")
            task_types = {page_type(candidates_by_id[cluster_id].get("page_type")) for cluster_id in cluster_ids}
            if set(job.get("task_type_ids", [])) != task_types or "unknown" in task_types or not job.get("evidence_refs"):
                raise ValueError("landing job task types or evidence differ from source clusters")
            assigned_clusters.update(cluster_ids)
            assigned_intents.update(intent_ids_for_job)
            if job.get("disposition") == "create":
                published_type = page_type(job.get("published_type_id"))
                if published_type not in task_types or not job.get("action_id"):
                    raise ValueError("approved landing job needs published page type and action")
                for cluster_id in cluster_ids:
                    if page_type(candidates_by_id[cluster_id].get("page_type")) == published_type:
                        disposition = candidates_by_id[cluster_id].get("disposition")
                        if disposition is not None and (not isinstance(disposition, dict) or disposition.get("decision") != "create"):
                            raise ValueError("approved page cannot override a noncreate source disposition")
                        continue
                    disposition = candidates_by_id[cluster_id].get("disposition", {})
                    if (not isinstance(disposition, dict) or disposition.get("decision") != "integrated_in_job"
                            or disposition.get("covered_by_landing_job_id") != job["landing_job_id"]
                            or not disposition.get("basis") or not disposition.get("evidence")):
                        raise ValueError("cross-type future coverage needs sourced integrated disposition")
                approved_jobs_by_type[published_type] += 1
                approved_job_ids.append(job["landing_job_id"])
            elif job.get("disposition") not in {"no_new_page", "rejected"} or job.get("published_type_id") is not None or job.get("action_id"):
                raise ValueError("noncreate landing decision cannot claim a published page")
            else:
                for cluster_id in cluster_ids:
                    disposition = candidates_by_id[cluster_id].get("disposition", {})
                    allowed = {"rejected"} if job["disposition"] == "rejected" else {"covered", "no_new_page"}
                    if not isinstance(disposition, dict) or disposition.get("decision") not in allowed or not disposition.get("basis") or not disposition.get("evidence"):
                        raise ValueError("noncreate landing decision needs matching sourced disposition")
        job_decisions_resolved = (assigned_clusters == candidate_ids and assigned_intents == set(supported)
                                  and not unresolved and not unmapped_candidates
                                  and not any(row["status"] in {"needs_business_answer", "unknown"} for row in demand_clusters))
        kinds.update(approved_jobs_by_type)
    # URL work and the eventual canonical inventory are different quantities.  A
    # legacy input without route decisions remains an unknown projection.
    raw_routes = data.get("planned_url_dispositions", [])
    if not isinstance(raw_routes, list):
        raise ValueError("planned_url_dispositions must be a list")
    routes = {}
    for route in raw_routes:
        if not isinstance(route, dict) or route.get("source_url") not in by_canonical:
            raise ValueError("planned URL disposition needs an observed canonical source_url")
        source_url = route["source_url"]
        if source_url in routes:
            raise ValueError("duplicate planned URL disposition")
        decision = route.get("disposition")
        if decision not in {"retain_canonical", "retire_410", "redirect_or_merge", "pending"}:
            raise ValueError("invalid planned URL disposition")
        if decision == "pending":
            if not isinstance(route.get("reason"), str) or not route["reason"].strip():
                raise ValueError("pending planned URL disposition needs a reason")
        elif not isinstance(route.get("source_ref"), str) or not route["source_ref"].strip() or not isinstance(route.get("basis"), str) or not route["basis"].strip():
            raise ValueError("resolved planned URL disposition needs source_ref and basis")
        if decision != "pending":
            source_ref = route["source_ref"]
            if source_ref.startswith(("/", "\\")) or ":" in source_ref or "\\" in source_ref or ".." in Path(source_ref).parts:
                raise ValueError("resolved planned URL source_ref must be a run-relative file path")
        if decision == "redirect_or_merge":
            destination = route.get("destination_url")
            destination_job = route.get("destination_landing_job_id")
            if (destination is None) == (destination_job is None):
                raise ValueError("redirect/merge needs exactly one canonical URL or approved landing job destination")
            if destination is not None and not isinstance(destination, str):
                raise ValueError("redirect/merge destination_url needs a canonical URL")
            if destination_job is not None and (not isinstance(destination_job, str) or not destination_job.strip()):
                raise ValueError("redirect/merge destination_landing_job_id needs a job ID")
        elif route.get("destination_url") is not None or route.get("destination_landing_job_id") is not None:
            raise ValueError("only redirect/merge has a destination")
        routes[source_url] = route

    approved_jobs = [job for job in (raw_jobs or []) if job["disposition"] == "create"]
    approved_jobs_by_id = {job["landing_job_id"]: job for job in approved_jobs}
    proposed_urls = {}
    pending_jobs = []
    for job in approved_jobs:
        proposed = job.get("proposed_url")
        if proposed is None:
            pending_jobs.append(job["landing_job_id"])
        elif not isinstance(proposed, str) or not proposed.strip():
            raise ValueError("proposed_url must be a nonempty canonical identity or absent while pending")
        else:
            identity = canonical_identity(proposed, data["client_domain"])
            if identity in observed_identities or identity in proposed_urls:
                raise ValueError("approved create canonical identity collides with an observed or proposed URL")
            proposed_urls[identity] = job["landing_job_id"]
    observed_urls_by_identity = {canonical_identity(url, data["client_domain"]): url for url in by_canonical}
    pending_destinations = set()
    for source_url, route in routes.items():
        if route["disposition"] != "redirect_or_merge":
            continue
        destination_job = route.get("destination_landing_job_id")
        if destination_job is not None:
            if destination_job not in approved_jobs_by_id:
                raise ValueError("redirect/merge destination job must be an approved create job")
            if destination_job in pending_jobs:
                pending_destinations.add(source_url)
            continue
        identity = canonical_identity(route["destination_url"], data["client_domain"])
        if identity == canonical_identity(source_url, data["client_domain"]):
            raise ValueError("redirect/merge cannot target itself")
        observed_destination = observed_urls_by_identity.get(identity)
        if observed_destination is not None:
            target_route = routes.get(observed_destination)
            if target_route is None or target_route["disposition"] != "retain_canonical":
                raise ValueError("redirect/merge observed destination must be explicitly retained; chains and unresolved targets are invalid")
        elif identity not in proposed_urls:
            raise ValueError("redirect/merge destination must be a retained canonical or unique approved create identity")
    pending_urls = sorted(set(by_canonical) - set(routes) | {url for url, route in routes.items() if route["disposition"] == "pending"} | pending_destinations)
    projection_resolved = complete and job_decisions_resolved and not pending_urls and not pending_jobs
    retained_by_type = Counter(page_type(by_canonical[url].get("page_type")) for url, route in routes.items()
                               if route["disposition"] == "retain_canonical")
    planned_total = sum(retained_by_type.values()) + len(approved_jobs) if projection_resolved else None
    page_plan = {
        "decision_status": "resolved" if job_decisions_resolved else "pending",
        "approved_new_page_job_ids": sorted(approved_job_ids) if job_decisions_resolved else None,
        "approved_new_pages": len(approved_job_ids) if job_decisions_resolved else None,
        "approved_new_pages_by_published_type": dict(sorted(approved_jobs_by_type.items())) if job_decisions_resolved else None,
        "gross_observed_plus_approved_jobs": len(pages) + len(approved_job_ids) if complete and job_decisions_resolved else None,
        "projection_status": "resolved" if projection_resolved else "pending",
        "pending_inventory_scope": not complete,
        "pending_source_urls": pending_urls,
        "pending_create_job_ids": sorted(pending_jobs),
        "planned_retained_existing_count": sum(retained_by_type.values()) if projection_resolved else None,
        "approved_published_page_target_all": planned_total,
    }
    if untyped_supported:
        kinds.add("unknown")
    for peer in competitors:
        if peer["type_counts_comparable"]:
            kinds.update(peer["type_counts"])
    type_gap = []
    for kind in sorted(kinds):
        comparable_peers = [peer for peer in competitors if peer["type_counts_comparable"]]
        peer_counts = [peer["type_counts"].get(kind, 0) for peer in comparable_peers]
        peer_rank_rows = []
        for raw_peer in data.get("competitors", []):
            peer_result = next(peer for peer in competitors if peer["domain"] == raw_peer["domain"])
            peer_ranks = raw_peer.get("rankings", {})
            if not peer_result["type_counts_comparable"]:
                continue
            peer_type_by_url = {p["canonical_url"]: page_type(p.get("page_type")) for p in raw_peer.get("pages", [])}
            rank_result, comparisons = {}, {}
            for source in ("gsc", "serp"):
                selected = [row for row in peer_ranks.get(source, [])
                            if page_type(peer_type_by_url.get(row["canonical_url"], row.get("page_type"))) == kind]
                own_selected = [row for row in ranks.get(source, []) if own_observed_type(row) == kind]
                rank_result[source] = weighted_rank(selected, source) if selected else None
                if selected:
                    comparisons[source] = rank_cohort_comparison(
                        own_selected, selected, ranks.get("scope"), peer_ranks.get("scope"), source)
            if any(value is not None and value["average_position"] is not None for value in rank_result.values()):
                peer_rank_rows.append({"domain": raw_peer["domain"], **rank_result,
                                       "own_rank_scope": ranks.get("scope"),
                                       "peer_rank_scope": peer_ranks.get("scope"),
                                       "cohort_comparison": comparisons})
        current_observed = sum(type_actions.get(kind, {}).values())
        supported_intent_count = supported_intents_by_type[kind]
        approved_count = approved_jobs_by_type[kind] if job_decisions_resolved else None
        own_type_urls = {url for url, page in by_canonical.items() if page_type(page.get("page_type")) == kind}
        observed_own_rows = [row for source in ("gsc", "serp") for row in ranks.get(source, [])
                             if own_observed_type(row) == kind and row.get("position") is not None
                             and row.get("impressions" if source == "gsc" else "weight", 1 if source == "serp" else 0) > 0]
        type_gap.append({
            "page_type": kind,
            "rank_coverage": {"observed_own_rows": len(observed_own_rows),
                              "ranked_own_urls": len({row["canonical_url"] for row in observed_own_rows}),
                              "own_urls": len(own_type_urls)},
            "purpose_counts": dict(type_purpose.get(kind, {})),
            "current": current_observed if complete else None,
            "current_observed": current_observed,
            "competitor_count_mean": mean(peer_counts) if peer_counts else None,
            "competitor_count_median": median(peer_counts) if peer_counts else None,
            "competitor_count_peer_count": len(peer_counts),
            "competitor_count_domains": [peer["domain"] for peer in comparable_peers],
            "supported_missing_intent_count": supported_intent_count,
            "untyped_supported_missing_intent_count": len(untyped_supported) if kind == "unknown" else 0,
            "unresolved_missing_intent_count": unresolved_by_type[kind],
            "approved_new_page_jobs": approved_count,
            "approved_published_page_target": retained_by_type[kind] + approved_count if projection_resolved and approved_count is not None else None,
            "demand_cluster_statuses": dict(demand_by_type[kind]),
            "demand_cluster_ids": demand_cluster_ids_by_type[kind],
            "unmapped_create_candidates": sum(row['page_type'] == kind and not row['reconciled'] for row in candidate_reconciliation),
            "improve": type_actions[kind]["optimize"] + type_actions[kind]["rework"],
            "unknown": type_actions[kind]["unknown"],
            "average_position_ours": by_type_rank.get(kind, {"gsc": None, "serp": None}),
            "average_position_competitors": peer_rank_rows or None,
            "rank_source_scope": ranks.get("scope"),
            "inventory_complete": complete,
        })

    links = data.get("links", [])
    if not isinstance(links, list):
        raise ValueError("links must be a list")
    link_assessment = data.get("links_assessment")
    if link_assessment is None:
        link_assessment = {"status": "not_reported", "reason": "Link source assessment was not supplied",
                           "limitations": "Link gap is unknown", "evidence_files": {}}
    if not isinstance(link_assessment, dict) or link_assessment.get("status") not in {"available", "partial", "unavailable", "not_reported"}:
        raise ValueError("links_assessment needs an explicit source status")
    link_status = link_assessment["status"]
    if link_status != "not_reported":
        evidence_files = link_assessment.get("evidence_files")
        if (not isinstance(evidence_files, dict) or not evidence_files
                or any(not isinstance(path, str) or not path or not isinstance(sha, str)
                       or not re.fullmatch(r"[0-9a-f]{64}", sha) for path, sha in evidence_files.items())):
            raise ValueError("links_assessment needs saved source evidence file hashes")
        if not isinstance(link_assessment.get("source_system"), str) or not link_assessment["source_system"].strip():
            raise ValueError("links_assessment needs source_system")
        if not isinstance(link_assessment.get("limitations"), str) or not link_assessment["limitations"].strip():
            raise ValueError("links_assessment needs source limitations")
        if link_status in {"partial", "unavailable"} and (not isinstance(link_assessment.get("reason"), str) or not link_assessment["reason"].strip()):
            raise ValueError("Unavailable/partial link source needs exact reason")
        if any(not isinstance(row, dict) or row.get("source_ref") not in evidence_files for row in links):
            raise ValueError("Link row must bind saved source evidence")
    if link_status == "available":
        client_rows = [row for row in links if row.get("domain") == data.get("client_domain")]
        comparable_pairs = [(client, peer) for client in client_rows for peer in links
                            if peer.get("domain") != data.get("client_domain")
                            and any(x["domain"] == peer.get("domain") and x["relevant"] is True for x in competitors)
                            and client.get("complete") is True and peer.get("complete") is True
                            and all(client.get(key) == peer.get(key) and client.get(key) is not None for key in LINK_KEYS)
                            and all(isinstance(row.get(metric), int) and not isinstance(row.get(metric), bool)
                                    and row[metric] >= 0 for row in (client, peer)
                                    for metric in ("live_referring_domains", "live_links"))]
        if not comparable_pairs:
            raise ValueError("Available link source needs measured comparable client and relevant peer rows")
    elif link_status == "unavailable" and links:
        raise ValueError("Unavailable link source cannot contain measured rows")
    client_domain = data.get("client_domain")
    raw_observations, raw_benchmarks, raw_pairs, raw_primary_complete = raw_link_projection(links, link_assessment, competitors, client_domain)
    comparisons = []
    for peer in links:
        if peer.get("domain") == client_domain:
            continue
        client = next((x for x in links if x.get("domain") == client_domain and all(x.get(k) == peer.get(k) for k in LINK_KEYS)), None)
        relevant_peer = any(x["domain"] == peer.get("domain") and x["relevant"] is True for x in competitors)
        same = (link_status in {"available", "not_reported"} and relevant_peer and client is not None
                and peer.get("complete") is True and client.get("complete") is True)
        for entry in (peer, client) if client else (peer,):
            for metric in ("live_referring_domains", "live_links"):
                value = entry.get(metric)
                if value is not None:
                    require_nonnegative_int(value, metric)
        comparisons.append({
            "domain": peer.get("domain"),
            "comparable": same,
            "relevant": relevant_peer,
            "provider": peer.get("provider"),
            "date": peer.get("date"),
            "scope": peer.get("scope"),
            "client_live_referring_domains": client.get("live_referring_domains") if client else None,
            "peer_live_referring_domains": peer.get("live_referring_domains"),
            "client_live_links": client.get("live_links") if client else None,
            "peer_live_links": peer.get("live_links"),
            "referring_domain_delta": peer["live_referring_domains"] - client["live_referring_domains"] if same and peer.get("live_referring_domains") is not None and client.get("live_referring_domains") is not None else None,
            "links_delta": peer["live_links"] - client["live_links"] if same and peer.get("live_links") is not None and client.get("live_links") is not None else None,
            "raw_pair": raw_pairs[id(peer)],
            "interpretation": "benchmark only; review relevance and quality before any link work",
        })

    link_groups = {}
    for row in comparisons:
        if not row["comparable"]:
            continue
        group_key = json.dumps(
            [row["provider"], row["date"], row["scope"]],
            ensure_ascii=False, sort_keys=True,
        )
        link_groups.setdefault(group_key, []).append(row)
    link_benchmarks = []
    for group in link_groups.values():
        first = group[0]
        summary = {
            "provider": first["provider"],
            "date": first["date"],
            "scope": first["scope"],
            "interpretation": "observed difference only; not a link acquisition target",
        }
        for metric, client_field, peer_field in (
            ("live_referring_domains", "client_live_referring_domains", "peer_live_referring_domains"),
            ("live_links", "client_live_links", "peer_live_links"),
        ):
            valid = [row for row in group if row[peer_field] is not None]
            values = [row[peer_field] for row in valid]
            client_value = first[client_field]
            peer_mean = mean(values) if values else None
            peer_median = median(values) if values else None
            summary[metric] = {
                "client_count": client_value,
                "peer_mean": peer_mean,
                "peer_median": peer_median,
                "peer_count": len(valid),
                "peer_domains": [row["domain"] for row in valid],
                "mean_difference_from_client": peer_mean - client_value if peer_mean is not None and client_value is not None else None,
                "median_difference_from_client": peer_median - client_value if peer_median is not None and client_value is not None else None,
            }
        link_benchmarks.append(summary)

    return {
        "assessment": {
            "action_review": {
                "observed_pages": len(pages),
                "assessed_pages": assessed,
                "unknown_pages": actions["unknown"],
                "assessed_fraction": assessed / len(pages) if pages else None,
                "complete": action_complete,
                "by_type": {kind: {"assessed": sum(counts.values()) - counts["unknown"], "unknown": counts["unknown"]} for kind, counts in sorted(type_actions.items())},
            },
            "demand_review": {
                "status": "complete_in_declared_scope" if demand_complete else "structural_complete_in_declared_scope" if structural_complete else "partial" if demand_clusters else "not_reported",
                "complete": demand_complete,
                "structural_complete": structural_complete,
                "comparison": comparison,
                "clusters_observed": len(demand_clusters),
                "status_counts": dict(demand_counts),
                "scope": demand.get("scope"),
                "sources": demand.get("sources"),
            },
            "whole_site_gap_complete": complete and action_complete and demand_complete and job_decisions_resolved and not unmapped_candidates and not unresolved and not untyped_supported and link_status != "not_reported" and raw_primary_complete and not any(page["purpose"] == "unknown" or page_type(page.get("page_type")) == "unknown" for page in pages),
            "raw_primary_complete": raw_primary_complete,
            "interpretation": "inventory completeness alone is not full GAP; missing intents are demand evidence, not approved page work",
        },
        "inventory": {
            "complete": complete,
            "scope": inventory_scope,
            "canonical_pages_observed": len(pages),
            "canonical_pages_total": len(pages) if complete else None,
            "purpose_action_table": {purpose: {"pages": sum(counts.values()), **dict(counts)} for purpose, counts in sorted(purpose_counts.items())},
            "type_purpose_table": {kind: {"pages": sum(counts.values()), "purpose_counts": dict(counts), "actions": dict(type_actions[kind])} for kind, counts in sorted(type_purpose.items())},
            "actions": dict(actions),
            "unknown_purpose_pages": sum(p["purpose"] == "unknown" for p in pages),
        },
        "missing_intents": {"supported_count": len(supported), "supported_ids": supported, "untyped_supported_ids": untyped_supported, "unresolved_ids": unresolved, "supported_total_if_resolved": None if unresolved or unmapped_candidates else len(supported)},
        "demand_candidate_reconciliation": {"complete": not unmapped_candidates, "unmapped_candidate_ids": unmapped_candidates, "rows": candidate_reconciliation},
        "page_plan": page_plan,
        "competitors": competitors,
        "ranks": {"gsc": gsc, "serp": serp, "by_purpose": by_purpose_rank, "by_type": by_type_rank, "distinct_observed_ranking_urls_union": len(rank_urls), "coverage": ranks.get("coverage"), "scope": ranks.get("scope")},
        "type_gap": type_gap,
        "type_gap_totals": {
            "typed_supported_missing_intent_count": sum(supported_intents_by_type.values()),
            "untyped_supported_missing_intent_count": len(untyped_supported),
            "supported_missing_intent_count": len(supported),
            "intent_reconciled": sum(supported_intents_by_type.values()) + len(untyped_supported) == len(supported) and not unmapped_candidates,
            "type_allocation_complete": len(untyped_supported) == 0 and not unresolved and not unmapped_candidates,
        },
        "links": comparisons,
        "link_benchmarks": link_benchmarks,
        "raw_link_observations": raw_observations,
        "raw_link_benchmarks": raw_benchmarks,
        "links_assessment": {**link_assessment,
                             "client_live_referring_domains": comparable_pairs[0][0]["live_referring_domains"] if link_status == "available" else None,
                             "client_live_links": comparable_pairs[0][0]["live_links"] if link_status == "available" else None},
        "drilldown": {"pages": pages, "missing_intents": missing},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = summarize(json.loads(args.input.read_text(encoding="utf-8")))
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
