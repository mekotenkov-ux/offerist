import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { basename, dirname, join, resolve } from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const manifest = JSON.parse(await readFile(join(here, "manifest.json"), "utf8"));
const adapterSha256 = sha256(await readFile(fileURLToPath(import.meta.url)));
let verifiedCorePromise;

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

async function verifiedCore() {
  if (!verifiedCorePromise) verifiedCorePromise = (async () => {
    const bundlePath = join(here, "core.bundle.cjs");
    const actualBundleHash = sha256(await readFile(bundlePath));
    if (actualBundleHash !== manifest.bundle_sha256) throw new Error("BAR core.bundle.cjs SHA-256 mismatch");
    const expectedDictionaries = Object.entries(manifest.dictionary_sha256 ?? {});
    if (expectedDictionaries.length !== 10) throw new Error("BAR dictionary manifest must contain exactly 10 files");
    const dictionaryDir = resolve(here, "../../assets/bar/dicts");
    for (const [name, expected] of expectedDictionaries) {
      if (basename(name) !== name) throw new Error("BAR dictionary manifest contains an invalid filename");
      const actual = sha256(await readFile(join(dictionaryDir, name)));
      if (actual !== expected) throw new Error(`BAR dictionary SHA-256 mismatch: ${name}`);
    }
    const core = require(bundlePath);
    if (typeof core.analyzePayload !== "function") throw new Error("BAR core bundle has no analyzePayload export");
    return core;
  })();
  return verifiedCorePromise;
}

const isRecord = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const clean = (value) => typeof value === "string" ? value.trim() : "";
const rank = (peer) => Number.isSafeInteger(peer.position) && peer.position > 0 ? peer.position : Number.MAX_SAFE_INTEGER;
const MAX_ANALYSIS_TOKEN_LENGTH = 80;

function prepareTextPage(page, role) {
  const sourceText = page.text;
  const omitted = new Map();
  // The original BAR snapshot accepts keys of at most 256 UTF-16 units. Its
  // n-grams span at most three words, so a fused lexical run above 80 units is
  // excluded as an extraction artifact before it can create an invalid key.
  const text = sourceText.replace(/[\p{L}\p{M}\p{N}]+/gu, (token) => {
    if (token.length <= MAX_ANALYSIS_TOKEN_LENGTH) return token;
    const hash = sha256(Buffer.from(token, "utf8"));
    const entry = omitted.get(hash) ?? { sha256: hash, length: token.length, occurrences: 0 };
    entry.occurrences += 1;
    omitted.set(hash, entry);
    return " ";
  });
  if (text.trim().length < 80) throw new Error(`${role} has too little analyzable text after excluding overlong fused tokens`);
  const excluded = [...omitted.values()].sort((a, b) => a.sha256.localeCompare(b.sha256));
  return {
    page: { ...page, text },
    receipt: { role, url: clean(page.url) || null, source_text_sha256: sha256(Buffer.from(sourceText, "utf8")),
      analyzed_text_sha256: sha256(Buffer.from(text, "utf8")),
      excluded_overlong_token_occurrences: excluded.reduce((total, item) => total + item.occurrences, 0),
      excluded_utf16_units: excluded.reduce((total, item) => total + item.length * item.occurrences, 0),
      excluded_tokens: excluded },
  };
}

function verifyPage(page, role, pageType, intent) {
  if (!isRecord(page)) throw new Error(`${role} must be an object`);
  if (clean(page.page_type) !== pageType) throw new Error(`${role}.page_type does not match page_type`);
  if (clean(page.intent) !== intent) throw new Error(`${role}.intent does not match intent`);
  if (!clean(page.text)) throw new Error(`${role}.text is required for offline BAR`);
  if (page.text.length > 200_000) throw new Error(`${role}.text exceeds the 200000-character BAR limit`);
}

function pageInput(page) {
  return { name: clean(page.name) || undefined, url: clean(page.url) || undefined,
    text: page.text, position: Number.isSafeInteger(page.position) ? page.position : undefined,
    title: clean(page.title) || undefined };
}

async function analyzeNoNetwork(payload) {
  const { analyzePayload } = await verifiedCore();
  const originalFetch = globalThis.fetch;
  const oldFirecrawl = process.env.FIRECRAWL;
  const oldFirecrawlKey = process.env.FIRECRAWL_API_KEY;
  globalThis.fetch = async () => { throw new Error("BAR offline adapter forbids network fetch"); };
  delete process.env.FIRECRAWL;
  delete process.env.FIRECRAWL_API_KEY;
  try { return await analyzePayload(payload); }
  finally {
    globalThis.fetch = originalFetch;
    if (oldFirecrawl === undefined) delete process.env.FIRECRAWL; else process.env.FIRECRAWL = oldFirecrawl;
    if (oldFirecrawlKey === undefined) delete process.env.FIRECRAWL_API_KEY; else process.env.FIRECRAWL_API_KEY = oldFirecrawlKey;
  }
}

function coverage(report, supplied, comparablePeers, role, preparationWarnings) {
  const analyzed = report.meta.competitorsAnalyzed;
  return {
    input_pages: supplied,
    comparable_peer_pages: comparablePeers,
    analyzed_peer_pages: analyzed,
    excluded_peer_pages: comparablePeers - analyzed,
    warning_count: report.warnings.length,
    preparation_warning_count: preparationWarnings,
    confidence: report.meta.confidence,
    language: report.meta.language,
    morphology_mode: report.meta.morphologyMode,
    phrase_similarity_engine: report.meta.phraseSimilarityEngine,
    available_zones: ["all", "text"],
    reference_role: role,
    source: "provided_text",
  };
}

function provenance() {
  return {
    bar_repository: manifest.bar_repository,
    bar_commit: manifest.bar_commit,
    bar_package_version: manifest.bar_package_version,
    bundle_sha256: manifest.bundle_sha256,
    dictionary_sha256: manifest.dictionary_sha256,
    dependencies: manifest.dependencies,
    snapshot_version: manifest.snapshot_version,
    adapter_sha256: adapterSha256,
    input_preparation_version: "overlong-fused-token-v1",
  };
}

function requirementRows(rows, label) {
  return rows.filter((row) => row.importance > 0).sort((a, b) => b.importance - a.importance)
    .map((row) => ({ key: row.key, label: row[label], importance: row.importance,
      peer_reference_count: row.ownCount, peer_median_count: row.medianCount,
      peer_q25_count: row.q25Count, peer_q75_count: row.q75Count, peer_sites: row.sites }));
}

export async function analyzeBarInput(input) {
  if (!isRecord(input)) throw new Error("Input must be a JSON object");
  const pageType = clean(input.page_type);
  const intent = clean(input.intent);
  if (!pageType || !intent) throw new Error("page_type and intent are required");
  const queries = Array.isArray(input.queries) ? input.queries : [];
  if (!queries.length || queries.some((value) => !clean(value))) throw new Error("queries must contain at least one nonempty string");
  const selectedQueries = new Set();
  for (const entry of queries) for (const part of entry.split(/[\n,;]+/)) {
    const query = part.trim().replace(/\s+/g, " ");
    if (!query) continue;
    if (query.length > 160) throw new Error("BAR query exceeds 160 characters");
    selectedQueries.add(query);
  }
  if (!selectedQueries.size) throw new Error("queries must contain at least one nonempty string");
  if (selectedQueries.size > 20) throw new Error("BAR supports at most 20 unique queries; input would be silently truncated");
  const peers = Array.isArray(input.peers) ? input.peers : [];
  for (const [index, peer] of peers.entries()) verifyPage(peer, `peers[${index}]`, pageType, intent);
  const own = input.own_page;
  const hasOwn = isRecord(own) && Boolean(clean(own.text));
  if (hasOwn) verifyPage(own, "own_page", pageType, intent);
  const preparedPeers = peers.map((peer, index) => prepareTextPage(peer, `peers[${index}]`));
  const preparedOwn = hasOwn ? prepareTextPage(own, "own_page") : null;
  const preparationPages = [...(preparedOwn ? [preparedOwn.receipt] : []), ...preparedPeers.map((item) => item.receipt)];
  const excludedOccurrences = preparationPages.reduce((total, page) => total + page.excluded_overlong_token_occurrences, 0);
  const preparation = { method: "exclude_wordlike_runs_over_80_utf16_units_before_original_BAR_core",
    reason: "BAR snapshot n-gram keys have a 256-character limit; long fused extraction artifacts cannot be serialized safely",
    excluded_overlong_token_occurrences: excludedOccurrences, pages: preparationPages,
    warnings: excludedOccurrences ? [`Excluded ${excludedOccurrences} overlong fused token occurrences from analysis text; original text and SHA-256 receipts are retained in the input`] : [] };
  await verifiedCore();
  const base = { schema_version: 1, page_type: pageType, intent, provenance: provenance(), preparation };
  if (!peers.length) return { ...base, status: "insufficient_peers", mode: hasOwn ? "own_vs_peers" : "peer_only",
    reference_role: hasOwn ? "own" : "peer", own_score: null,
    limitation: "BAR needs comparable peer texts; no score or benchmark was calculated" };
  const search = isRecord(input.search) ? input.search : undefined;
  const common = { queries, serpQuery: clean(input.serp_query) || undefined, language: clean(input.language) || "auto",
    search, stopWords: input.stop_words, stopPhrases: input.stop_phrases };
  if (hasOwn) {
    const report = await analyzeNoNetwork({ ...common, ownPage: pageInput(preparedOwn.page), competitors: preparedPeers.map((item) => pageInput(item.page)) });
    return { ...base, status: "ok", mode: "own_vs_peers", reference_role: "own",
      own_score: { key_tfidf_score: report.summary.keyTfidfScore, weighted_coverage: report.summary.weightedCoverage,
        weighted_density: report.summary.weightedDensity },
      coverage: coverage(report, peers.length, peers.length, "own", preparation.warnings.length), report };
  }
  if (peers.length < 2) return { ...base, status: "insufficient_peers", mode: "peer_only",
    reference_role: "peer", own_score: null,
    limitation: "A peer-only benchmark needs at least two real peer pages" };
  const ordered = preparedPeers.map((prepared, index) => ({ prepared, index })).sort((a, b) => rank(a.prepared.page) - rank(b.prepared.page) || a.index - b.index);
  const reference = ordered[0].prepared.page;
  const cohort = ordered.slice(1).map(({ prepared }) => prepared.page);
  const report = await analyzeNoNetwork({ ...common, ownPage: pageInput(reference), competitors: cohort.map(pageInput) });
  return { ...base, status: "ok", mode: "peer_only", reference_role: "peer", own_score: null,
    limitation: "Peer reference metrics describe a real competitor, never the absent client page",
    coverage: coverage(report, peers.length, cohort.length, "peer", preparation.warnings.length),
    peer_benchmark: {
      reference: { name: clean(reference.name) || null, url: clean(reference.url) || null,
        position: Number.isSafeInteger(reference.position) ? reference.position : null },
      reference_metrics: { key_tfidf_score: report.summary.keyTfidfScore,
        weighted_coverage: report.summary.weightedCoverage, weighted_density: report.summary.weightedDensity,
        words: report.summary.ownWords },
      peer_median_words: report.summary.medianWords,
      recommended_words_min: report.summary.recommendedMin,
      recommended_words_max: report.summary.recommendedMax,
      term_requirements: requirementRows(report.terms, "term"),
      phrase_requirements: requirementRows(report.ngrams, "phrase"),
      warnings: report.warnings,
      meta: { competitors_analyzed: report.meta.competitorsAnalyzed, confidence: report.meta.confidence,
        language: report.meta.language, morphology_mode: report.meta.morphologyMode,
        snapshot_id: report.meta.snapshotId },
    } };
}
