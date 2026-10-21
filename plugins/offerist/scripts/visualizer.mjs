/** Offline authored HTML -> vector SVG -> PNG. Uses an existing Chromium only. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const plugin = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const assetRoot = path.join(plugin, 'skills/offerist-visualizer/assets');
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');

export function validateMeta(meta, base) {
  for (const key of ['client', 'section', 'title', 'takeaway', 'source_note']) {
    if (typeof meta[key] !== 'string' || !meta[key].trim()) throw Error(`${key} is required`);
  }
  if (/[:—–]/u.test(meta.title)) throw Error('Use a simple heading without colon or dash');
  if (!['diagram', 'matrix', 'table', 'list', 'prototype', 'sample'].includes(meta.presentation)) {
    throw Error('Use charts.py for quantitative charts; unsupported presentation');
  }
  if (!['observed', 'proposed', 'sample'].includes(meta.basis)) throw Error('basis is required');
  if (!Array.isArray(meta.limitations) || meta.limitations.some(x => typeof x !== 'string')) {
    throw Error('limitations must be an array of text');
  }
  if (!Array.isArray(meta.sources) || !meta.sources.length) throw Error('At least one source is required');
  return meta.sources.map(source => {
    if (typeof source.path !== 'string' || typeof source.description !== 'string' || !source.description.trim()) {
      throw Error('Each source needs a local path and description');
    }
    const filename = path.resolve(base, source.path);
    const actual = hash(fs.readFileSync(filename));
    if (!/^[a-f0-9]{64}$/.test(source.sha256 || '') || actual !== source.sha256) {
      throw Error('Source hash mismatch');
    }
    return { ...source, path: filename, sha256: actual };
  });
}

export async function render({ input, metaFile, output, browserPath, modulesRoot }) {
  const meta = JSON.parse(fs.readFileSync(metaFile, 'utf8'));
  const sources = validateMeta(meta, path.dirname(path.resolve(metaFile)));
  const html = fs.readFileSync(input, 'utf8');
  if (Buffer.byteLength(html) > 8_000_000) throw Error('Split large sections into separate visuals');
  const target = path.resolve(output);
  const files = Object.fromEntries(['html', 'svg', 'png', 'json'].map(ext => [ext, `${target}.${ext}`]));
  if (Object.values(files).some(filename => fs.existsSync(filename))) throw Error('Output exists; choose a new name');
  const lock = JSON.parse(fs.readFileSync(path.join(assetRoot, 'renderer.json'), 'utf8'));
  const bundle = fs.readFileSync(path.join(assetRoot, lock.file), 'utf8');
  if (hash(Buffer.from(bundle)) !== lock.sha256) throw Error('Renderer integrity mismatch');
  const dependencyRoot = modulesRoot || path.resolve(path.dirname(process.execPath), '../node_modules');
  const require = createRequire(path.join(dependencyRoot, 'playwright/package.json'));
  const { chromium } = require('playwright');
  const candidates = [browserPath, process.env.OFFERIST_BROWSER, chromium.executablePath(),
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome'];
  const executablePath = candidates.find(value => value && fs.existsSync(value));
  if (!executablePath) throw Error('No existing Chromium found; use an available renderer, do not download a browser automatically');
  const browser = await chromium.launch({ executablePath, headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1200, height: 1600 }, deviceScaleFactor: 2 });
    await context.route('**/*', route => route.abort());
    const page = await context.newPage();
    // Generated local HTML only. Never execute downloaded reference pages or their scripts.
    await page.evaluate(source => {
      const doc = new DOMParser().parseFromString(source, 'text/html');
      if (doc.querySelector('script,iframe,object,embed,form,canvas,video,audio')) throw Error('Active content is unsupported');
      for (const element of doc.querySelectorAll('*')) {
        for (const attr of element.attributes) {
          if (/^on/i.test(attr.name) || /javascript:/i.test(attr.value)) throw Error('Active attributes are unsupported');
        }
      }
      for (const image of doc.querySelectorAll('img')) {
        if (!/^data:image\/(png|jpeg|webp|gif);/i.test(image.getAttribute('src') || '')) {
          throw Error('Embed checked images as data URI; external and file images are unsupported');
        }
      }
    }, html);
    const csp = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:; font-src data:; script-src \'none\'; connect-src \'none\'">';
    await page.setContent(csp + html, { waitUntil: 'load', timeout: 15000 });
    const measurements = await page.evaluate(async metadata => {
      await document.fonts.ready;
      const root = document.querySelector('#visual');
      if (!root) throw Error('HTML needs a #visual element');
      const rect = root.getBoundingClientRect();
      if (rect.width < 600 || rect.width > 1800 || rect.height < 100 || rect.height > 1800) {
        throw Error('Visual must fit a readable page block; split or resize it');
      }
      if (root.scrollWidth > root.clientWidth + 2 || root.scrollHeight > root.clientHeight + 2) {
        throw Error('Visual clips content; split it instead of shrinking the font');
      }
      const text = root.innerText;
      if (!text.includes(metadata.title)) throw Error('Visual must show its title');
      if (!text.includes(metadata.source_note)) throw Error('Visual must show its source note');
      if (/Замените этапы решениями|заполняются перед выпуском/u.test(text)) {
        throw Error('Replace the starter template text with the current client material');
      }
      const labels = { observed: 'Данные', proposed: 'Предлагаемая схема', sample: 'Образец' };
      if (!text.includes(labels[metadata.basis])) throw Error(`Show the basis label ${labels[metadata.basis]}`);
      const sizes = [...root.querySelectorAll('*')].filter(node => node.childNodes.length &&
        [...node.childNodes].some(child => child.nodeType === 3 && child.textContent.trim()))
        .map(node => parseFloat(getComputedStyle(node).fontSize));
      return { width: Math.ceil(rect.width), height: Math.ceil(rect.height), min_font_px: Math.min(...sizes), text };
    }, meta);
    await page.evaluate(bundle);
    const svg = await page.evaluate(() => {
      const result = window.offeristToSVG(document.querySelector('#visual'));
      for (const image of result.querySelectorAll('image')) {
        const href = image.getAttribute('href') || image.getAttributeNS('http://www.w3.org/1999/xlink', 'href') || '';
        if (!href.startsWith('data:')) throw Error('SVG contains an unresolved image resource');
      }
      return new XMLSerializer().serializeToString(result);
    });
    if (/<foreignObject[\s>]/i.test(svg) || !/<text[\s>]/i.test(svg)) throw Error('Expected vector SVG with text, no foreignObject');
    // The companion is rendered from the actual SVG, never a separate imitation.
    await page.setContent(csp + '<style>html,body{margin:0;background:white}svg{display:block}</style>' + svg,
      { waitUntil: 'load', timeout: 15000 });
    const png = await page.locator('svg').screenshot({ type: 'png', timeout: 15000 });
    const receipt = {
      ...meta, sources, created_at: new Date().toISOString(), renderer: lock,
      runtime: { playwright: require('playwright/package.json').version, chromium: browser.version() },
      network: 'blocked', ...measurements,
      files: { html: { path: files.html, sha256: hash(Buffer.from(html)) },
        svg: { path: files.svg, sha256: hash(Buffer.from(svg)) }, png: { path: files.png, sha256: hash(png) } },
      visual_review: 'required', data_review: 'source hashes checked; author must verify claims and numbers',
    };
    fs.mkdirSync(path.dirname(target), { recursive: true });
    // Exclusive writes also protect a concurrent run using the same name.
    fs.writeFileSync(files.html, html, { flag: 'wx' });
    fs.writeFileSync(files.svg, svg, { flag: 'wx' });
    fs.writeFileSync(files.png, png, { flag: 'wx' });
    fs.writeFileSync(files.json, JSON.stringify(receipt, null, 2), { flag: 'wx' });
    return receipt;
  } finally { await browser.close(); }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const values = {};
    for (let i = 2; i < process.argv.length; i += 2) {
      const key = process.argv[i];
      if (!['--input', '--meta', '--output', '--browser', '--modules-root'].includes(key) || !process.argv[i + 1]) {
        throw Error('Usage visualizer.mjs --input section.html --meta section.json --output out/section [--browser existing-chromium]');
      }
      values[key] = process.argv[i + 1];
    }
    if (!values['--input'] || !values['--meta'] || !values['--output']) throw Error('input, meta and output are required');
    const receipt = await render({ input: values['--input'], metaFile: values['--meta'], output: values['--output'],
      browserPath: values['--browser'], modulesRoot: values['--modules-root'] });
    console.log(JSON.stringify({ png: receipt.files.png.path, svg: receipt.files.svg.path, width: receipt.width, height: receipt.height }));
  } catch (error) { console.error(`Visualizer failed: ${error.message}`); process.exitCode = 1; }
}
