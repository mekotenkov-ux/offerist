"""Paginated, read-only Yandex Webmaster bridge; credentials are never saved."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API = "https://api.webmaster.yandex.net/v4"
INDICATORS = ("TOTAL_SHOWS", "TOTAL_CLICKS", "AVG_SHOW_POSITION", "AVG_CLICK_POSITION")


def read_env_token(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError("env_file_missing")
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith("YANDEX_WEBMASTER_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("webmaster_token_missing")


def get(token: str, path: str, params: dict | None = None) -> dict:
    # Webmaster routes require the terminal slash; omitting it can return
    # RESOURCE_NOT_FOUND even for an authenticated /user request.
    url = API + path.rstrip("/") + "/" + ("?" + urlencode(params, doseq=True) if params else "")
    request = Request(url, headers={"Authorization": "OAuth " + token, "Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"http_{error.code}") from None
    except URLError:
        raise RuntimeError("connection_failed") from None


def flatten_query(row: dict) -> dict:
    values = row.get("indicators") or {}
    return {"query_id": row.get("query_id"), "query": row.get("query_text"),
            "shows": values.get("TOTAL_SHOWS"), "clicks": values.get("TOTAL_CLICKS"),
            "avg_show_position": values.get("AVG_SHOW_POSITION"),
            "avg_click_position": values.get("AVG_CLICK_POSITION")}


def paginate_popular(fetch_page, page_size: int = 500, preview_rows: int | None = None) -> dict:
    rows, seen = [], set()
    offset, pages, attempted, count, reason = 0, 0, 0, None, None
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
        page = raw.get("queries")
        if not isinstance(page, list) or len(page) > limit:
            reason = "invalid_provider_page"
            break
        pages += 1
        current_count = raw.get("count")
        if current_count is not None:
            try:
                current_count = int(current_count)
            except (TypeError, ValueError):
                reason = "invalid_provider_count"
                break
            if count is not None and count != current_count:
                reason = "count_changed"
                break
            count = current_count
        if not page:
            reason = "api_exhausted"
            break
        added = 0
        for row in page:
            key = str(row.get("query_id") or row.get("query_text"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(flatten_query(row))
            added += 1
        if added == 0:
            reason = "repeated_page"
            break
        if added != len(page):
            reason = "duplicate_rows"
            break
        offset += len(page)
        if count is not None and len(rows) >= count:
            reason = "reported_count_reached"
            break
        if len(page) < limit:
            reason = "api_exhausted"
            break
        if preview_rows is not None and len(rows) >= preview_rows:
            reason = "preview_limit"
            break
    return {"rows": rows, "pages_fetched": pages, "api_requests_attempted": attempted,
            "reported_count": count,
            "truncated": reason == "preview_limit", "incomplete": True,
            "coverage": {"scope": "provider_popular_queries_only", "rows_retrieved": len(rows),
                         "absent_row_is_not_zero": True,
                         "reported_count": count, "stop_reason": reason,
                         "provider_top_list_not_all_queries": True}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--mode", choices=("user", "hosts", "queries", "totals"), required=True)
    parser.add_argument("--user-id")
    parser.add_argument("--host-id")
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--order-by", choices=("TOTAL_SHOWS", "TOTAL_CLICKS"), default="TOTAL_SHOWS")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-rows", type=int, help="Legacy explicit preview cap")
    parser.add_argument("--preview-rows", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.page_size <= 500:
        parser.error("--page-size must be 1..500")
    preview = args.preview_rows if args.preview_rows is not None else args.max_rows
    if preview is not None and preview < 1:
        parser.error("preview rows must be positive")
    if args.mode in ("hosts", "queries", "totals") and (not args.user_id or not args.user_id.isdigit()):
        parser.error("--user-id is required and numeric")
    if args.mode in ("queries", "totals") and not all((args.host_id, args.date_from, args.date_to)):
        parser.error("report requires --host-id and dates")
    token = read_env_token(args.env_file)
    if not token:
        raise RuntimeError("webmaster_token_empty")
    started = perf_counter()
    if args.mode == "user":
        raw = get(token, "/user")
        result = {"source": "Yandex Webmaster API v4", "mode": "user", "user_id": raw["user_id"]}
        count = 1
    elif args.mode == "hosts":
        raw = get(token, f"/user/{args.user_id}/hosts")
        hosts = raw.get("hosts") or []
        result = {"source": "Yandex Webmaster API v4", "mode": "hosts", "hosts": [
            {"host_id": h.get("host_id"), "url": h.get("unicode_host_url") or h.get("ascii_host_url"),
             "verified": h.get("verified")} for h in hosts]}
        count = len(hosts)
    elif args.mode == "totals":
        path = f"/user/{args.user_id}/hosts/{quote(args.host_id, safe='')}/search-queries/all/history"
        raw = get(token, path, {"query_indicator": list(INDICATORS),
                                "device_type_indicator": "ALL", "date_from": args.date_from,
                                "date_to": args.date_to})
        indicators = raw.get("indicators") or {}
        result = {"source": "Yandex Webmaster API v4", "mode": "totals", "host_id": args.host_id,
                  "date_from": args.date_from, "date_to": args.date_to,
                  "grain": "date x host", "totals": indicators, "rows": [],
                  "truncated": False, "incomplete": not bool(indicators),
                  "coverage": {"scope": "provider_all_query_history_totals_only", "no_query_rows": True,
                               "has_indicators": bool(indicators), "absent_row_is_not_zero": True},
                  "pages_fetched": 1, "api_requests_attempted": 1}
        count = 0
    else:
        path = f"/user/{args.user_id}/hosts/{quote(args.host_id, safe='')}/search-queries/popular"
        params = {"order_by": args.order_by, "query_indicator": list(INDICATORS),
                  "device_type_indicator": "ALL", "date_from": args.date_from,
                  "date_to": args.date_to}
        walked = paginate_popular(lambda offset, limit: get(token, path,
                                  {**params, "offset": offset, "limit": limit}), args.page_size, preview)
        result = {"source": "Yandex Webmaster API v4", "mode": "queries", "host_id": args.host_id,
                  "date_from": args.date_from, "date_to": args.date_to, "order_by": args.order_by,
                  "grain": "query x host", **walked}
        count = len(walked["rows"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    stop = (result.get("coverage") or {}).get("stop_reason")
    status = "partial" if result.get("truncated") or stop not in (None, "reported_count_reached", "api_exhausted") else "ok"
    print(json.dumps({"system": "Yandex Webmaster", "mode": args.mode, "status": status,
                      "rows": count, "duration_seconds": round(perf_counter() - started, 3)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        code = str(exc)
        if not (re.fullmatch(r"http_\d{3}", code) or code in {
            "connection_failed", "env_file_missing", "webmaster_token_missing",
            "webmaster_token_empty", "response_exceeds_limit"
        }):
            code = type(exc).__name__
        print(json.dumps({"system": "Yandex Webmaster", "status": "error", "error_code": code}))
        raise SystemExit(1)
