// AUTO mode (plugin v2) pure-function checks: the tier table, mode resolution, and the session
// state machine -- everything that decides where a request lands, without needing DSH or the
// Python service. Plain-script check, repo convention (see parseArm.test.mjs).
//
//     node routing/plugin/dsh-router-laya/auto.test.mjs
import { isOurRoute, nextSessionState, resolveSchedule, serviceBaseUrl, serviceLaunchSpec, TIER_TABLE } from './index.js';

let pass = 0;
const fail = [];

function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (ok) pass++;
  else fail.push(`${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

// TIER_TABLE: the product policy -- low/high/max on deepseek-flash, fallback low. `max` is legal
// (adapter resolveThinking accepts off|low|high|max). Fallback row mirrors low (C3 default).
check('tiers low', TIER_TABLE.low, { provider: 'deepseek-official', model: 'deepseek-flash', effort: 'low' });
check('tiers high', TIER_TABLE.high, { provider: 'deepseek-official', model: 'deepseek-flash', effort: 'high' });
check('tiers max', TIER_TABLE.max, { provider: 'deepseek-official', model: 'deepseek-flash', effort: 'max' });
check('tiers fallback = low', TIER_TABLE.fallback, TIER_TABLE.low);

// resolveSchedule: 'auto' literal is checked BEFORE parseSchedule (a malformed arm elsewhere must
// still throw loud, but 'auto' itself must not reach the arm parser). Env restored afterwards.
function withArm(spec, fn) {
  const saved = process.env.ROUTEEXP_ARM;
  if (spec === undefined) delete process.env.ROUTEEXP_ARM;
  else process.env.ROUTEEXP_ARM = spec;
  try {
    fn(resolveSchedule());
  } finally {
    if (saved === undefined) delete process.env.ROUTEEXP_ARM;
    else process.env.ROUTEEXP_ARM = saved;
  }
}
check('auto literal', true, true); // placeholder ordering guard
withArm(undefined, (r) => check('resolve unset -> null', r, null));
withArm('deepseek-flash:low', (r) => check('resolve arm unchanged', r[0].effort, 'low'));
withArm('judge', (r) => check('resolve judge', r, 'judge'));
withArm('auto', (r) => check('resolve auto', r, 'auto'));
withArm('AUTO', (r) => check('resolve AUTO case-insensitive', r, 'auto'));

// Session state machine (nextSessionState):
// turn 1: no previous state, judgment present -> state advances to the judged tier/task.
let s = nextSessionState(undefined, 1, '帮我优化一下', { tier: 'low', triggered_by: 'laya' });
check('t1 fresh', s, { tier: 'low', task: '帮我优化一下', turn: 1 });

// turn 2: regenerate detected on the Python side (tier high) -> state advances to high.
s = nextSessionState(s, 2, '帮我优化一下', { tier: 'high', triggered_by: 'escalate_regenerate' });
check('t2 regen escalates', s, { tier: 'high', task: '帮我优化一下', turn: 2 });

// turn 3: second escalation caps at max.
s = nextSessionState(s, 3, '还是不行，再试一次', { tier: 'max', triggered_by: 'escalate_regenerate' });
check('t3 caps at max', s, { tier: 'max', task: '还是不行，再试一次', turn: 3 });

// new unrelated task: normal judgment replaces the state (no tier inheritance outside intent).
s = nextSessionState(s, 4, 'write a poem', { tier: 'low', triggered_by: 'laya' });
check('t4 new task resets tier by judgment', s, { tier: 'low', task: 'write a poem', turn: 4 });

// judgment FAILED (judge unreachable): keep the last known tier but advance task/turn -- the
// next regenerate check compares against what the user last asked, judged or not.
s = nextSessionState(s, 5, '帮我优化一下', null);
check('t5 failed judgment keeps tier, advances task/turn',
  s, { tier: 'low', task: '帮我优化一下', turn: 5 });

// turn went BACKWARDS (session reset / id reuse): stale prev_tier/prev_task must not leak
// into the fresh conversation (deepseek F4) -- the state is discarded and rebuilt from turn 1.
s = nextSessionState(s, 1, 'brand new conversation', { tier: 'low', triggered_by: 'laya' });
check('t-backwards resets state', s, { tier: 'low', task: 'brand new conversation', turn: 1 });

// judgment failed on a fresh conversation: tier falls back to low (the C3 default), state rebuilt.
s = nextSessionState(undefined, 1, '帮我优化一下', null);
check('t1 failed judgment -> low', s, { tier: 'low', task: '帮我优化一下', turn: 1 });

// isOurRoute (v2.1): the self-lock fix. An explicit effort matching what WE served this session is
// our own footprint -> re-route; anything foreign defers (deepseek live finding 2b).
const served = { tier: 'low', task: 'x', turn: 3 };
check('ours: effort+model match last served',
  isOurRoute({ reasoningEffort: 'low', model: 'deepseek-flash' }, served), true);
check('ours: foreign effort on same model',  // user pinned high while we served low
  isOurRoute({ reasoningEffort: 'high', model: 'deepseek-flash' }, served), false);
check('ours: no session state yet (t1)',  // nothing served -> any explicit effort is foreign
  isOurRoute({ reasoningEffort: 'low', model: 'deepseek-flash' }, undefined), false);
check('ours: config without effort',
  isOurRoute({ model: 'deepseek-flash' }, served), false);
check('ours: unknown tier in state',
  isOurRoute({ reasoningEffort: 'low', model: 'deepseek-flash' }, { tier: 'xhigh', task: 'x', turn: 1 }), false);
check('ours: model mismatch (delegation to another model)',
  isOurRoute({ reasoningEffort: 'low', model: 'qwen3.8-flash' }, served), false);

// Service discovery for auto-start (distribution): pure, so `exists`/`platform` are injected and no
// filesystem is touched. The point of these cases is that a WRONG guess is worse than no guess --
// spawning a missing python would log a lie, so every unresolved shape must return null.
const has = (...paths) => (p) => paths.includes(p);
const REPO = '/repo';
const HERE = '/repo/routing/plugin/dsh-router-laya';

check('service url: strips /judge',
  serviceBaseUrl({ LAYA_ROUTER_URL: 'http://127.0.0.1:9999/judge' }), 'http://127.0.0.1:9999');
check('service url: trailing slash', serviceBaseUrl({ LAYA_ROUTER_URL: 'http://127.0.0.1:9999/judge/' }),
  'http://127.0.0.1:9999');
check('service url: default', serviceBaseUrl({}), 'http://127.0.0.1:8765');

check('autoStart:false disables discovery', serviceLaunchSpec({ autoStart: false }, {}, HERE, has(), 'linux'), null);
check('dev checkout resolves the venv by platform (win32)',
  serviceLaunchSpec({}, {}, HERE,
    has('/repo/training/laya_router_finetuned', '/repo/.venv/Scripts/python.exe', '/repo/routing/laya_router.py'),
    'win32'),
  { python: '/repo/.venv/Scripts/python.exe', script: '/repo/routing/laya_router.py' });
check('dev checkout resolves the venv by platform (posix)',
  serviceLaunchSpec({}, {}, HERE,
    has('/repo/training/laya_router_finetuned', '/repo/.venv/bin/python', '/repo/routing/laya_router.py'),
    'linux'),
  { python: '/repo/.venv/bin/python', script: '/repo/routing/laya_router.py' });
check('no checkpoint marker -> no guess',
  serviceLaunchSpec({}, {}, '/usr/lib/node_modules/dsh-router-laya', has(), 'linux'), null);
check('explicit config wins',
  serviceLaunchSpec({ servicePython: '/py', serviceScript: '/pkg/service/laya_router.py' }, {}, '/nowhere',
    has('/py', '/pkg/service/laya_router.py'), 'linux'),
  { python: '/py', script: '/pkg/service/laya_router.py' });
check('explicit config pointing at nothing -> null, never a spawn',
  serviceLaunchSpec({ servicePython: '/missing', serviceScript: '/missing.py' }, {}, '/nowhere', has(), 'linux'),
  null);
check('venv absent in a checkout -> null rather than bare python',
  serviceLaunchSpec({}, {}, HERE, has('/repo/training/laya_router_finetuned'), 'linux'), null);

// ── report ───────────────────────────────────────────────────────────────────────────────────
console.log('='.repeat(70));
console.log(`  router-laya AUTO checks: ${pass} passed, ${fail.length} failed`);
console.log('='.repeat(70));
for (const line of fail) console.log('  FAIL ' + line);
process.exit(fail.length ? 1 : 0);
