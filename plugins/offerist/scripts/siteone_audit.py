#!/usr/bin/env python3
"""Run the bundled SiteOne Crawler and normalize its saved responses, not a second crawl."""
import argparse
import base64
from collections import Counter, defaultdict, deque
import csv
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
import zipfile

ASSETS = Path(__file__).resolve().parents[1] / 'assets' / 'siteone'
ACTION = r'(?i)(?:[?&](?:action|add-to-cart|add2basket|buy|logout|clear_cache)=|/(?:logout|login|cart|basket|checkout|wp-admin|wp-login)(?:[/.?]|$))'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def ensure_runtime(cache_dir, assets=ASSETS):
    manifest = json.loads((assets / 'runtime.json').read_text(encoding='utf-8'))
    if platform.system() != 'Windows' or platform.machine().lower() not in {'amd64', 'x86_64', 'arm64', 'aarch64'}:
        raise RuntimeError('Bundled SiteOne runtime requires Windows x64, or Windows ARM64 x64 emulation')
    archive = assets / manifest['archive']
    if sha(archive) != manifest['archive_sha256']:
        raise ValueError('SiteOne archive SHA-256 mismatch')
    target = Path(cache_dir).resolve() / f"siteone-{manifest['version']}" / 'siteone-crawler.exe'
    if target.exists() and sha(target) == manifest['binary_sha256']:
        return target, manifest
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        body = z.read(manifest['binary_member'])
    if hashlib.sha256(body).hexdigest() != manifest['binary_sha256']:
        raise ValueError('SiteOne executable SHA-256 mismatch')
    pending = target.with_suffix('.tmp')
    pending.write_bytes(body)
    pending.replace(target)
    return target, manifest


def clean_url(value, base):
    try:
        url = urldefrag(urljoin(base, value.strip()))[0]
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            return None
        if any(ord(c) < 32 for c in url):
            return None
        parsed.port
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or '/', parsed.query, ''))
    except (ValueError, AttributeError):
        return None


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.title, self.h1s, self.description, self.robots, self.canonical = '', [], '', '', ''
        self.links, self.base, self.capture, self.anchor = [], '', None, None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'base' and not self.base:
            self.base = attrs.get('href', '')
        elif tag == 'title':
            self.capture = 'title'
        elif tag == 'h1':
            self.h1s.append('')
            self.capture = 'h1'
        elif tag == 'meta':
            name = attrs.get('name', '').lower()
            if name == 'description': self.description = attrs.get('content', '')
            if name in {'robots', 'googlebot', 'yandex'}: self.robots += ' ' + attrs.get('content', '')
        elif tag == 'link' and 'canonical' in attrs.get('rel', '').lower().split():
            self.canonical = attrs.get('href', '')
        elif tag == 'a' and attrs.get('href'):
            self.anchor = {'href': attrs['href'], 'anchor': '', 'nofollow': 'nofollow' in attrs.get('rel', '').lower().split()}
            self.links.append(self.anchor)

    def handle_endtag(self, tag):
        if tag in {'title', 'h1'}: self.capture = None
        if tag == 'a': self.anchor = None

    def handle_data(self, value):
        if self.capture == 'title': self.title += value
        if self.capture == 'h1': self.h1s[-1] += value
        if self.anchor is not None: self.anchor['anchor'] += value


def csv_write(path, rows, fields):
    with Path(path).open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def normalize(out, site, *, max_pages, max_depth, known_total=None):
    out = Path(out)
    report = json.loads((out / 'siteone.json').read_text(encoding='utf-8'))
    results = {r['url']: r for r in report['results']}
    hostname = urlsplit(site).hostname
    pages, edges, cache_urls = {}, {}, set()
    for file in sorted((out / 'http-cache').rglob('*.cache')):
        cache = json.loads(file.read_text(encoding='utf-8'))
        url = clean_url(cache.get('url', ''), site)
        if url not in results or urlsplit(url).hostname != hostname:
            continue
        cache_urls.add(url)
        headers = {k.lower(): str(v) for k, v in cache.get('headers', {}).items()}
        if 'html' not in headers.get('content-type', '').lower():
            continue
        encoding = re.search(r'charset=([\w-]+)', headers.get('content-type', ''))
        try:
            body = base64.b64decode(cache['body'], validate=True).decode(encoding.group(1) if encoding else 'utf-8', errors='replace')
        except LookupError:
            body = base64.b64decode(cache['body'], validate=True).decode('utf-8', errors='replace')
        page = Page(body)
        status = int(cache['status_code'])
        row = {'url': url, 'status': status, 'title': page.title.strip(), 'description': page.description.strip(),
               'h1': ' | '.join(h.strip() for h in page.h1s), 'h1_count': len(page.h1s),
               'canonical': clean_url(page.canonical, url) if page.canonical else '',
               'robots': (page.robots + ' ' + headers.get('x-robots-tag', '')).strip(),
               'elapsed_seconds': results[url].get('elapsedTime'), 'source_cache': str(file.relative_to(out)),
               'source_sha256': sha(file)}
        row['noindex'] = bool(re.search(r'\b(?:noindex|none)\b', row['robots'], re.I))
        pages[url] = row
        if not 200 <= status < 300:
            continue
        base = clean_url(page.base, url) if page.base else url
        for link in page.links:
            target = clean_url(link['href'], base or url)
            if not target:
                continue
            key = (url, target)
            edge = edges.setdefault(key, {'source': url, 'target': target, 'internal': urlsplit(target).hostname == hostname,
                'occurrences': 0, 'nofollow_occurrences': 0, 'anchor_example': link['anchor'].strip(),
                'target_status': results.get(target, {}).get('status', ''), 'source_cache': row['source_cache']})
            edge['occurrences'] += 1
            edge['nofollow_occurrences'] += int(link['nofollow'])

    edge_rows = list(edges.values())
    internal = [e for e in edge_rows if e['internal']]
    checked = [e for e in internal if str(e['target_status']).isdigit() and int(e['target_status']) > 0]
    broken = [e for e in checked if int(e['target_status']) >= 400]
    healthy = [r for r in pages.values() if 200 <= r['status'] < 300]
    indexable = [r for r in healthy if not r['noindex']]
    titles = Counter(r['title'].casefold() for r in indexable if r['title'])
    incoming, outgoing, adjacency = defaultdict(set), defaultdict(set), defaultdict(set)
    for e in internal:
        incoming[e['target']].add(e['source'])
        outgoing[e['source']].add(e['target'])
        adjacency[e['source']].add(e['target'])
    depth, queue = {site: 0}, deque([site])
    while queue:
        source = queue.popleft()
        for target in adjacency[source]:
            if target not in depth:
                depth[target] = depth[source] + 1
                queue.append(target)
    for row in pages.values():
        row.update(inlinks_observed=len(incoming[row['url']]), outlinks_observed=len(outgoing[row['url']]),
                   click_depth_observed=depth.get(row['url'], ''), duplicate_title=bool(row['title'] and titles[row['title'].casefold()] > 1))

    findings = []
    def finding(code, severity, count, denominator, unit, note):
        findings.append({'code': code, 'severity': severity, 'affected': count, 'denominator': denominator,
            'unit': unit, 'percent': round(100 * count / denominator, 2) if denominator else None, 'note': note})
    finding('broken_internal_links', 'serious', len(broken), len(checked), 'unique_directed_links', 'Only links to targets with a known HTTP response; unvisited targets are unknown, not healthy.')
    finding('http_5xx', 'critical', sum(str(r.get('status', '')).startswith('5') for r in results.values()), len(results), 'visited_urls', 'Observed responses only; timeout/network errors remain separate.')
    finding('missing_title', 'serious', sum(not r['title'] for r in indexable), len(indexable), 'html_2xx_not_noindex', 'Noindex pages excluded; a meta directive is not search-engine indexation.')
    finding('duplicate_title', 'serious', sum(r['duplicate_title'] for r in indexable), len(indexable), 'html_2xx_not_noindex', 'Affected pages in duplicate groups, not the number of duplicate strings.')
    finding('missing_h1', 'serious', sum(not r['h1'] for r in indexable), len(indexable), 'html_2xx_not_noindex', 'Source HTML only; JavaScript rendering is disabled.')
    finding('missing_description', 'review', sum(not r['description'] for r in indexable), len(indexable), 'html_2xx_not_noindex', 'Review snippet potential; not automatically a ranking blocker.')
    finding('missing_canonical', 'review', sum(not r['canonical'] for r in healthy), len(healthy), 'html_2xx', 'Not every page needs a canonical; confirm duplicate intent before recommending changes.')
    finding('noindex_observed', 'review', sum(r['noindex'] for r in healthy), len(healthy), 'html_2xx', 'May be intentional. Never recommend opening all noindex pages.')
    skipped = report.get('tables', {}).get('skipped-summary', {}).get('rows', [])
    discovered = set(results) | {e['target'] for e in internal}
    unvisited = discovered - set(results)
    coverage = {'visited_urls': len(results), 'html_responses_parsed': len(pages), 'html_2xx': len(healthy),
        'html_2xx_not_noindex': len(indexable), 'raw_cached_visited_urls': len(cache_urls),
        'discovered_internal_urls_lower_bound': len(discovered), 'discovered_unvisited_urls': len(unvisited),
        'known_total_urls': known_total,
        'visited_share_of_known_total_percent': round(100 * len(results) / known_total, 2) if known_total and known_total >= len(results) else None,
        'unique_internal_directed_links': len(internal), 'internal_link_occurrences': sum(e['occurrences'] for e in internal),
        'internal_links_with_known_http_status': len(checked), 'internal_links_with_unknown_http_status': len(internal) - len(checked),
        'max_pages': max_pages, 'max_depth': max_depth, 'page_limit_reached': len(results) >= max_pages,
        'skipped_summary': skipped, 'scope': 'bounded_sample' if unvisited or skipped or len(results) >= max_pages else 'discovered_linked_component',
        'whole_site_coverage_proven': False,
        'limitations': ['A linked crawl cannot discover orphan URLs; combine with sitemap, GSC and Webmaster inventories.',
                       'No rendering, external checks or asset crawling. JS-only links, images and Core Web Vitals are not audited.',
                       'Depth and page limits, robots rules and action URL filters may exclude pages. Ratios describe observed denominators only.',
                       'Observed inlinks and depth describe the sampled graph, not sitewide PageRank. No orphan claim from a partial graph.']}
    deductions = sum((3 if f['severity'] == 'critical' else 1.5) * f['affected'] / f['denominator']
        for f in findings if f['severity'] in {'critical', 'serious'} and f['denominator'])
    score = round(max(0, 10 - deductions), 1) if indexable and len(cache_urls) == len(results) else None
    summary = {'schema_version': 1, 'source': 'SiteOne Crawler', 'source_version': report['crawler']['version'],
        'pageCount': len(results), 'linkEdgeCount': len(internal),
        'site': site, 'coverage': coverage, 'findings': findings,
        'seo_health': {'score': score, 'scale': 10, 'scope': 'observed_sample_only',
            'zone': 'critical' if any(f['severity'] == 'critical' and f['affected'] for f in findings) else 'serious' if any(f['severity'] == 'serious' and f['affected'] for f in findings) else 'good' if score is not None else 'unknown',
            'formula': '10 minus 3 times each critical affected share and 1.5 times each serious affected share; review items do not deduct. Not a search-engine score.'},
        'siteone_quality_scores': report.get('qualityScores', {}), 'siteone_findings': report.get('summary', {}).get('items', []),
        'evidence': {'raw_json': 'siteone.json', 'raw_json_sha256': sha(out/'siteone.json'), 'cached_response_count': len(cache_urls)}}
    csv_write(out/'pages.csv', list(pages.values()), ['url','status','title','description','h1','h1_count','canonical','robots','noindex','duplicate_title','elapsed_seconds','inlinks_observed','outlinks_observed','click_depth_observed','source_cache','source_sha256'])
    csv_write(out/'links.csv', edge_rows, ['source','target','internal','occurrences','nofollow_occurrences','anchor_example','target_status','source_cache'])
    csv_write(out/'findings.csv', findings, ['code','severity','affected','denominator','unit','percent','note'])
    save_json(out/'graph.json', {'scope': coverage['scope'], 'whole_site_coverage_proven': False,
        'nodes': [{'url': url, 'visited': url in results, 'status': results.get(url, {}).get('status', ''),
                   'inlinks_observed': len(incoming[url]), 'outlinks_observed': len(outgoing[url]),
                   'click_depth_observed': depth.get(url)} for url in sorted(discovered)], 'edges': internal})
    save_json(out/'audit-summary.json', summary)
    return summary


def run(site, out, *, max_pages=100, max_depth=8, seconds=180, known_total=None, runtime_cache=None):
    site = clean_url(site, site)
    if not site: raise ValueError('A valid HTTP(S) site without credentials is required')
    if not 1 <= max_pages <= 1000 or not 1 <= max_depth <= 20 or not 15 <= seconds <= 900:
        raise ValueError('Use max-pages 1..1000, max-depth 1..20, seconds 15..900')
    if known_total is not None and known_total < 1: raise ValueError('known-total must be positive')
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()): raise FileExistsError('Use a new empty output directory to preserve earlier evidence')
    out.mkdir(parents=True, exist_ok=True)
    binary, manifest = ensure_runtime(runtime_cache or out.parent/'.siteone-runtime')
    origin = urlsplit(site)
    origin_regex = '^' + re.escape(f'{origin.scheme}://{origin.netloc}') + r'(?:/|$)'
    cmd = [str(binary), f'--url={site}', '--disable-all-assets', '--ignore-html-comments', '--workers=2',
        '--max-reqs-per-sec=2', f'--max-visited-urls={max_pages}', f'--max-depth={max_depth}', '--timeout=12',
        '--max-queue-length=5000', '--max-skipped-urls=5000', '--rows-limit=10000', '--memory-limit=512M',
        f'--include-regex={origin_regex}', f'--ignore-regex={ACTION}', f'--http-cache-dir={out / "http-cache"}',
        '--http-cache-ttl=1h', '--output=json', f'--output-json-file={out / "siteone.json"}',
        f'--output-html-report={out / "siteone.html"}', '--output-text-file=', '--no-color', '--hide-progress-bar']
    env = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA','USERDOMAIN','USERNAME','PATHEXT'}}
    started, clock = datetime.now(timezone.utc).isoformat(), time.monotonic()
    timed_out = False
    with (out/'stdout.txt').open('w', encoding='utf-8') as stdout, (out/'stderr.txt').open('w', encoding='utf-8') as stderr:
        try:
            result = subprocess.run(cmd, cwd=out, stdout=stdout, stderr=stderr, env=env, timeout=seconds, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            timed_out, returncode = True, None
    receipt = {'started_utc': started, 'ended_utc': datetime.now(timezone.utc).isoformat(),
        'elapsed_seconds': round(time.monotonic()-clock, 3), 'returncode': returncode, 'timed_out': timed_out,
        'runtime': manifest, 'command': cmd, 'status': 'raw_report_available' if (out/'siteone.json').exists() else 'incomplete_no_report'}
    save_json(out/'run.json', receipt)
    if not (out/'siteone.json').exists():
        raise RuntimeError(f'SiteOne did not produce a report; inspect {out / "run.json"} and stderr.txt; do not treat this as an audit pass')
    summary = normalize(out, site, max_pages=max_pages, max_depth=max_depth, known_total=known_total)
    summary['execution'] = receipt
    if timed_out or returncode != 0:
        summary['coverage']['scope'] = 'interrupted_partial'
        summary['seo_health'].update(score=None, zone='unknown')
    save_json(out/'audit-summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--max-pages', type=int, default=100)
    parser.add_argument('--max-depth', type=int, default=8)
    parser.add_argument('--seconds', type=int, default=180)
    parser.add_argument('--known-total', type=int)
    parser.add_argument('--runtime-cache', type=Path)
    args = parser.parse_args()
    summary = run(args.site, args.out, max_pages=args.max_pages, max_depth=args.max_depth, seconds=args.seconds,
                  known_total=args.known_total, runtime_cache=args.runtime_cache)
    print(json.dumps({'summary': str(args.out.resolve()/'audit-summary.json'), 'coverage': summary['coverage'], 'seo_health': summary['seo_health']}, ensure_ascii=False))


if __name__ == '__main__': main()
