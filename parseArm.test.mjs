// Arm parsing is the one piece of real logic in the router plugin, and a typo in an arm name would
// silently mislabel a whole run -- so it gets the repo's usual plain-script check, no framework.
//
//     node parseArm.test.mjs
import { applyRoute, carriesExplicitRoute, parseArm, parseSchedule, resolveSchedule, routeForLevel, ROUTE_TABLE, usableRoute } from './index.js';

let pass = 0;
const fail = [];

function check(spec, want) {
  let got;
  try {
    got = parseArm(spec);
  } catch (e) {
    got = 'threw: ' + e.message;
  }
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (ok) pass++;
  else fail.push(`${JSON.stringify(spec)} -> ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

/** Same contract for the other pure functions, named in the failure line so a miss is locatable.
 *
 * `resolveSchedule` is the env-var layer, not a string parser, so it is exercised through
 * `ROUTEEXP_ARM` itself and the variable is restored afterwards. */
function check2(name, input, want) {
  const saved = process.env.ROUTEEXP_ARM;
  if (name === 'resolveSchedule') {
    if (input === undefined) delete process.env.ROUTEEXP_ARM;
    else process.env.ROUTEEXP_ARM = input;
  }
  let got;
  try {
    got = name === 'applyRoute' ? applyRoute(input[0], input[1]) : FUNCS[name](input);
  } finally {
    if (saved === undefined) delete process.env.ROUTEEXP_ARM;
    else process.env.ROUTEEXP_ARM = saved;
  }
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (ok) pass++;
  else fail.push(`${name}(${JSON.stringify(input)}) -> ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

/** Same contract, for a function of several arguments. */
function checkArgs(name, args, want) {
  let got;
  try {
    got = FUNCS[name](...args);
  } catch (e) {
    got = 'threw: ' + e.message;
  }
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (ok) pass++;
  else fail.push(`${name}(${JSON.stringify(args)}) -> ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

const FUNCS = {
  parseSchedule,
  resolveSchedule,
  routeForLevel,
  usableRoute,
  carriesExplicitRoute,
};

// unset means "do not touch the route"
check(undefined, null);
check(null, null);
check('', null);
check('   ', null);

// provider defaults to the DeepSeek route when omitted; effort is optional
check('deepseek-v4-pro', { provider: 'deepseek-official', model: 'deepseek-v4-pro' });
check('deepseek-v4-pro:max', { provider: 'deepseek-official', model: 'deepseek-v4-pro', effort: 'max' });
check('deepseek-v4-flash:off', { provider: 'deepseek-official', model: 'deepseek-v4-flash', effort: 'off' });
check('  deepseek-flash:low  ', { provider: 'deepseek-official', model: 'deepseek-flash', effort: 'low' });

// an explicit provider wins, and hyphens/dots in ids survive
check('zai/glm-5.3-flash:high', { provider: 'zai', model: 'glm-5.3-flash', effort: 'high' });
check('xiaomi-token-plan-cn/mimo-v2.5-pro', { provider: 'xiaomi-token-plan-cn', model: 'mimo-v2.5-pro' });

// Effort levels are adapter-owned, so this parser must NOT gate them: deepseek accepts max, qwen
// accepts xhigh, and a hardcoded list here rejected valid levels on every model it was not written
// for. A bad level fails at the adapter instead.
check('deepseek-flash:max', { provider: 'deepseek-official', model: 'deepseek-flash', effort: 'max' });
check('qwen-token-plan-cn/qwen3.8-flash:xhigh',
  { provider: 'qwen-token-plan-cn', model: 'qwen3.8-flash', effort: 'xhigh' });
check('qwen-token-plan-cn/qwen3.8-flash:medium',
  { provider: 'qwen-token-plan-cn', model: 'qwen3.8-flash', effort: 'medium' });

// structurally malformed input must still throw, not fall through to a default arm
for (const bad of ['deepseek-v4-pro:', '/deepseek-v4-pro', 'zai/', ':max']) {
  let threw = false;
  try {
    parseArm(bad);
  } catch (e) {
    threw = true;
  }
  if (threw) pass++;
  else fail.push(`${JSON.stringify(bad)} was accepted, want a throw`);
}

// A schedule is a cycle, and `judge` is not an arm -- it is the other layer.
check2('parseSchedule', 'deepseek-flash:low,deepseek-flash:max', [
  { provider: 'deepseek-official', model: 'deepseek-flash', effort: 'low' },
  { provider: 'deepseek-official', model: 'deepseek-flash', effort: 'max' },
]);
check2('parseSchedule', undefined, null);
check2('parseSchedule', '  ', null);
check2('resolveSchedule', 'judge', 'judge');
check2('resolveSchedule', '  JUDGE ', 'judge');
check2('resolveSchedule', 'deepseek-flash:low', [
  { provider: 'deepseek-official', model: 'deepseek-flash', effort: 'low' },
]);

// The judge answers a *level*; the table maps it. An unknown level must take the fallback column
// rather than guessing a route -- that is the whole failure contract of the judge.
check2('routeForLevel', 'easy', ROUTE_TABLE.easy);
check2('routeForLevel', 'Hard', ROUTE_TABLE.hard);
check2('routeForLevel', ' medium ', ROUTE_TABLE.medium);
for (const bad of [null, undefined, '', 'moderate', 'HARD!']) {
  check2('routeForLevel', bad, ROUTE_TABLE.fallback);
}

// Rewriting a route drops an inherited adapter default and replaces effort wholesale.
check2('applyRoute', [{ provider: 'a', model: 'm', maxTokens: 999, reasoningEffort: 'max' }, ROUTE_TABLE.easy],
  { provider: 'deepseek-official', model: 'deepseek-flash', reasoningEffort: 'low' });
check2('applyRoute', [{ provider: 'a', model: 'm' }, { provider: 'b', model: 'n' }],
  { provider: 'b', model: 'n' });

// Deferral: a config that already carries an effort is a route someone chose.
check2('carriesExplicitRoute', { provider: 'a', model: 'b', reasoningEffort: 'off' }, true);
check2('carriesExplicitRoute', { provider: 'a', model: 'b' }, false);
check2('carriesExplicitRoute', undefined, false);

// A row config re-points the level -> route policy; the built-in table is the default, which is what
// keeps the experiment bit-for-bit unchanged while a preset states its own table.
checkArgs('routeForLevel', ['hard', { hard: { provider: 'qwen-token-plan-cn', model: 'qwen3.8-flash', effort: 'xhigh' } }],
  { provider: 'qwen-token-plan-cn', model: 'qwen3.8-flash', effort: 'xhigh' });
checkArgs('routeForLevel', ['hard'], ROUTE_TABLE.hard);
// A table with no fallback column yields no route at all rather than guessing one.
checkArgs('routeForLevel', ['hard', { easy: ROUTE_TABLE.easy }], undefined);

// The provider guard, and the reason it exists: `hard` names a provider this deployment never
// registered, and applying it would replace the user's model with an unroutable one and fail the
// step. Unusable routes resolve to null, which leaves the request on the config its owner chose.
// The refusal is logged to stderr, so one line on this test's output is expected.
const LLM_OK = { get: (name) => (name === 'llm' ? { listProviders: () => [{ id: 'deepseek-official' }] } : undefined) };
const LLM_GONE = { get: () => undefined };
checkArgs('usableRoute', [LLM_OK, ROUTE_TABLE.easy], ROUTE_TABLE.easy);
checkArgs('usableRoute', [LLM_OK, ROUTE_TABLE.hard], null);
checkArgs('usableRoute', [LLM_OK, undefined], null);
checkArgs('usableRoute', [LLM_OK, null], null);
// No registry to ask is not a reason to stop routing: the table is trusted as written.
checkArgs('usableRoute', [LLM_GONE, ROUTE_TABLE.hard], ROUTE_TABLE.hard);

if (fail.length > 0) {
  console.log(`\n${fail.length} failed:`);
  for (const f of fail) console.log('   ' + f);
}
console.log(`${pass} passed, ${fail.length} failed`);
process.exit(fail.length === 0 ? 0 : 1);
