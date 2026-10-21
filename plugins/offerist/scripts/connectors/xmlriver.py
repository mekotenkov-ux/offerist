"""Bounded, cached XMLRiver SERP collection; credentials never leave memory."""
import argparse
import hashlib
import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from connections import xmlriver_credentials
from external import _ledger_lock

RETRYABLE = {"110", "111", "115", "202", "203", "500", "http_429", "http_502", "http_503", "http_504", "TimeoutError", "URLError"}
ENDPOINTS = {"yandex": "/search_yandex/xml", "google": "/search/xml"}


def parameters(engine, query, region_id, page=None):
    if engine not in ENDPOINTS or not query.strip() or len(query) > 1400 or type(region_id) is not int or region_id <= 0:
        raise ValueError("Require supported engine, query and positive region ID")
    first = 0 if engine == "yandex" else 1
    page = first if page is None else page
    if type(page) is not int or page < first:
        raise ValueError("Invalid page for search engine")
    params = {"query": query.strip(), "device": "desktop", "groupby": 10, "page": page}
    params.update({"lr": region_id, "lang": "ru", "domain": "ru"} if engine == "yandex"
                  else {"loc": region_id, "country": 2643, "lr": "RU"})
    return params


def parse(raw, offset=0):
    if len(raw) > 8 * 1024 * 1024 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        return {"status": "error", "error_code": "invalid_xml"}
    try:
        tree = ET.fromstring(raw)
    except ET.ParseError:
        return {"status": "error", "error_code": "invalid_xml"}
    error = tree.find(".//error")
    if error is not None:
        code = error.get("code", "unknown")
        return {"status": "empty" if code == "15" else "error", "error_code": code, "rows": []}
    docs = tree.findall(".//results/grouping/group/doc")
    if not docs:
        return {"status": "error", "error_code": "missing_organic_results", "rows": []}
    rows = []
    for doc in docs:
        url = doc.findtext("url", "")
        if not url.startswith(("https://", "http://")):
            return {"status": "error", "error_code": "invalid_result_url", "rows": []}
        rows.append({"rank": offset + len(rows) + 1, "url": url,
                     "title": "".join(doc.find("title").itertext()) if doc.find("title") is not None else ""})
    return {"status": "ok", "rows": rows}


def _download(endpoint, params, auth):
    # Never return/log request URLs or exception strings: both can contain keys.
    try:
        request = Request("https://xmlriver.com" + endpoint + "?" + urlencode({**params, **auth}), headers={"User-Agent": "Offerist/1.0"})
        with urlopen(request, timeout=65) as response:
            return response.read(8 * 1024 * 1024 + 1)
    except Exception as exc:
        return {"status": "error", "error_code": "http_" + str(exc.code) if hasattr(exc, "code") else type(exc).__name__}


def collect(engine, query, region_id, *, cache_root, max_attempts, region_name="", page=None,
            attempts_per_query=4, live=False, env_file=None):
    params = parameters(engine, query, region_id, page)
    if type(max_attempts) is not int or max_attempts <= 0 or not 1 <= attempts_per_query <= 6:
        raise ValueError("Positive total budget and 1..6 attempts per query required")
    root = Path(cache_root)
    endpoint = ENDPOINTS[engine]
    cache_id = hashlib.sha256(json.dumps([endpoint, params], sort_keys=True).encode()).hexdigest()[:24]
    dest, ledger = root / (cache_id + ".json"), root / "ledger.jsonl"
    with _ledger_lock(root):
        if dest.exists():
            result = json.loads(dest.read_text(encoding="utf-8"))
            return {**result, "cache": True}
        if not live:
            raise ValueError("Network requires --live and explicit total attempt budget")
        auth = xmlriver_credentials(env_file)
        ledger_text = ledger.read_text(encoding="utf-8") if ledger.exists() else ""
        if ledger_text and not ledger_text.endswith("\n"):
            raise ValueError("Incomplete budget ledger; preserve reservations before repair")
        reservations = [json.loads(line) for line in ledger_text.splitlines()]
        if any(row.get("attempt") != i + 1 for i, row in enumerate(reservations)):
            raise ValueError("Inconsistent budget ledger; do not reset reservations")
        attempts = len(reservations)
        result = {"status": "error", "error_code": "budget_exhausted", "rows": []}
        attempt_results = []
        for retry in range(attempts_per_query):
            if attempts >= max_attempts:
                break
            attempts += 1
            stamp = datetime.now(timezone.utc).isoformat()
            reservation = {"attempt": attempts, "timestamp_utc": stamp, "engine": engine,
                           "endpoint": endpoint, "params": params, "network_attempt": True}
            with ledger.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(reservation, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            raw = _download(endpoint, params, auth)
            first = 0 if engine == "yandex" else 1
            result = raw if isinstance(raw, dict) else parse(raw, (params["page"] - first) * 10)
            attempt_results.append({"attempt": attempts, "status": result["status"], "error_code": result.get("error_code")})
            result.update({"engine": engine, "query": params["query"], "region_id": region_id,
                           "region_name": region_name, "region_scope": "requested API location",
                           "retrieved_utc": stamp, "params": params, "endpoint": endpoint,
                           "attempts_used": attempts, "attempt_results": attempt_results, "cache": False})
            if result["status"] in {"ok", "empty"}:
                temporary = dest.with_suffix(".tmp")
                temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(dest)
                break
            if result.get("error_code") not in RETRYABLE or retry + 1 == attempts_per_query or attempts >= max_attempts:
                break
            time.sleep((2, 5, 10, 15, 20)[retry])
        # Errors are evidence of unavailable data, never cached successful empty SERPs.
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=ENDPOINTS, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--region-id", type=int, required=True)
    parser.add_argument("--region-name", default="")
    parser.add_argument("--page", type=int)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--max-attempts", type=int, required=True)
    parser.add_argument("--attempts-per-query", type=int, default=4)
    parser.add_argument("--xmlriver-env-file", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = collect(args.engine, args.query, args.region_id, cache_root=args.cache_root,
                         max_attempts=args.max_attempts, region_name=args.region_name, page=args.page,
                         attempts_per_query=args.attempts_per_query, live=args.live, env_file=args.xmlriver_env_file)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: result.get(k) for k in ("status", "error_code", "engine", "region_id", "attempts_used", "cache")} | {"rows": len(result.get("rows", [])), "output": str(args.output)}, ensure_ascii=False))
        return 0 if result["status"] in {"ok", "empty"} else 2
    except Exception as exc:
        print(json.dumps({"status": "error", "error_class": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
