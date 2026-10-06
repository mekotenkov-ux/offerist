import { readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { builtinModules } from "node:module";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const offerist = resolve(here, "../..");
const manifestPath = join(here, "manifest.json");
const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
const depsArg = process.argv.indexOf("--deps");
if (depsArg < 0 || !process.argv[depsArg + 1]) throw new Error("Usage: node build.mjs --deps PATH_TO_INSTALLED_NODE_MODULES");
const deps = resolve(process.argv[depsArg + 1]);
const sha = (data) => createHash("sha256").update(data).digest("hex");

for (const [relative, expected] of Object.entries(manifest.source_sha256)) {
  const actual = sha(await readFile(join(here, relative)));
  if (actual !== expected) throw new Error(`Source hash mismatch: ${relative}`);
}
for (const [name, version] of Object.entries(manifest.dependencies)) {
  const metadata = JSON.parse(await readFile(join(deps, name, "package.json"), "utf8"));
  if (metadata.version !== version) throw new Error(`Dependency version mismatch: ${name}`);
}
for (const [name, expected] of Object.entries(manifest.dictionary_sha256)) {
  const actual = sha(await readFile(join(offerist, "assets/bar/dicts", name)));
  if (actual !== expected) throw new Error(`Dictionary hash mismatch: ${name}`);
}
const { build } = await import(pathToFileURL(join(deps, "esbuild/lib/main.js")).href);
const licenseNames = ["BAR-LICENSE", "az-MIT-NOTICE", "snowball-ISC-NOTICE", "stopword-LICENSE", "OpenCorpora-CC-BY-SA-3.0-NOTICE"];
const notices = await Promise.all(licenseNames.map((name) => readFile(join(offerist, "assets/bar/licenses", name), "utf8")));
const outfile = join(here, "core.bundle.cjs");
await build({
  entryPoints: [join(here, "source/semantic-analysis.ts")],
  outfile,
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "node22",
  nodePaths: [deps],
  minifyWhitespace: true,
  legalComments: "none",
  banner: { js: `/*\n${notices.join("\n---\n").replaceAll("*/", "* /\n")}\n*/` },
  metafile: true,
}).then(async (result) => {
  const builtins = new Set(builtinModules.map((name) => name.replace(/^node:/, "")));
  const externals = Object.values(result.metafile.inputs).flatMap((input) => input.imports.filter((item) => item.external && !builtins.has(item.path.replace(/^node:/, ""))));
  if (externals.length) throw new Error(`Unbundled imports: ${externals.map((item) => item.path).join(", ")}`);
});
const dependencyPrefix = `${relative(here, deps).replaceAll("\\", "/")}/`;
const generated = await readFile(outfile, "utf8");
const portable = generated.replaceAll(dependencyPrefix, "@bundled/");
if (portable.includes(dependencyPrefix)) throw new Error("Build dependency path leaked into bundle");
await writeFile(outfile, portable, "utf8");
const bundleHash = sha(await readFile(outfile));
manifest.bundle_sha256 = bundleHash;
await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
process.stdout.write(`BAR bundle ${bundleHash}\n`);
