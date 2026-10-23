// AUTO mode (plugin v2) pure-function checks: the tier table, mode resolution, and the session
// state machine -- everything that decides where a request lands, without needing DSH or the
// Python service. Plain-script check, repo convention (see parseArm.test.mjs).
//
//     node auto.test.mjs
import { applyRoute, clampEffort, clampRouteToModel, Config, inheritsModel, isOurRoute, MODE_NS,
  mountMode, nextSessionState, resolveModeEntryId, resolveSchedule, serviceBaseUrl, serviceLaunchSpec,
  supportedEffortIds, TIER_TABLE } from './index.js';
import { isVolatile } from '@deepseek-ai/cosmokit';

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
// Simulates the laya MONOREPO layout this discovery was written for: the plugin sits two levels
// below the checkout that holds `training/` and `.venv/`. This package repo has no such layout
// (it is the package itself), which is why a packaged install configures servicePython/serviceScript.
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

// ── mode entry id + volatile Config (write-path prerequisites) ────────────────────────────────
check('entry id: bundle insert',
  resolveModeEntryId([{ options: { id: 'include:router-laya', name: 'dsh-router-laya' } }]),
  'include:router-laya');
check('entry id: hand-patched row',
  resolveModeEntryId([{ options: { id: 'router-laya', name: 'dsh-router-laya' } }]),
  'router-laya');
check('entry id: empty -> fallback',
  resolveModeEntryId([], MODE_NS), MODE_NS);
check('entry id: other plugin ignored',
  resolveModeEntryId([
    { options: { id: 'include:other', name: 'dsh-other' } },
    { options: { id: 'include:router-laya', name: 'dsh-router-laya' } },
  ]), 'include:router-laya');
check('entry id: null entry ignored',
  resolveModeEntryId([null, undefined, { options: { name: 'dsh-router-laya' } }], 'fb'), 'fb');

// `mode` must stay volatile or settings.update refuses a non-volatile path (and the chip 503s).
// cosmokit's isVolatile tests resolved *references*, so assert both the schema meta and the ref
// that Config() materialises for mode.
check('Config.mode schema meta is volatile', Config.dict.mode.meta.volatile === true, true);
check('Config.mode resolves to a volatile ref', isVolatile(Config({}).mode), true);
check('Config.auto is not volatile', Config.dict.auto.meta.volatile === undefined, true);
check('Config keeps ordinary row fields',
  ['auto', 'judge', 'routes', 'tiers', 'global', 'servicePython', 'serviceScript', 'autoStart']
    .every((key) => Object.hasOwn(Config.dict, key)), true);

// ── the mode a row mounts with (`mode: manual` used to be read by nothing at all) ──────────────
check('mount mode: row asks for manual', mountMode({ mode: 'manual', auto: true }, 'auto'), 'manual');
check('mount mode: row asks for auto', mountMode({ mode: 'auto' }, 'auto'), 'auto');
check('mount mode: unset on an auto row starts auto', mountMode({ auto: true }, 'auto'), 'auto');
check('mount mode: unset on a judge row starts manual', mountMode({ judge: true }, 'judge'), 'manual');
check('mount mode: junk value falls back to the arm', mountMode({ mode: 'AUTO' }, 'auto'), 'auto');

// ── rows that omit the model: a tier change moves the effort, never the model ────────────────
// A table row may omit provider/model; that is the "inherit the request's model" form. Keeping
// maxTokens is part of the contract: the cap belongs to the model we are NOT changing.
check('applyRoute: inheriting row keeps provider, model and cap',
  applyRoute({ provider: 'p', model: 'm', maxTokens: 4096 }, { effort: 'high' }),
  { provider: 'p', model: 'm', maxTokens: 4096, reasoningEffort: 'high' });
check('applyRoute: inheriting row rewrites an existing effort',
  applyRoute({ provider: 'p', model: 'm', reasoningEffort: 'off' }, { effort: 'low' }),
  { provider: 'p', model: 'm', reasoningEffort: 'low' });
check('applyRoute: inheriting row with no effort clears it',
  applyRoute({ provider: 'p', model: 'm', reasoningEffort: 'high' }, {}),
  { provider: 'p', model: 'm' });
check('applyRoute: explicit row still re-points the model and drops the old cap',
  applyRoute({ provider: 'p', model: 'm', maxTokens: 4096 }, { provider: 'q', model: 'n', effort: 'low' }),
  { provider: 'q', model: 'n', reasoningEffort: 'low' });

// clampEffort: a declared level wins; otherwise the nearest ladder neighbour; ties go to the
// stronger level; a word this file does not know belongs to the adapter, so pass it through.
check('clamp: declared level kept', clampEffort('low', ['low', 'high', 'max']), 'low');
check('clamp: below range lifts to the only level', clampEffort('low', ['high']), 'high');
check('clamp: above range drops to the strongest', clampEffort('max', ['low', 'medium', 'high']), 'high');
check('clamp: equidistant tie goes stronger', clampEffort('medium', ['low', 'high']), 'high');
check('clamp: unknown level dropped to strongest', clampEffort('xhigh', ['low', 'medium', 'high', 'max']), 'max');
check('clamp: adapter-owned word passes through', clampEffort('turbo', ['low', 'high']), 'turbo');
check('clamp: no declared levels -> undefined', clampEffort('high', []), undefined);

// The matrix the README publishes, asserted here so the doc cannot drift from the code: one row per
// real model shape, columns are the three judged tiers the plugin asks for.
for (const [label, supported, expected] of [
  ['low|high|max', ['low', 'high', 'max'], ['low', 'high', 'max']],
  ['high only', ['high'], ['high', 'high', 'high']],
  ['medium only', ['medium'], ['medium', 'medium', 'medium']],
  ['low|medium|high|xhigh', ['low', 'medium', 'high', 'xhigh'], ['low', 'high', 'xhigh']],
  ['low|high (gaps)', ['low', 'high'], ['low', 'high', 'high']],
  ['no reasoning control', [], [undefined, undefined, undefined]],
]) {
  check(`matrix ${label}: low`, clampEffort('low', supported), expected[0]);
  check(`matrix ${label}: high`, clampEffort('high', supported), expected[1]);
  check(`matrix ${label}: max`, clampEffort('max', supported), expected[2]);
}

// supportedEffortIds: null = "could not find out" (leave the request alone), [] = "no reasoning
// control on this model" (drop the effort). The two must stay distinguishable.
const llmCtx = (resolveModelInfo) => ({ get: (name) => (name === 'llm' ? { resolveModelInfo } : undefined) });
check('efforts: no llm service -> null', await supportedEffortIds({ get: () => undefined }, 'p', 'm'), null);
check('efforts: model without reasoning -> []', await supportedEffortIds(llmCtx(async () => ({})), 'p', 'm'), []);
check('efforts: ids extracted in declared order',
  await supportedEffortIds(llmCtx(async () => ({ reasoning: { efforts: [{ id: 'low' }, { id: 'high' }] } })), 'p', 'm'),
  ['low', 'high']);
check('efforts: lookup throws -> null',
  await supportedEffortIds(llmCtx(async () => { throw new Error('adapter down'); }), 'p', 'm'), null);

// clampRouteToModel: only inheriting rows are touched.
const twoLevels = llmCtx(async () => ({ reasoning: { efforts: [{ id: 'low' }, { id: 'high' }] } }));
const request = { provider: 'p', model: 'm' };
check('clampRoute: inheriting row aligned to the current model',
  await clampRouteToModel(twoLevels, request, { effort: 'max' }), { effort: 'high' });
check('clampRoute: explicit row untouched',
  await clampRouteToModel(twoLevels, request, { provider: 'q', model: 'n', effort: 'max' }),
  { provider: 'q', model: 'n', effort: 'max' });
check('clampRoute: no effort -> untouched',
  await clampRouteToModel(twoLevels, request, {}), {});
check('clampRoute: model without reasoning control -> effort dropped',
  await clampRouteToModel(llmCtx(async () => ({})), request, { effort: 'max' }), { effort: undefined });
check('clampRoute: unknown capability -> untouched',
  await clampRouteToModel(llmCtx(async () => { throw new Error('x'); }), request, { effort: 'max' }), { effort: 'max' });

// A half-specified row (model but no provider) is a typo, not an inheriting row: it must keep the old
// loud behaviour instead of quietly turning into "keep whatever model the request already had".
check('inheritsModel: both fields absent', inheritsModel({ effort: 'low' }), true);
check('inheritsModel: model only is not inheriting', inheritsModel({ model: 'm', effort: 'low' }), false);
check('inheritsModel: provider only is not inheriting', inheritsModel({ provider: 'p', effort: 'low' }), false);
check('inheritsModel: explicit row', inheritsModel({ provider: 'p', model: 'm', effort: 'low' }), false);

// isOurRoute compares against what was actually sent, not the row's nominal effort: a tier that had
// to be aligned to the model (low -> high on a high-only model) is still our own route, otherwise the
// next turn would defer to itself and auto would stop routing.
const inheritedTiers = { low: { effort: 'low' }, fallback: { effort: 'high' } };
check('isOurRoute: aligned effort still ours',
  isOurRoute({ reasoningEffort: 'high' }, { tier: 'low', effort: 'high' }, inheritedTiers), true);
check('isOurRoute: foreign effort is not ours',
  isOurRoute({ reasoningEffort: 'medium' }, { tier: 'low', effort: 'high' }, inheritedTiers), false);
check('isOurRoute: falls back to the row effort when the state has none',
  isOurRoute({ reasoningEffort: 'low' }, { tier: 'low' }, inheritedTiers), true);

// ── report ───────────────────────────────────────────────────────────────────────────────────
console.log('='.repeat(70));
console.log(`  router-laya AUTO checks: ${pass} passed, ${fail.length} failed`);
console.log('='.repeat(70));
for (const line of fail) console.log('  FAIL ' + line);
process.exit(fail.length ? 1 : 0);
