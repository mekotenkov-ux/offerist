"""Official read-only Yandex Metrika bridge for a specified existing env file."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api-metrika.yandex.net"


def paginate(fetch_page, row_field: str, total_field: str, page_size: int,
             preview_rows: int | None = None, identity=None) -> dict:
    """Walk one-based Metrica offsets; preserve partial data on provider failure."""
    rows, seen = [], set()
    offset, pages, attempted, first, reason = 1, 0, 0, None, None
    total = None
    while True:
        limit = page_size if preview_rows is None else min(page_size, preview_rows - len(rows))
        if limit < 1:
            reason = "preview_limit"
            break
        attempted += 1
        try:
            raw = fetch_page(offset, limit)
        except Exception:
            reason = "provider_error"
            break
        if first is None:
            first = raw
        page = raw.get(row_field)
        if not isinstance(page, list) or len(page) > limit:
            reason = "invalid_provider_page"
            break
        pages += 1
        current_total = raw.get(total_field)
        if isinstance(current_total, int) and current_total >= 0:
            if total is not None and total != current_total:
                reason = "total_changed"
                break
            total = current_total
        if not page:
            reason = "api_exhausted"
            break
        added = 0
        for row in page:
            key = identity(row) if identity else json.dumps(row, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
            added += 1
        if added == 0:
            reason = "repeated_page"
            break
        if added != len(page):
            reason = "duplicate_rows"
            break
        offset += len(page)
        if total is not None and len(rows) >= total and not raw.get("total_rows_rounded"):
            reason = "reported_total_reached"
            break
        if len(page) < limit:
            reason = "api_exhausted"
            break
        if preview_rows is not None and len(rows) >= preview_rows:
            reason = "preview_limit"
            break
    truncated = reason == "preview_limit"
    return {"rows": rows, "first": first or {}, "pages_fetched": pages,
            "api_requests_attempted": attempted,
            "reported_total": total, "truncated": truncated,
            "incomplete": reason not in ("reported_total_reached", "api_exhausted"),
            "coverage": {"rows_retrieved": len(rows), "reported_total": total,
                         "absent_row_is_not_zero": True,
                         "stop_reason": reason}}


def read_token(env_file: pathlib.Path) -> str:
    if not env_file.is_file():
        raise RuntimeError("env_file_missing")
    for raw in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith("YANDEX_METRIKA_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("metrika_token_missing")


def get(token: str, endpoint: str, params: dict) -> dict:
    url = f"{API}{endpoint}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url, headers={"Authorization": f"OAuth {token}", "Accept": "application/json"}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"http_{error.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("connection_failed") from None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=pathlib.Path, required=True)
    parser.add_argument("--mode", choices=("counters", "counter-detail", "report"), required=True)
    parser.add_argument("--counter-id")
    parser.add_argument("--search-string", help="Filter counters by site, mirror, name or exact ID")
    parser.add_argument("--site-domain", help="Restrict report landing domains to this host and www variant")
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--grouping", choices=("total", "search-engine", "landing-device"))
    parser.add_argument("--goal-id")
    parser.add_argument("--limit", type=int, default=1000, help="Provider page size, not a result cap")
    parser.add_argument("--preview-rows", type=int, help="Explicit incomplete preview cap")
    parser.add_argument("--landing-path-contains", help="Explicit URL-path segment, not inferred intent")
    parser.add_argument("--segment-label")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100000:
        parser.error("--limit must be 1..100000")
    if args.preview_rows is not None and args.preview_rows < 1:
        parser.error("--preview-rows must be positive")
    if args.mode != "counters" and not args.counter_id:
        parser.error("--counter-id is required")
    if args.mode == "report" and not all((args.date_from, args.date_to, args.grouping)):
        parser.error("report requires dates and grouping")
    if args.goal_id and not args.goal_id.isdigit():
        parser.error("--goal-id must be numeric")
    if args.site_domain:
        args.site_domain = args.site_domain.lower().removeprefix("www.")
        if (len(args.site_domain) > 253 or not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?)+", args.site_domain)):
            parser.error("--site-domain must be a plain DNS domain")
    if args.landing_path_contains and not re.fullmatch(r"/[A-Za-z0-9/_-]{1,200}", args.landing_path_contains):
        parser.error("--landing-path-contains must be a simple URL path")
    token = read_token(args.env_file)
    if not token:
        raise RuntimeError("metrika_token_empty")
    started = time.perf_counter()
    if args.mode == "counters":
        params = {"per_page": min(args.limit, 10000), "offset": 1}
        if args.search_string:
            params["search_string"] = args.search_string
        walked = paginate(lambda offset, limit: get(token, "/management/v1/counters",
                          {**params, "offset": offset, "per_page": limit}),
                          "counters", "rows", min(args.limit, 10000), args.preview_rows,
                          identity=lambda row: str(row.get("id")))
        counters = [
            {
                "id": c.get("id"), "name": c.get("name"), "site": c.get("site"),
                "site2": c.get("site2"), "mirrors": c.get("mirrors"), "mirrors2": c.get("mirrors2"),
                "status": c.get("status"), "permission": c.get("permission"),
                "time_zone_name": c.get("time_zone_name"),
            }
            for c in walked["rows"]
        ]
        result = {"source": "Yandex Metrika management/v1/counters", "mode": "counters",
                  "search_string": args.search_string,
                  "reported_total": walked["reported_total"], "returned": len(counters),
                  "truncated": walked["truncated"], "incomplete": walked["incomplete"],
                  "coverage": walked["coverage"], "pages_fetched": walked["pages_fetched"],
                  "api_requests_attempted": walked["api_requests_attempted"],
                  "counters": counters}
        count = len(counters)
    elif args.mode == "counter-detail":
        raw = get(token, f"/management/v1/counter/{args.counter_id}", {"field": "goals,mirrors"})
        c = raw.get("counter") or {}
        result = {
            "source": "Yandex Metrika management/v1/counter/{id}", "mode": "counter-detail",
            "counter_id": c.get("id"), "site": c.get("site"), "site2": c.get("site2"),
            "mirrors": c.get("mirrors"), "mirrors2": c.get("mirrors2"),
            "time_zone_name": c.get("time_zone_name"),
            "goals": [
                {"id": g.get("id"), "name": g.get("name"), "type": g.get("type"),
                 "goal_source": g.get("goal_source"), "conditions": g.get("conditions")}
                for g in c.get("goals") or []
            ],
        }
        count = len(result["goals"])
    else:
        dimensions = {
            "total": None,
            "search-engine": "ym:s:searchEngine",
            "landing-device": "ym:s:startURL,ym:s:deviceCategory",
        }[args.grouping]
        metrics = "ym:s:visits"
        if args.goal_id:
            metrics += f",ym:s:goal{args.goal_id}visits"
        filter_expression = "ym:s:trafficSource=='organic'"
        if args.site_domain:
            domain_dimension = "ym:s:startURLDomain"
            filter_expression += (f" AND ({domain_dimension}=='{args.site_domain}'"
                                  f" OR {domain_dimension}=='www.{args.site_domain}')")
        if args.landing_path_contains:
            filter_expression += f" AND ym:s:startURLPath=@'{args.landing_path_contains}'"
        request_params = {
            "ids": args.counter_id, "date1": args.date_from, "date2": args.date_to,
            "metrics": metrics, "filters": filter_expression,
        }
        if dimensions:
            request_params["dimensions"] = dimensions
        walked = paginate(lambda offset, limit: get(token, "/stat/v1/data",
                          {**request_params, "offset": offset, "limit": limit}),
                          "data", "total_rows", args.limit, args.preview_rows,
                          identity=lambda row: json.dumps(row.get("dimensions", []), ensure_ascii=False, sort_keys=True))
        raw = walked["first"]
        rows = walked["rows"]
        result = {
            "source": "Yandex Metrika stat/v1/data", "mode": "report",
            "counter_id": args.counter_id, "date_from": args.date_from,
            "date_to": args.date_to, "site_domain": args.site_domain,
            "filter": filter_expression,
            "dimensions": dimensions.split(",") if dimensions else [], "metrics": metrics.split(","),
            "sampled": raw.get("sampled"), "sample_share": raw.get("sample_share"),
            "sample_size": raw.get("sample_size"), "sample_space": raw.get("sample_space"),
            "contains_sensitive_data": raw.get("contains_sensitive_data"),
            "total_rows": walked["reported_total"], "total_rows_rounded": raw.get("total_rows_rounded"),
            "totals": raw.get("totals"), "rows": rows,
            "truncated": walked["truncated"],
            "incomplete": walked["incomplete"] or bool(raw.get("sampled")) or bool(raw.get("contains_sensitive_data")),
            "coverage": {**walked["coverage"], "sampled": raw.get("sampled"),
                         "sample_share": raw.get("sample_share"), "total_rows_rounded": raw.get("total_rows_rounded")},
            "pages_fetched": walked["pages_fetched"], "segment_label": args.segment_label,
            "api_requests_attempted": walked["api_requests_attempted"],
            "segment_intent_verified": False,
        }
        count = len(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    stop = (result.get("coverage") or {}).get("stop_reason")
    status = "partial" if result.get("truncated") or stop not in (None, "reported_total_reached", "api_exhausted") else "ok"
    print(json.dumps({"system": "Yandex Metrika", "mode": args.mode, "status": status,
                      "rows": count, "duration_seconds": round(time.perf_counter() - started, 3)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        code = str(exc)
        if not (code.startswith("http_") or code in {
            "connection_failed", "metrika_token_missing", "metrika_token_empty", "env_file_missing",
            "response_exceeds_limit"
        }):
            code = type(exc).__name__
        print(json.dumps({"system": "Yandex Metrika", "status": "error", "error_code": code}))
        raise SystemExit(1)
