"""Read-only, cached Keys.so and Ahrefs bridge for an authorized domain.

Keys.so organic reports are collected page by page. A complete result requires
consistent pagination metadata and every page; previews and interrupted runs
report their coverage explicitly. Credentials come only from an explicit env
file or this process's environment and are never written to output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASES = ("msk", "gru", "zen", "gkv", "rnd", "ekb", "ufa", "sar", "krr",
         "prm", "sam", "kry", "oms", "kzn", "che", "nsk", "nnv", "vlg",
         "vrn", "spb", "mns", "tmn", "gmns", "tom", "gny")
KEYS_REPORTS = {
    "keys-pages": ("/report/simple/organic/sitepages", "it50|desc,url|asc"),
    "keys-keywords": ("/report/simple/organic/keywords", "wsk|desc,word|asc,url|asc"),
}
ENDPOINTS = {
    "keys-limits": ("keys", "/limits/all", False),
    "keys-dashboard": ("keys", "/report/simple/domain_dashboard", False),
    "ahrefs-limits": ("ahrefs", "/v3/subscription-info/limits-and-usage", False),
    "ahrefs-backlinks-stats": ("ahrefs", "/v3/site-explorer/backlinks-stats", True),
    "ahrefs-metrics": ("ahrefs", "/v3/site-explorer/metrics", True),
}
ROOT = Path.cwd() / ".offerist" / "cache" / "external"
SNAP = ROOT / "snapshots"
LEDGER = ROOT / "budget-ledger.jsonl"
ENV: Path | None = None
LIVE_ALLOWED = False
_LOCAL_LOCK = threading.RLock()
KEYS_INTERVAL_SECONDS = 1.05  # official limit: 10 requests in 10 seconds


@contextmanager
def _ledger_lock():
    """Serialize cache lookup, budget check, HTTP attempt, snapshot, and ledger."""
    with _LOCAL_LOCK:
        ROOT.mkdir(parents=True, exist_ok=True)
        with (ROOT / ".bridge.lock").open("a+b") as lockfile:
            lockfile.seek(0)
            if os.name == "nt":
                import msvcrt
                deadline = time.monotonic() + 120
                while True:
                    try:
                        msvcrt.locking(lockfile.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("external cache lock timeout")
                        time.sleep(0.1)
                try:
                    yield
                finally:
                    lockfile.seek(0)
                    msvcrt.locking(lockfile.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)


def valid_domain(value: str) -> str:
    """Accept a hostname, never a URL, path, wildcard, port, or IP address."""
    value = value.strip().rstrip(".").lower()
    try:
        ascii_name = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid domain") from exc
    labels = ascii_name.split(".")
    if (len(ascii_name) > 253 or len(labels) < 2 or
            any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x)
                for x in labels) or
            labels[-1].isdigit()):
        raise ValueError("invalid domain")
    return ascii_name


def credential(name: str) -> str:
    if ENV is not None:
        values = {}
        for line in ENV.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        value = values.get(name)
    else:
        value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing {name} credential")
    return value


def history() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines() if line.strip()]


def record(row: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _snapshot(provider: str, endpoint: str, params: dict) -> tuple[str, Path]:
    base = "https://api.keys.so" if provider == "keys" else "https://api.ahrefs.com"
    url = base + endpoint + ("?" + urlencode(params) if params else "")
    cache_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return url, SNAP / f"{provider}-{cache_id}.json"


def _cumulative_ahrefs_units() -> int:
    return sum(int(row.get("cost_units_actual") or 0) for row in history()
               if row.get("provider") == "ahrefs" and row.get("network_attempt"))


def _paid_ahrefs_attempts() -> int:
    return sum(1 for row in history() if row.get("provider") == "ahrefs" and
               row.get("paid") and row.get("network_attempt"))


def _pace_keys() -> float:
    """Space live Keys.so attempts across processes using the shared ledger."""
    previous = [row for row in history() if row.get("provider") == "keys" and
                row.get("network_attempt")]
    if previous:
        last = previous[-1]
        started = last.get("request_started_unix")
        if started is None and last.get("timestamp_utc"):
            try:
                started = datetime.fromisoformat(last["timestamp_utc"]).timestamp()
            except ValueError:
                pass
        if isinstance(started, (int, float)):
            wait = KEYS_INTERVAL_SECONDS - (time.time() - started)
            if wait > 0:
                time.sleep(wait)
    return time.time()


def invoke(provider: str, endpoint: str, params: dict, *, paid: bool = False,
           row_cap: int | None = None, refresh: bool = False,
           ahrefs_budget_units: int | None = None,
           ahrefs_max_requests: int | None = None) -> dict:
    """Fetch one request, cache it, and ledger every network attempt.

    ``row_cap`` is retained for callers using the old signature. It describes
    request size in the ledger and never imposes a session stop.
    """
    with _ledger_lock():
        return _invoke_locked(provider, endpoint, params, paid=paid, row_cap=row_cap,
                              refresh=refresh, ahrefs_budget_units=ahrefs_budget_units,
                              ahrefs_max_requests=ahrefs_max_requests)


def _invoke_locked(provider: str, endpoint: str, params: dict, *, paid: bool,
                   row_cap: int | None, refresh: bool,
                   ahrefs_budget_units: int | None,
                   ahrefs_max_requests: int | None) -> dict:
    allowed = {(p, e) for p, e, _ in ENDPOINTS.values()}
    allowed.update(("keys", e) for e, _ in KEYS_REPORTS.values())
    if (provider, endpoint) not in allowed:
        raise ValueError("unsupported aggregate/report endpoint")
    paid = paid or any(p == provider and e == endpoint and required
                       for p, e, required in ENDPOINTS.values())
    params = dict(params)
    if endpoint not in {"/limits/all", "/v3/subscription-info/limits-and-usage"}:
        field = "domain" if provider == "keys" else "target"
        if not params.get(field):
            raise ValueError(f"{field} is required")
        params[field] = valid_domain(str(params[field]))
    if provider == "keys" and "base" in params and params["base"] not in BASES:
        raise ValueError("unsupported Keys.so base")
    url, dest = _snapshot(provider, endpoint, params)
    if dest.exists() and not refresh:
        try:
            return {"cache": True, "snapshot": str(dest),
                    "data": json.loads(dest.read_text(encoding="utf-8"))}
        except (ValueError, OSError) as exc:
            return {"error": type(exc).__name__, "provider": provider,
                    "endpoint": endpoint, "cache": True}
    if not LIVE_ALLOWED:
        return {"error": "cache_miss_offline", "provider": provider,
                "endpoint": endpoint, "cache": False}
    if provider == "ahrefs" and paid:
        if ahrefs_max_requests is None or ahrefs_max_requests < 1:
            return {"error": "explicit_ahrefs_request_budget_required", "provider": provider,
                    "endpoint": endpoint}
        if _paid_ahrefs_attempts() >= ahrefs_max_requests:
            return {"error": "ahrefs_request_budget_reached", "provider": provider,
                    "endpoint": endpoint}
        if ahrefs_budget_units is not None:
            if ahrefs_budget_units < 1:
                return {"error": "invalid_ahrefs_unit_budget", "provider": provider,
                        "endpoint": endpoint}
            if any(row.get("provider") == "ahrefs" and row.get("paid") and
                   row.get("network_attempt") and row.get("status") == 200 and
                   row.get("cost_units_actual") is None for row in history()):
                return {"error": "ahrefs_cost_unmetered", "provider": provider,
                        "endpoint": endpoint}
            if _cumulative_ahrefs_units() >= ahrefs_budget_units:
                return {"error": "ahrefs_budget_reached", "provider": provider,
                        "endpoint": endpoint,
                        "cumulative_cost_units_actual": _cumulative_ahrefs_units()}
    if refresh and endpoint not in {"/limits/all", "/v3/subscription-info/limits-and-usage"}:
        raise ValueError("refresh is only available for limits")
    if refresh:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        dest = dest.with_name(dest.stem + "-" + stamp + ".json")
    headers = {"Accept": "application/json"}
    headers.update({"X-Keyso-TOKEN": credential("KEYSSO")} if provider == "keys"
                   else {"Authorization": "Bearer " + credential("AHREFS")})
    SNAP.mkdir(parents=True, exist_ok=True)
    started_unix = _pace_keys() if provider == "keys" else time.time()
    row = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
           "request_started_unix": started_unix,
           "provider": provider, "endpoint": endpoint, "params": params,
           "paid": paid, "network_attempt": True, "requested_row_cap": row_cap,
           "cost_units_actual": None, "cost_units_nominal": None,
           "rows": None, "status": None, "cache": "miss",
           "session_budget_units": ahrefs_budget_units if provider == "ahrefs" and paid else None,
           "session_max_requests": ahrefs_max_requests if provider == "ahrefs" and paid else None}
    try:
        with urlopen(Request(url, headers=headers), timeout=25) as response:
            row["status"] = response.status
            if response.status != 200:
                row["error_type"] = "non_200_response"
                row["retry_after"] = response.headers.get("Retry-After")
                record(row)
                return {"error": f"HTTP {response.status}", "provider": provider,
                        "endpoint": endpoint, "retry_after": row["retry_after"]}
            safe_headers = ("x-api-rows", "x-api-units-cost-row", "x-api-units-cost-total",
                            "x-api-units-cost-total-actual", "x-api-cache", "retry-after")
            row["response_headers"] = {key: response.headers.get(key) for key in safe_headers
                                       if response.headers.get(key)}
            for field, header in (("cost_units_actual", "x-api-units-cost-total-actual"),
                                  ("cost_units_nominal", "x-api-units-cost-total")):
                if response.headers.get(header):
                    try:
                        row[field] = int(response.headers[header])
                    except ValueError:
                        pass
            data = json.loads(response.read().decode("utf-8"))
            row["rows"] = (len(data["data"]) if isinstance(data, dict) and
                           isinstance(data.get("data"), list) else 1)
            if provider == "ahrefs" and response.headers.get("x-api-rows"):
                try:
                    row["rows"] = int(response.headers["x-api-rows"])
                except ValueError:
                    pass
            row["snapshot"] = str(dest)
            dest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            record(row)
            return {"cache": False, "snapshot": str(dest), "data": data, "ledger": row}
    except HTTPError as exc:
        row["status"] = exc.code
        row["error_type"] = "HTTPError"
        row["retry_after"] = exc.headers.get("Retry-After") if exc.headers else None
        record(row)
        return {"error": f"HTTP {exc.code}", "provider": provider,
                "endpoint": endpoint, "retry_after": row["retry_after"]}
    except (URLError, TimeoutError, OSError, ValueError, UnicodeError) as exc:
        row["error_type"] = type(exc).__name__
        record(row)
        return {"error": type(exc).__name__, "provider": provider, "endpoint": endpoint}


def _page_meta(data: dict, requested: int, per_page: int) -> tuple[int, int] | None:
    """Only documented, internally consistent Keys.so metadata proves extent."""
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None
    fields = (data.get("current_page"), data.get("per_page"),
              data.get("last_page"), data.get("total"))
    if any(type(value) is not int for value in fields):
        return None
    current, size, last, total = fields
    if current != requested or size != per_page or last < 1 or total < 0:
        return None
    expected_last = max(1, (total + size - 1) // size)
    if last != expected_last or requested > last:
        return None
    return last, total


def _row_identity(action: str, row: object) -> str | None:
    if not isinstance(row, dict):
        return None
    if action == "keys-pages":
        identity = row.get("url")
    else:
        identity = row.get("id") or row.get("kid")
        if identity is None:
            identity = (row.get("word"), row.get("url"))
            if identity == (None, None):
                return None
    if identity is None:
        return None
    return json.dumps(identity, ensure_ascii=False, sort_keys=True)


def collect_keys(action: str, domain: str, base: str = "msk", *, per_page: int = 25,
                 max_rows: int | None = None, term: str = "") -> dict:
    """Collect all pages unless a caller explicitly requests preview coverage."""
    if action not in KEYS_REPORTS or base not in BASES or per_page < 1:
        raise ValueError("invalid Keys.so report parameters")
    domain = valid_domain(domain)
    if max_rows is not None and max_rows < 1:
        raise ValueError("max_rows must be positive")
    endpoint, sort = KEYS_REPORTS[action]
    params = {"base": base, "domain": domain, "page": 1,
              "per_page": per_page, "sort": sort}
    if term:
        if action != "keys-keywords" or not re.fullmatch(r"[А-Яа-яЁёA-Za-z ]{2,80}", term):
            raise ValueError("term is valid only for keywords and must contain letters/spaces")
        params["filter"] = "wordLIKE" + term
    # The previous bridge cached just page 1 with a single-column sort. Use
    # it offline as partial evidence; live runs use a stable sort to finish.
    _, preferred_cache = _snapshot("keys", endpoint, params)
    if not preferred_cache.exists() and not LIVE_ALLOWED:
        legacy_sort = "it50|desc" if action == "keys-pages" else "wsk|desc"
        for legacy_size in dict.fromkeys((per_page, 20)):
            legacy = {**params, "per_page": legacy_size, "sort": legacy_sort}
            _, legacy_cache = _snapshot("keys", endpoint, legacy)
            if legacy_cache.exists():
                params = legacy
                per_page = legacy_size
                break
    legacy_sort_used = params["sort"] in {"it50|desc", "wsk|desc"}
    rows: list = []
    pages: list[dict] = []
    seen: set[str] = set()
    seen_rows: set[str] = set()
    last_page = total = None
    reason = None
    retry_after = None
    page = 1
    while True:
        params["page"] = page
        result = invoke("keys", endpoint, dict(params), row_cap=per_page)
        if "error" in result:
            reason = result["error"]
            retry_after = result.get("retry_after")
            break
        data = result["data"]
        meta = _page_meta(data, page, per_page)
        if meta is None:
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                rows.extend(data["data"][:max_rows - len(rows)] if max_rows is not None
                            else data["data"])
                pages.append({"page": page, "rows": len(data["data"]),
                              "cache": result["cache"], "snapshot": result["snapshot"],
                              "pagination_verified": False})
            reason = "missing_or_inconsistent_pagination_metadata"
            break
        found_last, found_total = meta
        if last_page is None:
            last_page, total = found_last, found_total
        elif (found_last, found_total) != (last_page, total):
            reason = "pagination_metadata_changed"
            break
        batch = data["data"]
        fingerprint = hashlib.sha256(json.dumps(batch, sort_keys=True,
                             ensure_ascii=False).encode("utf-8")).hexdigest()
        if fingerprint in seen and batch:
            reason = "repeated_page"
            break
        seen.add(fingerprint)
        if len(batch) != (per_page if page < last_page else total - per_page * (page - 1)):
            reason = "page_size_or_total_mismatch"
            break
        identities = [_row_identity(action, row) for row in batch]
        if any(identity is None for identity in identities):
            reason = "missing_row_identity"
            break
        if any(identity in seen_rows for identity in identities if identity is not None):
            reason = "overlapping_rows"
            break
        seen_rows.update(identity for identity in identities if identity is not None)
        if max_rows is None:
            rows.extend(batch)
        else:
            rows.extend(batch[:max_rows - len(rows)])
        pages.append({"page": page, "rows": len(batch), "cache": result["cache"],
                      "snapshot": result["snapshot"]})
        if legacy_sort_used and page < last_page:
            reason = "unstable_legacy_sort"
            break
        if max_rows is not None and len(rows) >= max_rows and page < last_page:
            reason = "preview_max_rows"
            break
        if page >= last_page:
            break
        page += 1
    complete = reason is None and total is not None and len(rows) == total
    if reason is None and not complete:
        reason = "row_count_mismatch"
    for item in pages:
        snapshot = Path(item["snapshot"])
        item["snapshot_saved_at"] = datetime.fromtimestamp(snapshot.stat().st_mtime, timezone.utc).isoformat() if snapshot.is_file() else None
    times = [p["snapshot_saved_at"] for p in pages if p["snapshot_saved_at"]]
    cached = sum(bool(p["cache"]) for p in pages)
    freshness = {"status": "unverified_cached" if pages and cached == len(pages) else "mixed_cached_and_live" if cached else "live_responses" if pages else "no_data",
                 "oldest_snapshot_saved_at": min(times) if times else None,
                 "newest_snapshot_saved_at": max(times) if times else None,
                 "definition": "File save timestamps, not the provider database date. Complete means page coverage; cached data are not freshly verified."}
    return {"provider": "keys", "action": action, "domain": domain, "base": base,
            "complete": complete, "incomplete_reason": reason,
            "freshness": freshness,
            "retry_after": retry_after,
            "coverage": {"rows_collected": len(rows), "total_reported": total,
                         "pages_collected": len(pages), "last_page_reported": last_page,
                         "preview": max_rows is not None,
                         "per_page": per_page, "sort": params["sort"]},
            "pages": pages, "data": rows}


def main(argv: list[str] | None = None) -> int:
    global ROOT, SNAP, LEDGER, ENV, LIVE_ALLOWED
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=[*ENDPOINTS, *KEYS_REPORTS])
    parser.add_argument("--domain")
    parser.add_argument("--base", choices=BASES, default="msk")
    parser.add_argument("--per-page", "--rows", type=int, default=25,
                        help="Keys.so request page size; this is not a total row cap")
    parser.add_argument("--max-rows", type=int, help="Explicit incomplete Keys.so preview")
    parser.add_argument("--date", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--term", default="")
    parser.add_argument("--refresh", action="store_true", help="Fresh limits request")
    parser.add_argument("--live", action="store_true", help="Allow provider requests on cache miss")
    parser.add_argument("--cache-root", type=Path,
                        default=Path.cwd() / ".offerist" / "cache" / "external")
    parser.add_argument("--env-file", type=Path,
                        help="Explicit local env file; otherwise process environment is used")
    parser.add_argument("--output", type=Path, help="Save assembled result including all rows; must be a new file")
    parser.add_argument("--ahrefs-budget-units", type=int,
                        help="Optional cap on ledgered actual Ahrefs units")
    parser.add_argument("--ahrefs-max-requests", type=int,
                        help="Required paid live Ahrefs attempt budget, including failures")
    args = parser.parse_args(argv)
    if args.output is not None and args.output.exists():
        parser.error("--output already exists; choose a new revision")
    ROOT, ENV, LIVE_ALLOWED = args.cache_root.resolve(), args.env_file, args.live
    SNAP, LEDGER = ROOT / "snapshots", ROOT / "budget-ledger.jsonl"
    if args.action not in {"keys-limits", "ahrefs-limits"}:
        if not args.domain:
            parser.error("--domain required")
        try:
            args.domain = valid_domain(args.domain)
        except ValueError:
            parser.error("--domain must be a valid hostname")
    if args.per_page < 1 or (args.max_rows is not None and args.max_rows < 1):
        parser.error("--per-page and --max-rows must be positive")
    if args.max_rows is not None and args.action not in KEYS_REPORTS:
        parser.error("--max-rows is only for Keys.so paginated reports")
    if args.term and args.action != "keys-keywords":
        parser.error("--term is only for keys-keywords")
    if args.refresh and args.action not in {"keys-limits", "ahrefs-limits"}:
        parser.error("--refresh is only for limits")
    if args.ahrefs_budget_units is not None and args.ahrefs_budget_units < 1:
        parser.error("--ahrefs-budget-units must be positive")
    if args.ahrefs_max_requests is not None and args.ahrefs_max_requests < 1:
        parser.error("--ahrefs-max-requests must be positive")
    if args.action in KEYS_REPORTS:
        try:
            result = collect_keys(args.action, args.domain, args.base,
                                  per_page=args.per_page, max_rows=args.max_rows,
                                  term=args.term)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        provider, endpoint, paid = ENDPOINTS[args.action]
        params = {}
        if args.action == "keys-dashboard":
            params = {"base": args.base, "domain": args.domain}
        elif args.action.startswith("ahrefs-") and args.action != "ahrefs-limits":
            params = {"date": args.date, "mode": "domain", "target": args.domain}
            if args.action == "ahrefs-metrics":
                params["country"] = "ru"
        result = invoke(provider, endpoint, params, paid=paid, row_cap=1,
                        refresh=args.refresh,
                        ahrefs_budget_units=args.ahrefs_budget_units,
                        ahrefs_max_requests=args.ahrefs_max_requests)
        if provider == "ahrefs":
            result["cumulative_cost_units_actual"] = _cumulative_ahrefs_units()
    output = args.output
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        output = ROOT / "reports" / (args.action + "-" + stamp + ".json")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    except (OSError, ValueError):
        parser.error("Cannot save result; choose a new writable --output path")
    result["output"] = str(output.resolve())
    result["output_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    print(json.dumps({key: value for key, value in result.items() if key != "data"},
                     ensure_ascii=False))
    if "error" in result:
        return 2
    if result.get("complete", True) or result.get("incomplete_reason") == "preview_max_rows":
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
