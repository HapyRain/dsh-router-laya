#!/usr/bin/env node
/**
 * Download the dsh-router-laya judge checkpoint (~846 MB) into `weights/model/`.
 *
 * Zero npm dependencies (Node >= 18 builtins only: fetch, fs, crypto, stream). The checkpoint is
 * NOT in the npm package -- 842 MB exceeds npm's limits and would re-download on every plugin
 * update -- so this script runs once at setup time and again whenever the manifest gains files.
 *
 *     node weights/fetch.mjs [--dry-run] [--dest <dir>] [--repo <id>] [--revision <rev>]
 *
 * Behaviour, in the order that matters when a network misbehaves:
 *   - Endpoints are tried in order: `$HF_ENDPOINT` (when set) -> `https://huggingface.co`
 *     -> `https://hf-mirror.com`. hf.co is unreachable on some networks (observed on CN
 *     networks) while the mirror is not, so the mirror is the default fallback, not a manual
 *     step (docs/marketplace-review.md §3.4).
 *   - Files land as `<dest>/<path>.part` and are streamed with HTTP Range on resume, so an
 *     interrupted 842 MB transfer continues instead of restarting.
 *   - Every finished file is sha256-verified against `weights/manifest.json`; a mismatch
 *     deletes the file and falls through to the next endpoint. A file already present AND
 *     hash-correct is skipped, which is what makes repeated setup runs cheap.
 *
 * The checkpoint is self-contained (docs/marketplace-review.md §3.4): `model.safetensors` holds
 * every parameter including the ModernBERT-large encoder, and the tokenizer + encoder configs
 * ship inside the same download, so setup needs exactly this one fetch -- never a base-model
 * download on top.
 */
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { Readable } from 'node:stream';
import { Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_ENDPOINTS = ['https://huggingface.co', 'https://hf-mirror.com'];
const DEFAULT_DEST = path.join(here, 'model');

// --- argument parsing (small on purpose; this runs once per machine) -------------------------

const args = process.argv.slice(2);
function argValue(flag) {
  const i = args.indexOf(flag);
  return i !== -1 && i + 1 < args.length ? args[i + 1] : undefined;
}
const dryRun = args.includes('--dry-run');
const help = args.includes('--help') || args.includes('-h');

if (help) {
  console.log(`usage: node weights/fetch.mjs [--dry-run] [--dest <dir>] [--repo <id>] [--revision <rev>]

  --dry-run        print the download plan (endpoints, files, hashes) and exit
  --dest <dir>     download target (default: <package>/weights/model)
  --repo <id>      Hugging Face repo id (default: manifest's "repo" field)
  --revision <rev> branch/tag/commit (default: manifest's "revision" field)

env: HF_ENDPOINT (tried first, before the built-in endpoints)`);
  process.exit(0);
}

// --- plan -------------------------------------------------------------------------------------

const manifest = JSON.parse(fs.readFileSync(path.join(here, 'manifest.json'), 'utf8'));
const repo = argValue('--repo') || process.env.LAYA_WEIGHTS_REPO || manifest.repo;
if (!repo) {
  console.error('fetch: no repo id -- pass --repo <id> or set "repo" in weights/manifest.json');
  process.exit(2);
}
const revision = argValue('--revision') || manifest.revision || 'main';
const dest = path.resolve(argValue('--dest') || DEFAULT_DEST);

// GitHub Releases is tried FIRST when the manifest declares a release base (the project's own
// distribution channel: versioned, no auth for public repos, GitHub-native). Each manifest file is
// fetched as `<releaseBase>/<file>`; failure falls through to the HF chain below. The HF chain
// remains the portable fallback (HF_ENDPOINT -> hf.co -> hf-mirror.com).
const releaseBase = (manifest.release_base || '').replace(/\/+$/, '');

// `$HF_ENDPOINT` first when set, then the built-ins, deduplicated in order.
const endpoints = [...new Set(
  [process.env.HF_ENDPOINT, ...DEFAULT_ENDPOINTS].filter(Boolean).map((s) => s.replace(/\/+$/, '')),
)];

function fileUrl(endpoint, relPath) {
  return `${endpoint}/${repo}/resolve/${revision}/${relPath.split('/').map(encodeURIComponent).join('/')}`;
}

function sha256File(file) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash('sha256');
    fs.createReadStream(file)
      .on('data', (chunk) => hash.update(chunk))
      .on('end', () => resolve(hash.digest('hex')))
      .on('error', reject);
  });
}

/** Download one file with `.part` resume; resolves when the part file is complete. */
async function downloadFile(url, relPath, bytes) {
  const part = path.join(dest, relPath + '.part');
  await fs.promises.mkdir(path.dirname(part), { recursive: true });
  let start = fs.existsSync(part) ? fs.statSync(part).size : 0;
  if (start > bytes) {
    fs.rmSync(part); // a stale part from an older manifest is worse than none
    start = 0;
  }

  const headers = start > 0 ? { Range: `bytes=${start}-` } : {};
  const res = await fetch(url, { headers, redirect: 'follow' });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
  if (res.status === 200 && start > 0) {
    start = 0; // server ignored the Range header; restart rather than corrupt
  }
  const expectedStatus = start > 0 ? 206 : 200;
  if (res.status !== expectedStatus) {
    throw new Error(`HTTP ${res.status} (wanted ${expectedStatus})`);
  }

  // Progress line every ~64 MB with a crude ETA (good enough for an 842 MB one-off).
  let received = start;
  let nextMark = Math.floor(received / 67108864) * 67108864 + 67108864;
  const t0 = Date.now();
  const counter = new Transform({
    transform(chunk, _enc, cb) {
      received += chunk.length;
      if (received >= nextMark) {
        nextMark = received + 67108864;
        const rate = (received - start) / Math.max(1, Date.now() - t0); // B/ms == MB/s
        const etaMin = rate > 0 ? ((bytes - received) / rate / 60000).toFixed(1) : '?';
        process.stdout.write(`  ${(received / 1e6).toFixed(0)}/${(bytes / 1e6).toFixed(0)} MB  eta ~${etaMin} min\r`);
      }
      cb(null, chunk);
    },
  });
  await pipeline(Readable.fromWeb(res.body), counter, fs.createWriteStream(part, { flags: start > 0 ? 'a' : 'w' }));
  process.stdout.write('\n');

  const size = fs.statSync(part).size;
  if (size !== bytes) {
    throw new Error(`incomplete: ${size}/${bytes} bytes`);
  }
  fs.renameSync(part, path.join(dest, relPath));
}

// --- run --------------------------------------------------------------------------------------

console.log(`laya-router-7q weights -> ${dest}`);
console.log(`repo: ${repo}@${revision}`);
console.log('endpoint order (first failure falls through to the next):');
let epLog = [...endpoints];
if (releaseBase) epLog = [`github-release: ${releaseBase}`, ...epLog];
epLog.forEach((ep, i) => console.log(`  ${i + 1}. ${ep}`));
console.log(`files: ${manifest.files.length} (${(manifest.totalBytes / 1e6).toFixed(1)} MB total)`);

if (dryRun) {
  for (const f of manifest.files) {
    const local = path.join(dest, f.path);
    const present = fs.existsSync(local) && fs.statSync(local).size === f.bytes ? 'present, will hash-verify' : 'missing, will download';
    console.log(`  ${f.path}  ${f.bytes} B  sha256=${f.sha256.slice(0, 12)}...  [${present}]`);
    console.log(`    from ${releaseBase ? releaseBase + '/' + f.path + ' then fallbacks' : fileUrl(endpoints[0], f.path) + ' then fallbacks'}`);
  }
  console.log('dry-run: nothing downloaded.');
  process.exit(0);
}

const failures = [];
for (const f of manifest.files) {
  const target = path.join(dest, f.path);
  if (fs.existsSync(target)) {
    const got = await sha256File(target);
    if (got === f.sha256) {
      console.log(`ok (cached): ${f.path}`);
      continue;
    }
    console.log(`hash mismatch on existing ${f.path} -- refetching`);
    fs.rmSync(target);
  }

  // Source chain: GitHub Release (project's own channel) first when declared, then the HF chain.
  // Release asset names are FLAT (GitHub forbids '/' in asset names): `encoder/config.json` is
  // uploaded as `encoder__config.json`, so the release source maps manifest paths accordingly.
  const assetName = f.path.split('/').join('__');
  const sources = [
    ...(releaseBase ? [{ label: 'github-release', url: `${releaseBase}/${assetName}` }] : []),
    ...endpoints.map((ep) => ({ label: ep, url: fileUrl(ep, f.path) })),
  ];
  let done = false;
  let lastError;
  for (const src of sources) {
    const url = src.url;
    try {
      console.log(`fetch ${f.path} from ${src.label} ...`);
      await downloadFile(url, f.path, f.bytes);
      const got = await sha256File(target);
      if (got !== f.sha256) {
        fs.rmSync(target);
        throw new Error(`sha256 mismatch (got ${got.slice(0, 12)}..., want ${f.sha256.slice(0, 12)}...)`);
      }
      console.log(`ok: ${f.path} (${(f.bytes / 1e6).toFixed(1)} MB, sha256 verified)`);
      done = true;
      break;
    } catch (e) {
      lastError = e;
      console.log(`  failed: ${e.message} -- trying next endpoint`);
    }
  }
  if (!done) {
    failures.push({ file: f.path, error: lastError ? lastError.message : 'all endpoints failed' });
  }
}

if (failures.length > 0) {
  console.error('\nfetch FAILED for:');
  for (const f of failures) console.error(`  ${f.file}: ${f.error}`);
  console.error(`\nmanual fallback: place the files under ${dest} yourself (names + sha256 in`);
  console.error('weights/manifest.json), or retry with another endpoint:');
  console.error('  HF_ENDPOINT=https://your-mirror node weights/fetch.mjs');
  process.exit(1);
}
console.log('all weights present and verified.');
