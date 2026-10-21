#!/usr/bin/env python3
"""Bounded ecommerce inventory: sitemaps, category tree, linked tags, template samples.

Counts describe discovered public URLs, never search-engine indexation. No product crawl.
Usage: python site_inventory.py --site https://example.org --out work/inventory
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import gzip
import hashlib
from html import unescape
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit, unquote, quote
from urllib.request import Request, HTTPRedirectHandler, build_opener
import xml.etree.ElementTree as ET


TYPES = ("category", "product", "tag_filter", "blog_article", "blog_listing", "other", "unknown")
TRACKING = {"gclid", "yclid", "fbclid", "_openstat", "ysclid", "ymclid"}
ACTION = {"action", "add_to_basket", "add2basket", "buy", "logout", "login", "clear_cache", "bxajaxid", "q"}
DISPLAY = {"sort", "view", "order", "order_by", "by", "how", "sphrase_id"}
ASSETS = re.compile(r"\.(?:jpg|jpeg|png|gif|webp|svg|css|js|pdf|zip|woff2?|ico|xml)$", re.I)
MAX_BYTES = 8 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def allowed_fetch(url, site):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme in {"http", "https"} and parsed.hostname == urlsplit(site).hostname
                and parsed.port in {None, 80, 443} and not parsed.username and not parsed.password
                and not any(ord(char) < 32 for char in url))
    except ValueError:
        return False


class SiteRedirectHandler(HTTPRedirectHandler):
    def __init__(self, site):
        self.site = site

    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not allowed_fetch(newurl, self.site):
            raise ValueError("redirect outside authorized public site")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def normalize_url(value, base):
    """Drop fragments and known tracking, preserve meaningful unknown query parameters."""
    try:
        parsed, origin = urlsplit(urljoin(base, unescape(value.strip()))), urlsplit(base)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.hostname != origin.hostname:
        return None
    if parsed.username or parsed.password or port not in {None, 80, 443}:
        return None
    params = parse_qsl(parsed.query, keep_blank_values=True)
    if any(k.lower() in ACTION or k.lower().startswith("pagen_") for k, _ in params):
        return None
    params = [(k, v) for k, v in params if k.lower() not in TRACKING | DISPLAY and not k.lower().startswith("utm_")]
    path = quote(unquote(parsed.path or "/"), safe="/:@-._~!$&'()*+,;=%")
    if "/filter/clear/apply/" in path:
        return None
    if ASSETS.search(path) or path.startswith(("/bitrix/", "/local/", "/upload/", "/moy-kabinet/", "/personal/", "/login/")):
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, urlencode(sorted(params)), ""))


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.links, self.canonical, self.meta, self.h1, self.title = [], None, {}, [], []
        self.capture, self.anchor = None, None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.anchor = {"href": attrs["href"], "class": attrs.get("class", ""), "text": ""}
            self.links.append(self.anchor)
        if tag == "link" and "canonical" in attrs.get("rel", "").lower().split():
            self.canonical = attrs.get("href")
        if tag == "meta" and attrs.get("name"):
            self.meta[attrs["name"].lower()] = attrs.get("content", "")
        if tag in {"h1", "title"}:
            self.capture = tag

    def handle_endtag(self, tag):
        if tag == "a":
            self.anchor = None
        if tag == self.capture:
            self.capture = None

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor["text"] += data
        if self.capture == "h1":
            self.h1.append(data.strip())
        if self.capture == "title":
            self.title.append(data.strip())


def classify_url(url, *, category_urls=(), product_urls=(), link_class=""):
    path = unquote(urlsplit(url).path)
    parts = [part for part in path.split("/") if part]
    if parts and parts[0] == "blog":
        return "blog_article" if len(parts) >= 3 else "blog_listing"
    if "/filter/" in path or "/tags/" in path:
        return "tag_filter"
    if url in category_urls or "catalog-section" in link_class or "spoiler__content-list-link" in link_class:
        return "category"
    if url in product_urls or "product-card__name" in link_class or "product-card__image" in link_class:
        return "product"
    if "tags__button" in link_class and parts and parts[0] == "catalog":
        return "tag_filter"
    if path == "/catalog/":
        return "category"
    if not parts or parts[0] != "catalog":
        return "other"
    return "unknown"


def template_type(html, url):
    if "catalog__main" in html or "catalog__filter" in html:
        return "category"
    if re.search(r'(?:class=["\'][^"\']*product__main|["\']@type["\']\s*:\s*["\']Product|itemtype\s*=\s*["\'][^"\']*schema\.org/Product)', html, re.I):
        return "product"
    return classify_url(url)


def summarize_rows(rows):
    """A canonical to a parent is a diagnosis, not permission to erase a detected tag."""
    detected = {row["url"]: row for row in rows}
    counts = Counter(row["page_type"] for row in detected.values())
    aliases = [{"url": row["url"], "declared_canonical": row["declared_canonical"], "page_type": row["page_type"]}
               for row in detected.values() if row.get("declared_canonical") and row["declared_canonical"] != row["url"]]
    return {"counts": {kind: counts[kind] for kind in TYPES}, "total_urls": len(detected),
            "sample_adjusted_canonical_identity_count": len({row.get("declared_canonical") or row["url"] for row in detected.values()}),
            "sampled_canonical_aliases": aliases}


def merge_verified_redirects(rows, sources, site):
    aliases = []
    for source in sources:
        requested = normalize_url(source["url"], site)
        target = normalize_url(source.get("final_url") or source["url"], site)
        if not source.get("error") and requested in rows and target and requested != target:
            row = rows.pop(requested)
            destination = rows.setdefault(target, {**row, "url": target, "sources": set()})
            destination["sources"].update(row["sources"])
            destination["sampled"] |= row["sampled"]
            aliases.append({"url": requested, "final_url": target, "source_ref": source.get("file")})
    return aliases


def collect(site, output, max_categories=1400, max_seconds=660, workers=4):
    output = Path(output)
    raw = output / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    started, clock = now(), time.monotonic()
    site = site.rstrip("/") + "/"
    opener = build_opener(SiteRedirectHandler(site))
    rows, sources, samples, failures = {}, [], [], []
    categories, products, visited, unknown_checked = set(), set(), set(), set()
    sitemap_members, sitemap_sources = defaultdict(set), defaultdict(set)

    def fetch(url):
        began = now()
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        receipt = {"url": url, "started_at": began}
        try:
            if not allowed_fetch(url, site):
                raise ValueError("resource outside authorized public site")
            with opener.open(Request(url, headers={"User-Agent": "OfferistInventory/1.0 (+bounded public SEO inventory)"}), timeout=18) as response:
                if not allowed_fetch(response.url, site):
                    raise ValueError("redirect outside authorized public site")
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ValueError("resource exceeds 8 MiB cap")
                final = response.url
                if body.startswith(b"\x1f\x8b"):
                    with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                        body = compressed.read(MAX_BYTES + 1)
                    if len(body) > MAX_BYTES:
                        raise ValueError("decompressed resource exceeds 8 MiB cap")
                extension = ".xml" if ".xml" in urlsplit(url).path else ".txt" if url.endswith("robots.txt") else ".html"
                path = raw / (key + extension)
                path.write_bytes(body)
                receipt.update({"status": response.status, "final_url": final, "bytes": len(body),
                                "sha256": hashlib.sha256(body).hexdigest(), "file": path.as_posix()})
        except Exception as exc:
            receipt["error"] = f"{type(exc).__name__}: {exc}"
            body = b""
        receipt["finished_at"] = now()
        return receipt, body.decode("utf-8-sig", errors="replace")

    def save_source(result):
        receipt, text = result
        sources.append(receipt)
        if receipt.get("error"):
            failures.append(receipt)
        return receipt, text

    def add(url, kind, source, basis, label=""):
        if not url:
            return
        row = rows.setdefault(url, {"url": url, "page_type": kind, "classification_basis": basis,
                                    "label": label.strip(), "sources": set(), "sampled": False})
        row["sources"].add(source)
        if row["page_type"] == "unknown" or kind in {"category", "tag_filter"}:
            row["page_type"], row["classification_basis"] = kind, basis
        if label.strip() and not row["label"]:
            row["label"] = " ".join(label.split())

    def inspect(url, result):
        receipt, html = save_source(result)
        if not html:
            return
        page = Page(html)
        kind = template_type(html, url)
        if "/filter/" in urlsplit(url).path:
            kind = "tag_filter"
        add(url, kind, receipt["url"], "fetched_template")
        if kind == "category":
            categories.add(url)
        elif kind == "product":
            products.add(url)
        rows[url]["sampled"] = True
        canonical = normalize_url(page.canonical, url) if page.canonical else None
        if canonical:
            rows[url]["declared_canonical"] = canonical
        record = {"url": url, "page_type": kind, "status": receipt.get("status"),
                  "canonical": canonical, "title": " ".join(page.title), "h1": " ".join(page.h1),
                  "description": page.meta.get("description"), "robots": page.meta.get("robots"),
                  "product_link_count": 0, "category_link_count": 0, "tag_link_count": 0,
                  "has_product_schema": bool(re.search(r'["\']@type["\']\s*:\s*["\']Product|itemtype\s*=\s*["\'][^"\']*schema\.org/Product', html, re.I)),
                  "discontinued_text": "Снят с производства" in html or "Снято с производства" in html,
                  "out_of_stock_text": "Нет в наличии" in html, "source_file": receipt["file"]}
        for link in page.links:
            target = normalize_url(link["href"], url)
            if not target:
                continue
            target_kind = classify_url(target, category_urls=categories, product_urls=products, link_class=link["class"])
            if target_kind == "category":
                categories.add(target)
                record["category_link_count"] += 1
            if target_kind == "tag_filter":
                record["tag_link_count"] += 1
            if target_kind == "product":
                record["product_link_count"] += 1
            # Do not turn every navigation URL into a fetched crawl candidate.
            add(target, target_kind, url, "html_link_context", link["text"])
        samples.append(record)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        _, robots = save_source(fetch(urljoin(site, "robots.txt")))
        pending_maps = re.findall(r"(?im)^Sitemap:\s*(\S+)", robots) or [urljoin(site, "sitemap.xml")]
        seen_maps = set()
        while pending_maps and len(seen_maps) < 50 and time.monotonic() - clock < max_seconds:
            batch = list(dict.fromkeys(u for u in pending_maps if u not in seen_maps))[:50-len(seen_maps)]
            pending_maps = []
            for url, result in zip(batch, pool.map(fetch, batch)):
                seen_maps.add(url)
                receipt, text = save_source(result)
                if not text:
                    continue
                try:
                    xml = ET.fromstring(text)
                    locations = [node.text.strip() for node in xml.iter() if node.tag.rsplit("}", 1)[-1] == "loc" and node.text]
                    if xml.tag.rsplit("}", 1)[-1] == "sitemapindex":
                        pending_maps.extend(u for u in locations if allowed_fetch(u, site))
                    else:
                        for value in locations:
                            target = normalize_url(value, site)
                            if target:
                                sitemap_members[url].add(target)
                                sitemap_sources[target].add(url)
                                add(target, classify_url(target), url, "sitemap_path")
                except ET.ParseError as exc:
                    failures.append({"url": url, "error": str(exc)})
        print(json.dumps({"stage": "sitemaps", "urls": len(rows), "elapsed_seconds": round(time.monotonic()-clock, 2)}, ensure_ascii=False), flush=True)
        seeds = [site, urljoin(site, "catalog/"), urljoin(site, "blog/")]
        for url, result in zip(seeds, pool.map(fetch, seeds)):
            visited.add(url)
            inspect(url, result)
        # Classify a sitemap catalogue cohort only after three spread samples show product templates.
        for source, members in sitemap_members.items():
            catalogue = sorted(u for u in members if urlsplit(u).path.startswith("/catalog/"))
            if len(catalogue) < 3:
                continue
            chosen = [catalogue[0], catalogue[len(catalogue)//2], catalogue[-1]]
            results = list(pool.map(fetch, chosen))
            if all(template_type(body, url) == "product" for url, (_, body) in zip(chosen, results)):
                products.update(catalogue)
                for url in catalogue:
                    if rows[url]["page_type"] == "unknown":
                        rows[url]["page_type"] = "product"
                        rows[url]["classification_basis"] = "sitemap_cohort_3_spread_product_template_samples"
            for url, result in zip(chosen, results):
                visited.add(url)
                inspect(url, result)
        while time.monotonic()-clock < max_seconds:
            remaining = sorted(categories - visited)
            if len(visited & categories) >= max_categories:
                break
            if remaining:
                batch = remaining[:min(workers * 3, max_categories-len(visited & categories))]
            else:
                # shortcut: inspect at most 12 untyped URLs, add a reviewed mapping for a different site structure.
                batch = sorted(url for url, row in rows.items() if row["page_type"] == "unknown" and url not in visited)[:12-len(unknown_checked)]
                if not batch:
                    break
                unknown_checked.update(batch)
            for url, result in zip(batch, pool.map(fetch, batch)):
                visited.add(url)
                inspect(url, result)
            if len(visited & categories) % 60 < len(batch):
                print(json.dumps({"stage": "category_tree", "categories_found": len(categories), "categories_fetched": len(visited & categories), "tags_found": sum(r["page_type"] == "tag_filter" for r in rows.values()), "elapsed_seconds": round(time.monotonic()-clock, 2)}), flush=True)

    redirect_aliases = merge_verified_redirects(rows, sources, site)
    aggregate = summarize_rows(rows.values())
    coverage = {"sitemap_files_fetched": len(seen_maps), "sitemap_url_count": len(sitemap_sources),
                "category_urls_discovered": len(categories), "category_urls_requested": len(categories & visited),
                "category_urls_fetched": sum(row["url"] in categories for row in samples),
                "category_tree_exhausted": not bool(categories - visited), "sampled_html_pages": len(samples),
                "unknown_urls_checked": len(unknown_checked),
                "tag_scope": "Unique linked tag/filter landing URLs found in fetched category HTML; not all possible filter combinations.",
                "indexation_checked": False,
                "classifier_profile": "Verified catalogue/category/product-card HTML classes; /catalog/ and /blog/ paths. Other site structures require reviewed mapping and stay unknown.",
                "classification_limit": "Product sitemap cohort inferred from three spread product template samples. Counts preserve unique normalized detected URLs. Canonical checks cover fetched HTML only; tag-to-parent canonicals remain detected tags.",
                "tag_filter_robots_disallow_observed": bool(re.search(r"Disallow:\s*.*(?:filter|tags)", robots, re.I))}
    summary = {"site": site, "started_at": started, "finished_at": now(),
               "duration_seconds": round(time.monotonic()-clock, 3), **aggregate,
               "verified_redirect_aliases": redirect_aliases,
               "rows_csv": (output / "inventory.csv").as_posix(),
               "sources_ref": (output / "sources.json").as_posix(),
               "coverage": coverage, "failures": failures}
    for filename, value in [("summary.json", summary), ("sources.json", sources), ("template-samples.json", samples)]:
        (output / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = ["url", "page_type", "label", "classification_basis", "sampled", "declared_canonical", "sources"]
    with (output / "inventory.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in sorted(rows.values(), key=lambda row: (row["page_type"], row["url"])):
            writer.writerow({**row, "sources": " | ".join(sorted(row["sources"]))})
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-categories", type=int, default=1400)
    parser.add_argument("--max-seconds", type=int, default=660)
    parser.add_argument("--workers", type=int, default=4)
    arguments = parser.parse_args()
    if urlsplit(arguments.site).scheme not in {"http", "https"} or not urlsplit(arguments.site).hostname:
        parser.error("--site needs an http(s) origin")
    if min(arguments.max_categories, arguments.max_seconds, arguments.workers) < 1 or arguments.workers > 8:
        parser.error("positive bounds required; --workers at most 8")
    collect(arguments.site, arguments.out, arguments.max_categories, arguments.max_seconds, arguments.workers)
