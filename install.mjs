/**
 * Place this plugin where a bare `@deepseek-ai/*` import resolves, because that is the only place the
 * judge can work.
 *
 * A bare specifier resolves against the importing module's own location, so the source in this
 * directory cannot import `@deepseek-ai/dsh-llm` however it is loaded -- not by absolute `file:///`
 * URL, and not through a junction, since Node resolves ESM against the realpath. A package simply has
 * to exist under a `node_modules` that reaches the harness. That is why the pilot could load the arm
 * selector by URL but the judge cannot.
 *
 *     node routing/plugin/dsh-router-laya/install.mjs <module-root>
 *
 * Copies (never links), so the two installs -- the isolated harness profile and the real profile --
 * stay independent and either can be deleted without touching the other or this checkout.
 *
 * It also writes a `package.json` when one is missing: the real profile's module root is populated by
 * pnpm and has no entry for a local plugin.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const PACKAGE = 'dsh-router-laya';
const here = path.dirname(fileURLToPath(import.meta.url));
const target = process.argv[2];
if (!target) {
  console.error('usage: node routing/plugin/dsh-router-laya/install.mjs <module-root>');
  process.exit(2);
}

const dest = path.join(path.resolve(target), PACKAGE);
fs.mkdirSync(dest, { recursive: true });
// `package.json` is copied rather than synthesised: it now carries `dsh.client` and the `./client`
// export, which is what makes the browser half discoverable at all.
for (const file of ['index.js', 'client.js', 'cordis.patch.yml', 'package.json']) {
  fs.copyFileSync(path.join(here, file), path.join(dest, file));
}
console.log(`installed ${PACKAGE} -> ${dest}`);
