// Offline wire-contract check for AUTO mode: boots against a RUNNING local /judge service and
// asserts the tier/triggered_by contract for the plan §5 message classes, including the
// prev_tier/prev_task-carrying requests v2 adds (deepseek review F9).
//
//     node routing/plugin/dsh-router-laya/contract.test.mjs   (service must be up; see
//                                                              routing/start_router.ps1)
const URL = process.env.LAYA_ROUTER_URL || 'http://127.0.0.1:8765/judge';

let pass = 0;
const fail = [];

async function post(body) {
  const res = await fetch(URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (ok) pass++;
  else fail.push(`${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

const cases = [
  { name: 'chitchat -> low/laya',
    body: { task: "hey, how's it going?" },
    want: { tier: 'low', triggered_by: 'laya' } },
  { name: 'batch migration -> high via rule 0',
    body: { task: '帮我把这 40 个测试文件的临时目录写法全部迁移到 tmp_path，一个都不能漏' },
    want: { tier: 'high', triggered_by: 'laya' } },
  { name: 'force intent -> max/intent_force (one-vote veto)',
    body: { task: '用最高档重新做这个' },
    want: { tier: 'max', triggered_by: 'intent_force' } },
  { name: 'inherit keeps prev_tier',
    body: { task: '继续', prev_tier: 'high' },
    want: { tier: 'high', triggered_by: 'intent_inherit' } },
  { name: 'regenerate detected but base already >= escalate target (no tier move)',
    body: { task: 'fix the login bug', prev_tier: 'low', prev_task: 'fix the login bug' },
    want: { tier: 'high', triggered_by: 'laya', regenerate: true } },
  { name: 'regenerate actually escalates: base low -> high',
    body: { task: '帮我优化一下', prev_tier: 'low', prev_task: '帮我优化一下' },
    want: { tier: 'high', triggered_by: 'escalate_regenerate', regenerate: true } },
  { name: 'exclusion caps the escalated tier (F1/F5): 别用 max + regen from high',
    body: { task: '别用 max，重新部署这套配置', prev_tier: 'high', prev_task: '部署这套配置' },
    want: { tier: 'high', triggered_by: 'intent_exclude', regenerate: false } },
];

for (const c of cases) {
  try {
    const j = await post(c.body);
    if (j.error) { fail.push(`${c.name}: service error ${j.error}`); continue; }
    check(`${c.name} [tier]`, j.tier, c.want.tier);
    check(`${c.name} [triggered_by]`, j.triggered_by, c.want.triggered_by);
    if (c.want.regenerate !== undefined) check(`${c.name} [regenerate]`, j.regenerate, c.want.regenerate);
  } catch (e) {
    fail.push(`${c.name}: ${e.message} (is the router service up? routing/start_router.ps1)`);
  }
}

console.log('='.repeat(70));
console.log(`  /judge wire contract: ${pass} passed, ${fail.length} failed`);
console.log('='.repeat(70));
for (const line of fail) console.log('  FAIL ' + line);
process.exit(fail.length ? 1 : 0);
