"""Cached-first read-only collection of three internal search sources."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path

WORK = Path(__file__).resolve().parent
CACHE_SCHEMA = "internal-bridges-v2-pagination"
SOURCE_METHOD_VERSION = {"gsc": "searchanalytics-startrow-v2", "metrika": "stat-offset-v2",
                         "wm": "popular-offset-and-totals-v2"}


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def host(value: str) -> str:
    value = str(value or "").strip()
    if value.startswith("sc-domain:"):
        value = value[len("sc-domain:"):]
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urllib.parse.urlsplit(value)
    return (parsed.hostname or "").lower().rstrip(".")


def canonical_host(value: str) -> str:
    found = host(value)
    return found.removeprefix("www.")


def counter_sites(counter: dict) -> list[str]:
    values: list[str] = []
    for key in ("site", "site2"):
        value = counter.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            values.append(str(value.get("site") or ""))
    for key in ("mirrors", "mirrors2"):
        for value in counter.get(key) or []:
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, dict):
                values.append(str(value.get("site") or ""))
    return sorted(set(filter(None, values)))


def gsc_covers(property_url: str, target_host: str) -> tuple[bool, str]:
    """Respect domain-property coverage and URL-prefix path/scheme bounds."""
    if property_url.startswith("sc-domain:"):
        base = host(property_url)
        covered = target_host == base or target_host.endswith(f".{base}")
        return covered, "domain_property" if covered else "none"
    parsed = urllib.parse.urlsplit(property_url)
    if parsed.scheme not in ("http", "https") or (parsed.path not in ("", "/")):
        return False, "folder_prefix_not_sitewide"
    prop_host = host(property_url)
    if prop_host == target_host:
        return True, "exact_url_prefix"
    if canonical_host(prop_host) == canonical_host(target_host):
        return True, "www_variant_requires_mirror_check"
    return False, "none"


def candidates(gsc_sites: list[dict], wm_hosts: list[dict], counters: list[dict]) -> list[dict]:
    result: list[dict] = []
    for counter in counters:
        if str(counter.get("status", "Active")).lower() == "deleted":
            continue
        for met_site in counter_sites(counter):
            met_host = host(met_site)
            if not met_host:
                continue
            for wm in wm_hosts:
                if wm.get("verified") is not True:
                    continue
                wm_url = str(wm.get("url") or "")
                wm_host = host(wm_url)
                if canonical_host(wm_host) != canonical_host(met_host):
                    continue
                for gsc in gsc_sites:
                    prop = str(gsc.get("siteUrl") or "")
                    if gsc.get("permissionLevel") == "siteUnverifiedUser":
                        continue
                    covered, scope = gsc_covers(prop, met_host)
                    if not covered:
                        continue
                    exact = met_host == wm_host and scope in ("domain_property", "exact_url_prefix")
                    result.append({
                        "domain": canonical_host(met_host),
                        "metrika_counter_id": counter.get("id"),
                        "metrika_site": met_site,
                        "wm_host_id": wm.get("host_id"),
                        "wm_url": wm_url,
                        "gsc_property": prop,
                        "gsc_scope": scope,
                        "match_confidence": "high" if exact else "needs_mirror_review",
                    })
    unique = {json.dumps(row, sort_keys=True, ensure_ascii=False): row for row in result}
    return sorted(unique.values(), key=lambda x: (x["domain"], str(x["metrika_counter_id"]), x["gsc_property"]))


def choose_candidate(items: list[dict], args: argparse.Namespace) -> tuple[dict | None, str]:
    filtered = items
    if args.site:
        filtered = [x for x in filtered if x["domain"] == canonical_host(args.site)]
    if args.counter_id:
        filtered = [x for x in filtered if str(x["metrika_counter_id"]) == args.counter_id]
    if args.gsc_property:
        filtered = [x for x in filtered if x["gsc_property"] == args.gsc_property]
    if args.wm_host_id:
        filtered = [x for x in filtered if x["wm_host_id"] == args.wm_host_id]
    if len(filtered) == 1 and filtered[0]["match_confidence"] == "high":
        return filtered[0], "selected"
    if not filtered:
        return None, "no_common_property"
    return None, "ambiguous_or_mirror_review_needed"


def select_source(items: list[dict], key: str, preferred: str | None = None) -> dict:
    """Choose only one distinct, confirmed identity; never break ties by ordering."""
    if preferred is not None:
        items = [item for item in items if str(item[key]) == preferred]
    identities = {str(item[key]): item for item in items}
    if len(identities) == 1:
        return {"status": "selected", "selected": next(iter(identities.values())),
                "candidate_count": 1}
    return {"status": "ambiguous" if identities else "not_found",
            "selected": None, "candidate_count": len(identities)}


def select_site_sources(args: argparse.Namespace, data: dict) -> dict:
    """Select each available source independently for an explicit target site."""
    target = canonical_host(args.site)
    result = {}
    if "sites" in data:
        gsc = []
        for row in data["sites"].get("sites", []):
            prop = str(row.get("siteUrl") or "")
            covered, scope = gsc_covers(prop, target)
            if row.get("permissionLevel") != "siteUnverifiedUser" and covered:
                gsc.append({"gsc_property": prop, "gsc_scope": scope,
                            "match_confidence": "high" if scope in ("domain_property", "exact_url_prefix")
                            and (prop.startswith("sc-domain:") or host(prop) == target) else "needs_mirror_review"})
        confident = [row for row in gsc if row["match_confidence"] == "high"]
        result["gsc"] = select_source(confident, "gsc_property", args.gsc_property)
        if not confident and gsc:
            result["gsc"]["status"] = "mirror_review_needed"
    else:
        result["gsc"] = {"status": "unknown", "selected": None, "candidate_count": None}

    if "hosts" in data:
        hosts = [{"wm_host_id": row.get("host_id"), "wm_url": row.get("url")}
                 for row in data["hosts"].get("hosts", [])
                 if row.get("verified") is True and row.get("host_id")
                 and canonical_host(row.get("url")) == target]
        result["wm"] = select_source(hosts, "wm_host_id", args.wm_host_id)
    else:
        result["wm"] = {"status": "unknown", "selected": None, "candidate_count": None}

    if "counters" in data:
        counters = [{"metrika_counter_id": row.get("id"), "metrika_sites": counter_sites(row)}
                    for row in data["counters"].get("counters", [])
                    if row.get("id") is not None
                    and str(row.get("status", "Active")).lower() != "deleted"
                    and any(canonical_host(site) == target for site in counter_sites(row))]
        result["metrika"] = select_source(counters, "metrika_counter_id", args.counter_id)
        if data["counters"].get("incomplete") or data["counters"].get("truncated"):
            result["metrika"] = {"status": "inventory_truncated", "selected": None,
                                 "candidate_count": result["metrika"]["candidate_count"]}
    else:
        result["metrika"] = {"status": "unknown", "selected": None, "candidate_count": None}
    return result


def periods(end_text: str | None) -> dict:
    end = dt.date.fromisoformat(end_text) if end_text else dt.date.today() - dt.timedelta(days=3)
    if end >= dt.date.today():
        raise ValueError("end_date_must_be_a_completed_day")
    current_start = end - dt.timedelta(days=27)
    previous_end = current_start - dt.timedelta(days=1)
    previous_start = previous_end - dt.timedelta(days=27)
    return {
        "current": (current_start.isoformat(), end.isoformat()),
        "previous": (previous_start.isoformat(), previous_end.isoformat()),
    }


class Cache:
    """Shared snapshots and append-only request ledger across output directories."""

    def __init__(self, root: Path, connection_name: str, live: bool):
        self.root = root.resolve()
        self.name = connection_name
        self.live = live
        self.snapshots = self.root / "snapshots"
        self.ledger = self.root / "request-ledger.jsonl"
        self.lock = self.root / ".lock"

    def key(self, source: str, operation: str, params: dict) -> str:
        identity = {"schema": CACHE_SCHEMA, "method_version": SOURCE_METHOD_VERSION[source],
                    "connection_name": self.name, "source": source,
                    "operation": operation, "params": params}
        return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def history(self) -> list[dict]:
        if not self.ledger.exists():
            return []
        return [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines() if line]

    @staticmethod
    def retryable(data: dict) -> bool:
        reason = (data.get("coverage") or {}).get("stop_reason")
        return reason in {"provider_error", "repeated_page", "duplicate_rows",
                          "invalid_provider_page", "total_changed", "count_changed",
                          "invalid_provider_count"}

    def get(self, source: str, operation: str, params: dict, group: str, request, preflight=None) -> tuple[dict, bool]:
        key = self.key(source, operation, params)
        snapshot = self.snapshots / f"{key}.json"
        if snapshot.is_file():
            cached = read_json(snapshot)["data"]
            if not (self.live and self.retryable(cached)):
                return cached, True
        if not self.live:
            raise RuntimeError("cache_miss")
        self.root.mkdir(parents=True, exist_ok=True)
        # The lock covers budget check, network attempt and snapshot commit.
        deadline = time.monotonic() + 30
        while True:
            try:
                fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("cache_lock_busy")
                time.sleep(0.1)
        try:
            if snapshot.is_file():
                cached = read_json(snapshot)["data"]
                if not self.retryable(cached):
                    return cached, True
            if preflight:
                preflight()
            self.snapshots.mkdir(parents=True, exist_ok=True)
            row = {"event": "attempt", "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "connection_name": self.name, "source": source, "operation": operation,
                   "params": params, "group": group, "budget_units": 1, "key": key}
            with self.ledger.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
            data = request()
            if not isinstance(data, dict):
                raise RuntimeError("invalid_bridge_result")
            if group == "analytical" and not isinstance(data.get("rows"), list):
                raise RuntimeError("invalid_report_rows")
            requested_data = data
            kept_cached = False
            # Keep the ledger and archive a failed partial snapshot before replacing it.
            pending = snapshot.with_suffix(".tmp")
            try:
                write_json(pending, {"identity": {"schema": CACHE_SCHEMA,
                                                  "method_version": SOURCE_METHOD_VERSION[source],
                                                  "connection_name": self.name, "source": source,
                                                  "operation": operation, "params": params}, "data": data})
                if snapshot.is_file():
                    old_data = read_json(snapshot)["data"]
                    old_rows = old_data.get("rows", old_data.get("counters", []))
                    new_rows = data.get("rows", data.get("counters", []))
                    if self.retryable(data) and len(new_rows) < len(old_rows):
                        pending.unlink(missing_ok=True)
                        data = old_data
                        kept_cached = True
                    else:
                        archived = self.snapshots / f"{key}.prior-{uuid.uuid4().hex}.json"
                        shutil.copy2(snapshot, archived)
                        pending.replace(snapshot)
                else:
                    pending.replace(snapshot)
            finally:
                pending.unlink(missing_ok=True)
            with self.ledger.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": "result", "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                                         "connection_name": self.name, "key": key,
                                         "api_pages": requested_data.get("api_requests_attempted",
                                                                         requested_data.get("pages_fetched", 1)),
                                         "incomplete": bool(requested_data.get("incomplete")),
                                         "truncated": bool(requested_data.get("truncated")),
                                         "retained_prior_snapshot": kept_cached},
                                        sort_keys=True, ensure_ascii=False) + "\n")
            return data, kept_cached
        finally:
            self.lock.unlink(missing_ok=True)


def run_bridge(command: list[str], cache_dir: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="bridge-", dir=cache_dir) as temp:
        output = Path(temp) / "result.json"
        try:
            completed = subprocess.run(command + ["--output", str(output)], capture_output=True,
                                       text=True, check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError("bridge_timeout") from None
        if completed.returncode != 0 or not output.is_file():
            try:
                status = json.loads(completed.stdout.strip().splitlines()[-1])
                code = status.get("error_code") or "bridge_failed"
            except (IndexError, ValueError):
                code = "bridge_failed"
            if not re.fullmatch(r"[A-Za-z0-9_:-]{1,100}", str(code)):
                code = "bridge_failed"
            raise RuntimeError(str(code))
        return read_json(output)


def bridge_request(args, cache: Cache, source: str, operation: str, params: dict,
                   group: str, *options: str) -> tuple[dict, bool]:
    command = None

    def preflight() -> None:
        nonlocal command
        if source != "gsc" and not args.connection_dir and not args.yandex_env_file:
            raise RuntimeError("connection_dir_required")
        connection = args.connection_dir.resolve() if args.connection_dir else None
        if source == "gsc":
            client_file = args.gsc_client_file or (Path(os.environ["GSC_OAUTH_CLIENT_FILE"])
                                                   if os.environ.get("GSC_OAUTH_CLIENT_FILE") else None)
            token_file = args.gsc_token_file or (Path(os.environ["GSC_TOKEN_FILE"])
                                                 if os.environ.get("GSC_TOKEN_FILE") else None)
            if token_file is None and connection is not None:
                token_file = connection / "gsc-token.json"
            if client_file is None or token_file is None:
                raise RuntimeError("gsc_credential_paths_required")
            client_file, token_file = client_file.resolve(), token_file.resolve()
            if not client_file.is_file() or not token_file.is_file():
                raise RuntimeError("gsc_credentials_unavailable")
            node = args.node or shutil.which("node")
            if not node:
                raise RuntimeError("node_unavailable")
            command = [str(node), str(WORK / "gsc_bridge.mjs"), "--client-file", str(client_file),
                       "--token-file", str(token_file), *options]
        elif source == "wm":
            env = args.yandex_env_file.resolve() if args.yandex_env_file else connection / ".env.yandex"
            if not env.is_file():
                raise RuntimeError("yandex_env_unavailable")
            command = [sys.executable, str(WORK / "wm_bridge.py"), "--env-file", str(env), *options]
        else:
            env = args.yandex_env_file.resolve() if args.yandex_env_file else connection / ".env.yandex"
            if not env.is_file():
                raise RuntimeError("yandex_env_unavailable")
            command = [sys.executable, str(WORK / "metrika_bridge.py"), "--env-file", str(env), *options]

    def request() -> dict:
        return run_bridge(command, cache.root)

    return cache.get(source, operation, params, group, request, preflight)


def fetch(args, cache: Cache, out: Path, source: str, operation: str, params: dict,
          group: str, filename: str, *options: str) -> dict:
    data, from_cache = bridge_request(args, cache, source, operation, params, group, *options)
    write_json(out / "private" / filename, data)
    return {"data": data, "cache": from_cache}


def discover(args, cache: Cache, out: Path) -> tuple[dict, dict]:
    sources: dict[str, dict] = {}
    data: dict[str, dict] = {}

    def attempt(source, operation, params, filename, *options):
        try:
            found = fetch(args, cache, out, source, operation, params, "discovery", filename, *options)
            sources[operation] = {"status": "partial" if found["data"].get("incomplete") else "ok",
                                  "cache": found["cache"], "coverage": found["data"].get("coverage")}
            data[operation] = found["data"]
        except (RuntimeError, OSError, ValueError) as exc:
            code = str(exc)
            sources[operation] = {"status": "unavailable", "reason": code if re.fullmatch(r"[A-Za-z0-9_:-]{1,100}", code) else type(exc).__name__}

    attempt("gsc", "sites", {"site": args.site}, "gsc-sites.json", "--mode", "sites", "--site", args.site)
    attempt("wm", "user", {}, "wm-user.json", "--mode", "user")
    if "user" in data:
        user_id = str(data["user"]["user_id"])
        attempt("wm", "hosts", {"user_id": user_id}, "wm-hosts.json",
                "--mode", "hosts", "--user-id", user_id)
    else:
        sources["hosts"] = {"status": "unavailable", "reason": "wm_user_unavailable"}
    search_domain = args.search_domain or (canonical_host(args.site) if args.site else None)
    counter_params = {"search_string": search_domain} if search_domain else {}
    counter_options = ("--search-string", search_domain) if search_domain else ()
    attempt("metrika", "counters", counter_params, "metrika-counters.json",
            "--mode", "counters", *counter_options)
    if all(key in data for key in ("sites", "hosts", "counters")):
        matches = candidates(data["sites"]["sites"], data["hosts"]["hosts"], data["counters"]["counters"])
        selected, reason = choose_candidate(matches, args)
        if data["counters"].get("incomplete") or data["counters"].get("truncated"):
            selected, reason = None, "counter_inventory_truncated"
        selection = {"status": reason, "candidate_count": len(matches), "candidates": matches, "selected": selected}
    else:
        selection = {"status": "common3_unknown", "candidate_count": None, "candidates": [], "selected": None}
    if args.site:
        selection["sources"] = select_site_sources(args, data)
        chosen = selection["sources"]
        if all(chosen[source]["status"] == "selected" for source in ("gsc", "wm", "metrika")):
            selection["selected"] = {key: value for source in ("gsc", "wm", "metrika")
                                     for key, value in chosen[source]["selected"].items()}
            selection["status"] = "selected"
        else:
            selection["selected"] = None
            selection["status"] = "partial_sources"
    elif selection["selected"]:
        selection["sources"] = {
            "gsc": {"status": "selected", "selected": {"gsc_property": selected["gsc_property"]}},
            "wm": {"status": "selected", "selected": {"wm_host_id": selected["wm_host_id"]}},
            "metrika": {"status": "selected", "selected": {"metrika_counter_id": selected["metrika_counter_id"]}},
        }
    write_json(out / "selection.json", selection)
    summary = {"mode": "live" if args.live else "cached", "action": "discover",
               "connection_name": args.connection_name, "sources": sources,
               "common3": "confirmed" if selection["selected"] else "unknown",
               "selection": selection["status"]}
    write_json(out / "collection-summary.json", summary)
    return data, summary


def collect(args, cache: Cache, out: Path, discovered: dict, summary: dict) -> dict:
    selection = read_json(out / "selection.json")
    selected = selection["selected"]
    source_selection = selection.get("sources") or {}
    if not any(item["status"] == "selected" for item in source_selection.values()):
        summary["action"] = "collect"
        summary["status"] = "needs_selection_or_sources"
        summary["scope"] = "internal_search_baseline_reports_not_technical_audit"
        summary["baseline_reports_collected"] = False
        summary["provider_collection_exhausted"] = False
        summary["analytical_complete"] = False
        summary["source_selection"] = source_selection
        write_json(out / "collection-summary.json", summary)
        return summary
    windows = periods(args.end_date)
    reports: dict[str, dict] = {}

    def report(source, operation, params, filename, *options):
        try:
            found = fetch(args, cache, out, source, operation, params, "analytical", filename, *options)
            item = found["data"]
            stop = (item.get("coverage") or {}).get("stop_reason")
            partial = bool(item.get("truncated")) or stop not in (None, "api_exhausted", "reported_total_reached", "reported_count_reached")
            status = "partial" if partial else "limited" if item.get("incomplete") else "ok"
            reports[operation] = {"status": status, "cache": found["cache"],
                                  "rows": len(item["rows"]), "incomplete": bool(item.get("incomplete")),
                                  "truncated": bool(item.get("truncated")), "coverage": item.get("coverage"),
                                  "pages_fetched": item.get("pages_fetched")}
        except (RuntimeError, OSError, ValueError) as exc:
            code = str(exc)
            reports[operation] = {"status": "unavailable", "reason": code if re.fullmatch(r"[A-Za-z0-9_:-]{1,100}", code) else type(exc).__name__}

    gsc_choice = source_selection.get("gsc", {}).get("selected")
    if gsc_choice:
        for label, (start, end) in windows.items():
            for dimension in ("total", "page", "query"):
                operation = f"gsc_{label}_{dimension}"
                params = {"property": gsc_choice["gsc_property"], "date_from": start, "date_to": end,
                          "dimensions": dimension, "preview_rows": args.preview_rows,
                          "landing_path_contains": args.landing_path_contains,
                          "query_contains": args.query_contains}
                options = ["--mode", "queries", "--site", gsc_choice["gsc_property"],
                           "--date-from", start, "--date-to", end, "--dimensions", dimension]
                if args.preview_rows:
                    options += ["--preview-rows", str(args.preview_rows)]
                if args.landing_path_contains:
                    options += ["--page-contains", args.landing_path_contains]
                if args.query_contains:
                    options += ["--query-contains", args.query_contains]
                if args.segment_label:
                    options += ["--segment-label", args.segment_label]
                report("gsc", operation, params, f"{operation}.json", *options)
    wm_choice = source_selection.get("wm", {}).get("selected")
    if wm_choice:
        user_id = str(discovered["user"]["user_id"])
        wm_end = dt.date.fromisoformat(windows["current"][1])
        wm_start = wm_end - dt.timedelta(days=13)
        report("wm", "wm_current_14d", {"user_id": user_id, "host_id": wm_choice["wm_host_id"],
               "date_from": wm_start.isoformat(), "date_to": wm_end.isoformat(), "preview_rows": args.preview_rows},
               "wm-current-14d.json", "--mode", "queries", "--user-id", user_id,
               "--host-id", wm_choice["wm_host_id"], "--date-from", wm_start.isoformat(),
               "--date-to", wm_end.isoformat(),
               *(["--preview-rows", str(args.preview_rows)] if args.preview_rows else []))
        report("wm", "wm_current_totals", {"user_id": user_id, "host_id": wm_choice["wm_host_id"],
               "date_from": wm_start.isoformat(), "date_to": wm_end.isoformat()},
               "wm-current-totals.json", "--mode", "totals", "--user-id", user_id,
               "--host-id", wm_choice["wm_host_id"], "--date-from", wm_start.isoformat(),
               "--date-to", wm_end.isoformat())
    metrika_choice = source_selection.get("metrika", {}).get("selected")
    goal_id = None
    goal_status = "unknown"
    if metrika_choice:
        counter_id = str(metrika_choice["metrika_counter_id"])
        report_domain = canonical_host(args.site) if args.site else selected.get("domain") if selected else None
        try:
            detail = fetch(args, cache, out, "metrika", "counter_detail", {"counter_id": counter_id},
                           "discovery", "metrika-goals.json", "--mode", "counter-detail", "--counter-id", counter_id)["data"]
            goals = detail.get("goals") or []
            if args.goal_id and args.goal_id not in {str(goal["id"]) for goal in goals}:
                raise RuntimeError("requested_goal_not_available")
            goal_id = args.goal_id
            goal_status = "confirmed" if goal_id else "not_requested"
        except (RuntimeError, OSError, ValueError) as exc:
            goal_status = str(exc) if re.fullmatch(r"[A-Za-z0-9_:-]{1,100}", str(exc)) else type(exc).__name__
            if args.goal_id:
                metrika_choice = None
        if metrika_choice:
            for label, (start, end) in windows.items():
                for grouping in ("total", "search-engine", "landing-device"):
                    operation = f"metrika_{label}_{grouping}"
                    params = {"counter_id": counter_id, "date_from": start, "date_to": end,
                              "grouping": grouping, "goal_id": goal_id,
                              "preview_rows": args.preview_rows,
                              "landing_path_contains": args.landing_path_contains}
                    options = ["--mode", "report", "--counter-id", counter_id, "--date-from", start,
                               "--date-to", end, "--grouping", grouping]
                    if report_domain:
                        params["site_domain"] = report_domain
                        options += ["--site-domain", report_domain]
                    if goal_id:
                        options += ["--goal-id", goal_id]
                    if args.preview_rows:
                        options += ["--preview-rows", str(args.preview_rows)]
                    if args.landing_path_contains:
                        options += ["--landing-path-contains", args.landing_path_contains]
                    if args.segment_label:
                        options += ["--segment-label", args.segment_label]
                    report("metrika", operation, params, f"{operation}.json", *options)
    expected = {f"gsc_{label}_{dimension}" for label in windows for dimension in ("total", "page", "query")}
    expected |= {"wm_current_14d", "wm_current_totals"}
    expected |= {f"metrika_{label}_{grouping}" for label in windows
                 for grouping in ("total", "search-engine", "landing-device")}
    baseline_ready = bool(selected) and set(reports) == expected and all(
        x["status"] in ("ok", "limited") for x in reports.values())
    any_limited = any(x["status"] == "limited" for x in reports.values())
    status = ("baseline_reports_collected_with_limits" if any_limited else "baseline_reports_collected") \
        if baseline_ready else "partial"
    summary.update({"action": "collect", "status": status,
                    "baseline_reports_collected": baseline_ready,
                    "provider_collection_exhausted": baseline_ready,
                    "analytical_complete": baseline_ready and not any_limited,
                    "scope": "internal_search_baseline_reports_not_technical_audit",
                    "selected": selected, "periods": windows, "reports": reports,
                    "source_selection": source_selection, "goal_status": goal_status,
                    "goal_id": goal_id,
                    "goal_interpretation": "Goal visits are visits with the configured goal reached, not goal completion counts or confirmed CRM leads."})
    write_json(out / "collection-summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("discover", "collect"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path.cwd() / ".offerist" / "cache" / "internal")
    parser.add_argument("--connection-name", required=True, help="Stable non-secret account label; use a different label for each account")
    parser.add_argument("--live", action="store_true", help="Allow read-only requests on cache misses")
    parser.add_argument("--connection-dir", type=Path, help="Directory with gsc-token.json and .env.yandex")
    parser.add_argument("--gsc-client-file", type=Path, help="Existing OAuth client JSON; overrides GSC_OAUTH_CLIENT_FILE")
    parser.add_argument("--gsc-token-file", type=Path, help="Existing GSC token JSON; overrides GSC_TOKEN_FILE and --connection-dir")
    parser.add_argument("--yandex-env-file", type=Path, help="Existing Yandex .env; overrides --connection-dir")
    parser.add_argument("--node", help="Node executable, if not on PATH")
    parser.add_argument("--site")
    parser.add_argument("--search-domain", help="Target domain for Metrika counter search_string")
    parser.add_argument("--counter-id")
    parser.add_argument("--gsc-property")
    parser.add_argument("--wm-host-id")
    parser.add_argument("--goal-id")
    parser.add_argument("--end-date")
    parser.add_argument("--preview-rows", type=int, help="Explicit incomplete report preview")
    parser.add_argument("--landing-path-contains", help="Explicit segment filter, not an intent classifier")
    parser.add_argument("--query-contains", help="Optional GSC query filter")
    parser.add_argument("--segment-label", help="User-defined label; does not prove search intent")
    args = parser.parse_args()
    if not args.site or not canonical_host(args.site):
        parser.error("--site is required for an authorized target")
    if args.preview_rows is not None and args.preview_rows < 1:
        parser.error("--preview-rows must be positive")
    if args.landing_path_contains and not re.fullmatch(r"/[A-Za-z0-9/_-]{1,200}", args.landing_path_contains):
        parser.error("--landing-path-contains must be a simple URL path")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", args.connection_name):
        parser.error("--connection-name must be a simple non-secret label")
    if args.live and not (args.connection_dir or args.gsc_token_file or args.yandex_env_file or
                          os.environ.get("GSC_TOKEN_FILE")):
        parser.error("--live requires --connection-dir or a credential file path")
    if args.search_domain and (not canonical_host(args.search_domain) or
                               (args.site and canonical_host(args.search_domain) != canonical_host(args.site))):
        parser.error("--search-domain must be a valid domain matching --site when both are given")
    if args.search_domain:
        args.search_domain = canonical_host(args.search_domain)
    out, cache_dir = args.output.resolve(), args.cache_dir.resolve()
    if out == cache_dir or out.is_relative_to(cache_dir) or cache_dir.is_relative_to(out):
        parser.error("--output and --cache-dir must be separate")
    if args.connection_dir and (out == args.connection_dir.resolve() or out.is_relative_to(args.connection_dir.resolve())):
        parser.error("--output must not be inside --connection-dir")
    if out.exists() and any(out.iterdir()):
        parser.error("--output must be new or empty")
    cache = Cache(cache_dir, args.connection_name, args.live)
    out.mkdir(parents=True, exist_ok=True)
    discovered, summary = discover(args, cache, out)
    if args.action == "collect":
        summary = collect(args, cache, out, discovered, summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

