import { readFile, writeFile, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { analyzeBarInput } from "./adapter.mjs";

export async function runCli(argv) {
  let inputPath = "", outputPath = "";
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--input") inputPath = argv[++index] ?? "";
    else if (argv[index] === "--out") outputPath = argv[++index] ?? "";
    else throw new Error(`Unknown argument: ${argv[index]}`);
  }
  if (!inputPath || !outputPath) throw new Error("Usage: node cli.mjs --input INPUT.json --out OUTPUT.json");
  const source = resolve(inputPath);
  const target = resolve(outputPath);
  if (source === target) throw new Error("Input and output paths must differ");
  const input = JSON.parse(await readFile(source, "utf8"));
  const output = await analyzeBarInput(input);
  await mkdir(dirname(target), { recursive: true });
  await writeFile(target, `${JSON.stringify(output, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify({ status: output.status, mode: output.mode, out: target })}\n`);
  return output;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  runCli(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  });
}
