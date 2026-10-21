/** Offline fixed-proposal HTML -> per-section PNG/SVG, chart PNG/SVG and PDF. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const plugin = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');

export async function renderGallery({ input, output, modulesRoot, browserPath }) {
  const html = fs.readFileSync(input, 'utf8');
  // Evidence-rich proposals embed several local screenshots as base64.
  if (Buffer.byteLength(html) > 64_000_000) throw Error('HTML exceeds 64 MB; resize evidence images');
  const out = path.resolve(output);
  const dependencyRoot = modulesRoot || path.resolve(path.dirname(process.execPath), '../node_modules');
  const require = createRequire(path.join(dependencyRoot, 'playwright/package.json'));
  const { chromium } = require('playwright');
  const executablePath = [browserPath, process.env.OFFERIST_BROWSER, chromium.executablePath(),
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/chromium', '/usr/bin/chromium-browser'].find(value => value && fs.existsSync(value));
  if (!executablePath) throw Error('No existing Chromium. No browser will be downloaded.');
  const assetRoot = path.join(plugin, 'skills/offerist-visualizer/assets');
  const lock = JSON.parse(fs.readFileSync(path.join(assetRoot, 'renderer.json'), 'utf8'));
  const bundle = fs.readFileSync(path.join(assetRoot, lock.file), 'utf8');
  if (hash(Buffer.from(bundle)) !== lock.sha256) throw Error('SVG renderer integrity mismatch');
  const browser = await chromium.launch({ executablePath, headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1200, height: 1300 }, deviceScaleFactor: 2 });
    await context.route('**/*', route => route.abort());
    const page = await context.newPage();
    await page.evaluate(source => {
      const doc = new DOMParser().parseFromString(source, 'text/html');
      if (doc.querySelector('script,iframe,object,embed,form,canvas,video,audio,img')) throw Error('Only inert generated HTML/SVG is supported');
      for (const element of doc.querySelectorAll('*')) for (const attr of element.attributes) {
        if (/^on/i.test(attr.name) || /javascript:/i.test(attr.value)) throw Error('Active content is unsupported');
      }
      for (const element of doc.querySelectorAll('image')) {
        const value = element.getAttribute('href') || element.getAttribute('xlink:href') || '';
        if (!/^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/=\s]+$/.test(value)) {
          throw Error('Only embedded local PNG/JPEG visual images are supported');
        }
      }
    }, html);
    await page.setContent(html, { waitUntil: 'load' });
    await page.evaluate(() => document.fonts.ready);
    const measurements = await page.evaluate(() => [...document.querySelectorAll('[data-section]')].map(root => {
      const rect = root.getBoundingClientRect();
      const horizontal = [...root.querySelectorAll('*')].filter(node => {
        const box = node.getBoundingClientRect();
        if (!box.width && !box.height) return false;
        return box.left < rect.left - 1 || box.right > rect.right + 1;
      }).map(node => ({ tag: node.tagName, text: node.textContent.slice(0, 80) }));
      const vertical = [...root.querySelectorAll('*')].filter(node => {
        const box = node.getBoundingClientRect();
        if (!box.width && !box.height) return false;
        return box.top < rect.top - 1 || box.bottom > rect.bottom + 1;
      }).map(node => ({ tag: node.tagName, text: node.textContent.slice(0, 80) }));
      return { id: root.dataset.section, width: rect.width, height: rect.height,
        overflow: root.scrollWidth > root.clientWidth + 2 || horizontal.length > 0 || vertical.length > 0,
        outside: [...horizontal, ...vertical], demo: root.innerText.includes('DEMO'), text: root.innerText,
        tableCount: root.querySelectorAll('table').length };
    }));
    if (!measurements.length) throw Error('No proposal sections found');
    if (measurements.some(item => !/^[a-z][a-z0-9_]*$/.test(item.id))) throw Error('Invalid section id');
    if (new Set(measurements.map(item => item.id)).size !== measurements.length) throw Error('Duplicate section id');
    const failures = measurements.filter(item => item.overflow);
    if (failures.length) throw Error('Layout overflow: ' + JSON.stringify(failures.map(({ id, outside }) => ({ id, outside }))));
    if (measurements.some(item => item.text.includes('\uFFFD'))) throw Error('Invalid Unicode replacement character');
    fs.mkdirSync(path.join(out, 'pages'), { recursive: true });
    fs.mkdirSync(path.join(out, 'visuals'), { recursive: true });
    await page.evaluate(bundle);
    const files = [];
    for (const item of measurements) {
      const locator = page.locator(`[data-section="${item.id}"]`);
      const png = path.join(out, 'pages', `${item.id}.png`);
      await locator.screenshot({ path: png, type: 'png', animations: 'disabled' });
      const svg = await locator.evaluate(root => {
        const result = window.offeristToSVG(root);
        return new XMLSerializer().serializeToString(result);
      });
      if (/<foreignObject[\s>]/i.test(svg) || !/<text[\s>]/i.test(svg)) throw Error(`Non-vector page ${item.id}`);
      const svgPath = path.join(out, 'pages', `${item.id}.svg`);
      fs.writeFileSync(svgPath, svg, 'utf8');
      const visuals = locator.locator('[data-visual]');
      const charts = [];
      const visualKeys = new Set();
      for (let index = 0, count = await visuals.count(); index < count; index += 1) {
        const visual = visuals.nth(index);
        const visualKey = await visual.getAttribute('data-visual');
        if (!visualKey || visualKeys.has(visualKey)) throw Error(`Duplicate/missing visual identity in ${item.id}`);
        visualKeys.add(visualKey);
        const visualSvg = await visual.evaluate(root => new XMLSerializer().serializeToString(root));
        // Keep the canonical chart filename for native Docs/DOCX. Evidence
        // indices are safe on Windows; authored keys never become path parts.
        const canonical = visualKey === item.id;
        const base = path.join(out, 'visuals', canonical
          ? `visual-${item.id}` : `evidence-${item.id}-${index + 1}`);
        fs.writeFileSync(`${base}.svg`, visualSvg, 'utf8');
        await visual.screenshot({ path: `${base}.png`, type: 'png' });
        charts.push({ key: visualKey, role: canonical ? 'visual' : 'evidence',
          svg: `${base}.svg`, png: `${base}.png` });
      }
      files.push({ section: item.id, png, svg: svgPath, charts, width: item.width, height: item.height,
        a4PagesApprox: Math.ceil((item.height - 1) / 1122.52), tables: item.tableCount, demo: item.demo });
    }
    const pdf = path.join(out, 'catalog.pdf');
    const footerText = measurements.every(item => item.demo) ? 'DEMO · Демонстрационный пример · Офферист' : '';
    await page.pdf({ path: pdf, printBackground: true, preferCSSPageSize: true,
      displayHeaderFooter: true, headerTemplate: '<div></div>',
      footerTemplate: `<div style="font:8px Arial;width:100%;padding:0 72px;color:#52606D;display:flex;justify-content:space-between"><span>${footerText}</span><span class="pageNumber"></span></div>`,
      margin: { top: '0.75in', bottom: '0.75in', left: '0.75in', right: '0.75in' } });
    const receipt = { input: path.resolve(input), inputSha256: hash(Buffer.from(html)),
      network: 'blocked', chromium: browser.version(), renderer: lock, sections: files, pdf,
      generatedAt: new Date().toISOString(), visualReview: 'required',
      checks: { horizontalOverflow: 0, verticalOverflow: 0, unicodeReplacement: 0, sectionCount: measurements.length,
        allDemoMarked: measurements.every(item => item.demo), noContentClipping: true } };
    fs.writeFileSync(path.join(out, 'gallery-manifest.json'), JSON.stringify(receipt, null, 2), 'utf8');
    return receipt;
  } finally {
    await browser.close();
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const values = {};
    for (let i = 2; i < process.argv.length; i += 2) {
      if (!['--input', '--output', '--modules-root', '--browser'].includes(process.argv[i]) || !process.argv[i + 1]) {
        throw Error('Usage: --input gallery.html --output folder [--modules-root path] [--browser path]');
      }
      values[process.argv[i]] = process.argv[i + 1];
    }
    if (!values['--input'] || !values['--output']) throw Error('input and output are required');
    const receipt = await renderGallery({ input: values['--input'], output: values['--output'],
      modulesRoot: values['--modules-root'], browserPath: values['--browser'] });
    console.log(JSON.stringify({ sections: receipt.sections.length, pdf: receipt.pdf, checks: receipt.checks }));
  } catch (error) {
    console.error(`Proposal gallery failed: ${error.message}`);
    process.exitCode = 1;
  }
}
