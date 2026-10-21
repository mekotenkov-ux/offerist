"""Join a structural URL inventory to additive landing metrics; preserve unknowns."""
import argparse
import csv
import json
import math
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, unquote, urlsplit, urlunsplit

TYPES = ("category", "product", "tag_filter", "blog_article", "blog_listing", "other", "unknown")
PREFIXES = ("organic_visits_", "google_clicks_", "google_impressions_")


def identity(url):
    parsed = urlsplit(url)
    params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
              if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "yclid", "ysclid", "fbclid", "sort", "view"}]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), unquote(parsed.path).rstrip("/") or "/", urlencode(sorted(params)), ""))


def analyze(inventory, traffic):
    types = {}
    for row in inventory:
        kind = row["page_type"]
        if kind not in TYPES:
            raise ValueError("Page type must describe structure, not a topic: " + kind)
        key = identity(row["url"])
        if key in types and types[key] != kind:
            raise ValueError("Conflicting structural types: " + key)
        types[key] = kind
    metrics = sorted({name for row in traffic for name in row if name.startswith(PREFIXES)})
    groups = {kind: {"page_type": kind, "inventory_urls": 0, "observed_landing_urls": 0,
                     **{metric: None for metric in metrics}} for kind in TYPES}
    for kind in types.values():
        groups[kind]["inventory_urls"] += 1
    detail, seen = [], set()
    for row in traffic:
        key = identity(row["url"])
        if key in seen:
            raise ValueError("Traffic must contain one row per normalized URL: " + key)
        seen.add(key)
        kind = types.get(key, "unknown")
        output = {"url": row["url"], "page_type": kind}
        groups[kind]["observed_landing_urls"] += 1
        for metric in metrics:
            raw = row.get(metric)
            if raw is None or raw == "":
                output[metric] = None
                continue
            value = float(raw)
            if not math.isfinite(value) or value < 0 or int(value) != value:
                raise ValueError("Additive metrics must be finite nonnegative integers")
            value = int(value)
            output[metric] = value
            groups[kind][metric] = (groups[kind][metric] or 0) + value
        detail.append(output)
    return list(groups.values()), detail


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
    parser.add_argument("--traffic", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--details", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.details.exists() or args.output.resolve() == args.details.resolve():
        parser.error("Use two distinct new output paths")
    grouped, detail = analyze(read_csv(args.inventory), read_csv(args.traffic))
    if not detail:
        parser.error("Traffic is empty; do not publish zero metrics as observations")
    write_csv(args.output, grouped)
    write_csv(args.details, detail)
    print(json.dumps({"inventory_urls": sum(row["inventory_urls"] for row in grouped),
                      "landing_urls": len(detail), "unknown_landings": next(row for row in grouped if row["page_type"] == "unknown")["observed_landing_urls"]}))


if __name__ == "__main__":
    main()
