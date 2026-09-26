#!/usr/bin/env node
/**
 * `npx dsh-router-laya setup` -- one-shot bootstrap for the judge service the plugin routes with.
 *
 * Four steps, fail-fast, each with a fix hint, and safe to re-run (idempotent):
 *
 *   a. python >= 3.10 -> create `.venv-router` -> pip install -r service/requirements.lock.txt
 *   b. node weights/fetch.mjs              (the ~846 MB checkpoint; --skip-weights to skip)
 *   c. print the plugin registration guide (profiles found, verbatim patch snippet, env vars)
 *   d. start the judge service and wait for /health (reuses service/start_router.*)
 *
 * It deliberately does NOT edit any cordis/profile config: step c prints the exact snippet for
 * the user to paste (docs/marketplace-review.md §4 -- an installer that rewrites the user's patch
 * file is how that file rotted twice before, docs/HANDOFF-20260925.md §5.1).
 *
 * Flags:
 *   --dry-run       print every step's plan, execute nothing (python probe and reads aside)
 *   --skip-weights  skip step b (weights already in place, or served another way)
 *   --skip-venv     skip step a entirely (no venv, no pip; launch uses LAYA_PYTHON/PATH python)
 *   --help          this text
 *
 * Environment: LAYA_PYTHON, LAYA_DEVICE, LAYA_ROUTER_URL (port the service binds), HF_ENDPOINT,
 * LAYA_START_TIMEOUT (health-wait seconds passed to the launcher, default 180 here -- CPU cold
 * loads take ~70s, and the launcher's own default of 60s is tuned for the warm dev flow).
 */
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url)); // <package>/bin
const pkg = path.dirname(here);
const VENV_DIR = path.join(pkg, '.venv-router');
const LOCK_FILE = path.join(pkg, 'service', 'requirements.lock.txt');
const FETCH_SCRIPT = path.join(pkg, 'weights', 'fetch.mjs');
const SERVICE_SCRIPT = path.join(pkg, 'service', 'laya_router.py');

const args = process.argv.slice(2);
const dryRun = args.includes('--dry-run');
const skipWeights = args.includes('--skip-weights');
const skipVenv = args.includes('--skip-venv');
const help = args.includes('--help') || args.includes('-h');

if (help) {
  console.log(`usage: npx dsh-router-laya setup [--dry-run] [--skip-weights] [--skip-venv]

  a. python >= 3.10 -> .venv-router -> pip install -r service/requirements.lock.txt
  b. node weights/fetch.mjs           (checkpoint download, ~846 MB, resumable, sha256-verified)
  c. print plugin registration guide  (no config is edited -- the snippet is for you to paste)
  d. start the judge service + /health poll

env: LAYA_PYTHON, LAYA_DEVICE, LAYA_ROUTER_URL (default http://127.0.0.1:8765/judge),
     HF_ENDPOINT, LAYA_START_TIMEOUT (default 180s for this script's cold-start path)`);
  process.exit(0);
}

function fail(step, message, hint) {
  console.error(`\n[setup] step ${step} FAILED: ${message}`);
  if (hint) console.error(`[setup] fix: ${hint}`);
  process.exit(1);
}

function note(message) {
  console.log(`[setup] ${message}`);
}

// ── step a: python + venv + pinned requirements ──────────────────────────────────────────────

function pythonCandidates() {
  const cands = [];
  if (process.env.LAYA_PYTHON) cands.push({ cmd: process.env.LAYA_PYTHON, base: [] });
  if (process.platform === 'win32') cands.push({ cmd: 'py', base: ['-3'] });
  cands.push({ cmd: 'python3', base: [] }, { cmd: 'python', base: [] });
  return cands;
}

function probePython(cand) {
  const code = 'import sys; print(".".join(map(str, sys.version_info[:3]))); print(sys.executable)';
  const r = spawnSync(cand.cmd, [...cand.base, '-c', code], { encoding: 'utf8', timeout: 15000 });
  if (r.status !== 0 || !r.stdout) return null;
  const [version, exe] = r.stdout.trim().split(/\r?\n/);
  const parts = version.split('.').map((n) => parseInt(n, 10));
  if (parts.length < 2 || parts.some(Number.isNaN)) return null;
  return { version, exe: exe || cand.cmd, major: parts[0], minor: parts[1] };
}

const venvPython = process.platform === 'win32'
  ? path.join(VENV_DIR, 'Scripts', 'python.exe')
  : path.join(VENV_DIR, 'bin', 'python');

function stepA() {
  console.log('\n== step a: python >= 3.10 + .venv-router + pinned requirements ==');
  if (skipVenv) {
    note('--skip-venv: not creating a venv; launch will use LAYA_PYTHON or PATH python.');
    return;
  }

  let probe = null;
  for (const cand of pythonCandidates()) {
    probe = probePython(cand);
    if (probe) {
      note(`python ${probe.version} at ${probe.exe}`);
      break;
    }
  }
  if (!probe) {
    fail('a', 'no usable python interpreter found',
      'install Python 3.10-3.13 from python.org (3.12 recommended) and re-run; '
      + 'or point LAYA_PYTHON at an existing interpreter');
  }
  if (probe.major < 3 || (probe.major === 3 && probe.minor < 10)) {
    fail('a', `python ${probe.version} is too old (need >= 3.10, 3.12 recommended)`,
      'install a newer Python from python.org, or point LAYA_PYTHON at one');
  }

  if (fs.existsSync(venvPython)) {
    note(`venv already exists: ${VENV_DIR}`);
  } else {
    note(`creating venv: ${probe.exe} -m venv ${VENV_DIR}`);
    if (!dryRun) {
      const r = spawnSync(probe.exe, [...probe.base, '-m', 'venv', VENV_DIR], { stdio: 'inherit' });
      if (r.status !== 0 || !fs.existsSync(venvPython)) {
        fail('a', `venv creation exited ${r.status}`,
          'on Debian/Ubuntu install python3-venv first; then re-run setup');
      }
    }
  }

  // The marker records the lock file's mtime, so editing requirements.lock.txt invalidates it.
  const marker = path.join(VENV_DIR, '.laya-reqs-ok');
  const lockStamp = String(fs.statSync(LOCK_FILE).mtimeMs);
  if (fs.existsSync(marker) && fs.readFileSync(marker, 'utf8') === lockStamp) {
    note('requirements already installed (marker matches requirements.lock.txt)');
    return;
  }
  const pipCmd = `${venvPython} -m pip install -r ${LOCK_FILE}`;
  note(pipCmd);
  if (!dryRun) {
    const r = spawnSync(venvPython, ['-m', 'pip', 'install', '-r', LOCK_FILE], { stdio: 'inherit' });
    if (r.status !== 0) {
      fail('a', `pip install exited ${r.status}`,
        'check the pip output above (proxy? disk space?); GPU users: swap the torch line in '
        + 'service/requirements.lock.txt for a CUDA wheel, then re-run');
    }
    fs.writeFileSync(marker, lockStamp);
  }
}

// ── step b: checkpoint download ───────────────────────────────────────────────────────────────

function stepB() {
  console.log('\n== step b: judge checkpoint (~846 MB, once) ==');
  if (skipWeights) {
    note('--skip-weights: leaving weights/ as-is.');
    return;
  }
  const manifest = JSON.parse(fs.readFileSync(path.join(pkg, 'weights', 'manifest.json'), 'utf8'));
  note(`repo ${manifest.repo}@${manifest.revision}, ${manifest.files.length} files, `
    + `${(manifest.totalBytes / 1e6).toFixed(0)} MB into weights/model/`);
  note('endpoints: $HF_ENDPOINT (if set) -> https://huggingface.co -> https://hf-mirror.com');
  if (!dryRun) {
    const r = spawnSync(process.execPath, [FETCH_SCRIPT], { stdio: 'inherit' });
    if (r.status !== 0) {
      fail('b', `weights fetch exited ${r.status}`,
        'set HF_ENDPOINT to a reachable mirror and re-run; or place the files into '
        + 'weights/model/ by hand (names + sha256 in weights/manifest.json)');
    }
  }
}

// ── step c: registration guide (read-only; prints, never edits) ──────────────────────────────

function servicePythonForSnippet() {
  if (fs.existsSync(venvPython)) return venvPython;
  if (process.env.LAYA_PYTHON) return process.env.LAYA_PYTHON;
  const probe = pythonCandidates().map(probePython).find(Boolean);
  return probe ? probe.exe : '<python>';
}

function stepC() {
  console.log('\n== step c: register the plugin (guidance only -- nothing is edited) ==');
  const profilesDir = path.join(os.homedir(), '.dsh', 'profiles');
  if (fs.existsSync(profilesDir)) {
    const profiles = fs.readdirSync(profilesDir, { withFileTypes: true })
      .filter((d) => d.isDirectory()).map((d) => d.name);
    note(`profiles under ${profilesDir}: ${profiles.length ? profiles.join(', ') : '(none)'}`);
    note("each profile's module root is ~/.dsh/profiles/<name>/node_modules -- register there.");
  } else {
    note(`no profiles directory at ${profilesDir} -- run 'dsh' once to create one, then re-run.`);
  }
  const py = servicePythonForSnippet();
  // Single-quoted YAML scalars treat backslashes literally -- emit Windows paths verbatim.
  console.log(`
Paste this row into the profile's cordis patch (verbatim; only add, never rewrite the file):

    - insert:
        - id: router-laya
          name: 'dsh-router-laya'
          config:
            auto: true
            servicePython: '${py}'
            serviceScript: '${SERVICE_SCRIPT}'

`);
  note('servicePython/serviceScript above let the plugin auto-start the judge (config.autoStart '
    + 'defaults on; set autoStart: false to opt out).');
  note('environment switches, read by the plugin at apply() time (a row config cannot set them):');
  note('  ROUTEEXP_ARM=auto              -- enables auto mode when the row omits `auto: true`');
  note('  ROUTEEXP_RESPECT_EXPLICIT=1    -- requests with an explicit effort keep it (recommended)');
}

// ── step d: start the service + /health poll ──────────────────────────────────────────────────

function judgeBase() {
  const raw = process.env.LAYA_ROUTER_URL || 'http://127.0.0.1:8765/judge';
  return String(raw).replace(/\/judge\/?$/, '');
}

async function healthOk(base, timeoutMs = 2000) {
  try {
    const res = await fetch(`${base}/health`, { signal: AbortSignal.timeout(timeoutMs) });
    if (!res.ok) return null;
    const info = await res.json();
    return info && info.protocol === 'finetuned' ? info : null;
  } catch {
    return null;
  }
}

async function stepD() {
  console.log('\n== step d: judge service + /health ==');
  const base = judgeBase();
  const port = new URL(base).port || '80';
  const launcher = process.platform === 'win32'
    ? { cmd: 'powershell.exe', args: ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        path.join(pkg, 'service', 'start_router.ps1'), '-Port', port] }
    : { cmd: 'bash', args: [path.join(pkg, 'service', 'start_router.sh'), '--port', port] };
  const env = { ...process.env, LAYA_START_TIMEOUT: process.env.LAYA_START_TIMEOUT || '180' };

  if (dryRun) {
    note(`dry-run: would probe ${base}/health, then run:`);
    console.log(`    ${launcher.cmd} ${launcher.args.join(' ')}`);
    note(`then poll ${base}/health (protocol must be "finetuned").`);
    return;
  }

  const up = await healthOk(base);
  if (up) {
    note(`already up at ${base} (protocol=finetuned, model=${up.model})`);
    return;
  }

  note(`starting via ${path.basename(launcher.cmd)} launcher on :${port} ...`);
  const r = spawnSync(launcher.cmd, launcher.args, { stdio: 'inherit', env });
  if (r.error && r.error.code === 'ENOENT') {
    fail('d', `${launcher.cmd} not found`,
      process.platform === 'win32'
        ? 'run powershell -File service/start_router.ps1 manually'
        : 'install bash, or run: LAYA_PYTHON=<python> bash service/start_router.sh');
  }
  if (r.status !== 0) {
    fail('d', `launcher exited ${r.status}`,
      `read the service log (%TEMP%/laya-router-service.err.log or /tmp/laya-router-service.err.log); `
      + 'a missing checkpoint or a broken pip install shows up here');
  }

  // The launcher already waited for /health; this re-probe is belt and braces for detached starts.
  const deadline = Date.now() + 15000;
  let info = null;
  while (Date.now() < deadline) {
    info = await healthOk(base, 1000);
    if (info) break;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  if (!info) {
    fail('d', `${base}/health never answered with protocol=finetuned`,
      'check the service log paths printed by the launcher');
  }
  note(`judge ready: ${base} (protocol=finetuned, model=${info.model})`);
}

// ── run ---------------------------------------------------------------------------------------

console.log(`dsh-router-laya setup -- package root: ${pkg}`);
if (dryRun) note('--dry-run: probing and printing only, nothing will be installed or started.');

stepA();
stepB();
stepC();
await stepD();

console.log('\n== setup done ==');
note('after registering the plugin row, restart dsh once (bundle layer needs a reboot, '
  + 'patchReload only covers the user patch layer).');
note('sanity check: curl the /health URL printed above, then send a message and look for '
  + "[router-laya] turn 1 auto ... -> low (laya) on the plugin's stderr.");
