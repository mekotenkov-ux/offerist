import { build } from 'esbuild';
await build({absWorkingDir:process.cwd(),tsconfigRaw:{},stdin:{contents:"import {elementToSVG} from 'dom-to-svg'; window.offeristToSVG=elementToSVG;",resolveDir:process.cwd()},bundle:true,platform:'browser',format:'iife',target:'es2020',minify:true,legalComments:'eof',outfile:'../dom-to-svg-0.12.2.browser.js'});
