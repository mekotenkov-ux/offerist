// Node adaptation of BAR lib/russian-morphology.ts. The original Vite ?url
// imports are replaced by a path to copied Az.Morph dictionaries. Morphology
// initialization and lemma selection preserve the upstream behavior.
import { access } from "node:fs/promises";
import { join, resolve } from "node:path";

declare const __dirname: string;

type AzParse = { word?: string; normalize?: () => AzParse | false };
type AzMorph = {
  (word: string): AzParse[];
  init: (path: string, callback: (error?: Error | null) => void) => void;
};
type AzModule = { Morph: AzMorph };

let morphology: AzMorph | null = null;
let initialization: Promise<void> | null = null;

export function initializeRussianMorphology() {
  if (morphology) return Promise.resolve();
  if (initialization) return initialization;
  initialization = import("az").then(async (module) => {
    const az = ((module as { default?: AzModule }).default ?? module) as AzModule;
    const dictionaries = resolve(__dirname, "../../assets/bar/dicts");
    await access(join(dictionaries, "words.dawg"));
    await new Promise<void>((done, fail) => az.Morph.init(dictionaries, (error) => error ? fail(error) : done()));
    morphology = az.Morph;
  }).catch((error) => {
    initialization = null;
    throw error;
  });
  return initialization;
}

export function russianLemma(word: string) {
  if (!morphology) throw new Error("Русская морфология не инициализирована");
  const normalized = morphology(word)[0]?.normalize?.();
  return normalized && normalized.word ? normalized.word : word;
}
