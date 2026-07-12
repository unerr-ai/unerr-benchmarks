#!/usr/bin/env node
// In-container SWE-bench Lite LOCALIZATION A/B (codex-only) — runs on a fly machine.
// For each instance: codex reads the real issue + repo@base_commit and names the
// file(s) to edit. Score vs gold-patch files. baseline (no unerr) vs unerr.
// Emits one JSON row per (instance,arm) to stdout AND /work/results/loc-results.jsonl.
//
// Env (set by entrypoint.sh):
//   OPENAI_API_KEY        codex auth
//   CODEX_HOME_BASE       codex config dir WITHOUT unerr mcp
//   CODEX_HOME_UNERR      codex config dir WITH unerr mcp + hooks
//   SELECT                'requests:6,flask:3,pytest:6' (default) — repo:n picks
//   LIMIT                 cap total instances (default = all selected)
//   ARMS                  'baseline,unerr' (default both)
import { spawn, execSync } from 'node:child_process';
import { readFileSync, appendFileSync, mkdirSync } from 'node:fs';

const APP = '/app';
const WORK = '/work';
const REPOS = `${WORK}/repos`;
const RESULTS = `${WORK}/results/loc-results.jsonl`;
const UNERR = 'unerr';
mkdirSync(`${WORK}/results`, { recursive: true });
mkdirSync(REPOS, { recursive: true });

const PRICING = { 'gpt-5.4-mini': { in: 0.25, cached_in: 0.025, out: 2.00 } };
function billFor(model, u) {
  const p = PRICING[model]; if (!p) return { usd: null, basis: 'no-price' };
  const fresh = Math.max(0, (u.in_tokens || 0) - (u.in_cached || 0));
  return { usd: (fresh * p.in + (u.in_cached || 0) * p.cached_in + (u.out_tokens || 0) * p.out) / 1e6, basis: 'computed:' + model };
}

// All 12 SWE-bench Lite repos (so SELECT can reach the 50-instance mini tier — the
// 3-repo set capped at ~26). Keys in REPO_KEY are the short names used in SELECT.
const REPO_DIR = {
  'psf/requests': 'requests', 'pallets/flask': 'flask', 'pytest-dev/pytest': 'pytest',
  'django/django': 'django', 'sympy/sympy': 'sympy', 'scikit-learn/scikit-learn': 'scikit-learn',
  'matplotlib/matplotlib': 'matplotlib', 'sphinx-doc/sphinx': 'sphinx', 'astropy/astropy': 'astropy',
  'pylint-dev/pylint': 'pylint', 'pydata/xarray': 'xarray', 'mwaskom/seaborn': 'seaborn',
};
const CLONE_URL = {
  'psf/requests': 'https://github.com/psf/requests.git',
  'pallets/flask': 'https://github.com/pallets/flask.git',
  'pytest-dev/pytest': 'https://github.com/pytest-dev/pytest.git',
  'django/django': 'https://github.com/django/django.git',
  'sympy/sympy': 'https://github.com/sympy/sympy.git',
  'scikit-learn/scikit-learn': 'https://github.com/scikit-learn/scikit-learn.git',
  'matplotlib/matplotlib': 'https://github.com/matplotlib/matplotlib.git',
  'sphinx-doc/sphinx': 'https://github.com/sphinx-doc/sphinx.git',
  'astropy/astropy': 'https://github.com/astropy/astropy.git',
  'pylint-dev/pylint': 'https://github.com/pylint-dev/pylint.git',
  'pydata/xarray': 'https://github.com/pydata/xarray.git',
  'mwaskom/seaborn': 'https://github.com/mwaskom/seaborn.git',
};
const REPO_KEY = {
  requests: 'psf/requests', flask: 'pallets/flask', pytest: 'pytest-dev/pytest',
  django: 'django/django', sympy: 'sympy/sympy', 'scikit-learn': 'scikit-learn/scikit-learn',
  matplotlib: 'matplotlib/matplotlib', sphinx: 'sphinx-doc/sphinx', astropy: 'astropy/astropy',
  pylint: 'pylint-dev/pylint', xarray: 'pydata/xarray', seaborn: 'mwaskom/seaborn',
};

const all = readFileSync(`${APP}/swebl.ndjson`, 'utf8').trim().split('\n').map(JSON.parse);
const select = (process.env.SELECT || 'requests:6,flask:3,pytest:6').split(',').map(s => s.split(':'));
let INSTANCES = [];
for (const [k, n] of select) { const repo = REPO_KEY[k]; INSTANCES.push(...all.filter(r => r.repo === repo).slice(0, Number(n))); }
if (process.env.LIMIT) INSTANCES = INSTANCES.slice(0, Number(process.env.LIMIT));
const ARMS = (process.env.ARMS || 'baseline,unerr').split(',').map(a => `codex-${a}`);

function goldFiles(patch) {
  const f = new Set();
  for (const m of patch.matchAll(/^diff --git a\/(\S+) b\/(\S+)/gm)) f.add(m[2]);
  return [...f].filter(x => x.endsWith('.py') && !/(^|\/)(tests?|testing)\//i.test(x) && !/test_|_test\.py$/.test(x.split('/').pop()));
}
const norm = p => p.replace(/^\.\//, '').replace(/^\/+/, '').trim();
function ensureRepo(repo) {
  const dir = `${REPOS}/${REPO_DIR[repo]}`;
  try { execSync(`test -d ${dir}`); } catch { execSync(`git clone --quiet ${CLONE_URL[repo]} ${dir}`, { stdio: 'ignore' }); }
  return dir;
}
function checkout(dir, c) {
  execSync(`git -C ${dir} fetch --quiet --depth 1 origin ${c} 2>/dev/null || true`);
  execSync(`git -C ${dir} checkout -f -q ${c}`); execSync(`git -C ${dir} clean -fdq`);
}
function run(cmd, args, { env = {}, cwd, timeoutMs = 300000 } = {}) {
  return new Promise((resolve) => {
    const t0 = Date.now(); const child = spawn(cmd, args, { cwd, env: { ...process.env, ...env } });
    let out = '', err = ''; const k = setTimeout(() => child.kill('SIGKILL'), timeoutMs);
    child.stdout.on('data', d => out += d); child.stderr.on('data', d => err += d); child.stdin.end();
    child.on('close', code => { clearTimeout(k); resolve({ code, out, err, wall_s: (Date.now() - t0) / 1000 }); });
  });
}
function parseCodex(out, model) {
  let inTok = 0, cached = 0, outTok = 0, result = ''; const tools = {};
  const ACT = new Set(['command_execution', 'mcp_tool_call', 'function_call', 'local_shell_call']);
  for (const line of out.split('\n')) { if (!line.trim()) continue; let ev; try { ev = JSON.parse(line); } catch { continue; }
    if (ev.type === 'turn.completed' && ev.usage) { inTok += ev.usage.input_tokens || 0; cached += ev.usage.cached_input_tokens || 0; outTok += ev.usage.output_tokens || 0; }
    if (ev.type === 'item.completed' && ev.item) { const it = ev.item; if (it.type === 'agent_message') result = it.text || result;
      if (ACT.has(it.type)) { const key = it.type === 'mcp_tool_call' ? `mcp:${it.tool || it.name || '?'}` : it.type; tools[key] = (tools[key] || 0) + 1; } } }
  return { model, in_tokens: inTok, in_cached: cached, out_tokens: outTok, tool_calls: Object.values(tools).reduce((a, b) => a + b, 0), tools, result };
}
function predictedFiles(t) {
  const m = t.match(/FILES:\s*(\[[^\]]*\])/i); if (m) { try { return JSON.parse(m[1]).map(norm); } catch {} }
  return [...t.matchAll(/[\w./-]+\.py/g)].map(x => norm(x[0]));
}
function score(pred, gold) {
  const g = new Set(gold.map(norm)); const p = [...new Set(pred.map(norm))];
  const hit = p.filter(f => g.has(f)); const recall = gold.length ? hit.length / g.size : 0;
  const precision = p.length ? hit.length / p.length : 0;
  const f1 = (recall + precision) ? 2 * recall * precision / (recall + precision) : 0;
  return { recall, precision, f1, found_all: hit.length === g.size && g.size > 0, pred_n: p.length, gold_n: g.size };
}
const PROMPT = (repo, issue) =>
  `You are debugging the \`${repo}\` repository (already checked out in your working directory). ` +
  `Read the GitHub issue below and identify which SOURCE file(s) must be edited to fix it. ` +
  `Do NOT make any edits. Investigate, then end your answer with a single line:\n` +
  `FILES: ["path/one.py","path/two.py"]  (repo-relative, most-likely first)\n\nISSUE:\n${issue}`;

async function runArm(inst, dir, gold, arm) {
  const home = arm === 'codex-unerr' ? process.env.CODEX_HOME_UNERR : process.env.CODEX_HOME_BASE;
  const res = await run('codex', ['exec', '--json', '--dangerously-bypass-approvals-and-sandbox', '-C', dir, PROMPT(inst.repo, inst.ps_text)],
    { env: { CODEX_HOME: home } });
  const m = parseCodex(res.out, 'gpt-5.4-mini');
  const sc = score(predictedFiles(m.result || ''), gold);
  const bill = billFor(m.model, m);
  const row = { ts: new Date().toISOString(), instance: inst.id, repo: inst.repo, arm, model: m.model, code: res.code,
    wall_s: +res.wall_s.toFixed(1), in_tokens: m.in_tokens, in_cached: m.in_cached, out_tokens: m.out_tokens,
    bill_usd: bill.usd, tool_calls: m.tool_calls, tools: m.tools, ...sc, gold, pred: predictedFiles(m.result || '') };
  appendFileSync(RESULTS, JSON.stringify(row) + '\n');
  console.log(JSON.stringify({ ev: 'result', ...row }));
  return row;
}

console.log(JSON.stringify({ ev: 'start', instances: INSTANCES.length, arms: ARMS }));
for (const inst of INSTANCES) {
  const dir = ensureRepo(inst.repo); checkout(dir, inst.base);
  const gold = goldFiles(inst.patch);
  console.log(JSON.stringify({ ev: 'instance', instance: inst.id, repo: inst.repo, gold }));
  if (!gold.length) continue;
  if (ARMS.includes('codex-unerr')) { try { execSync(`${UNERR} index`, { cwd: dir, stdio: 'ignore' }); } catch {} }
  for (const arm of ARMS) await runArm(inst, dir, gold, arm);
}
console.log(JSON.stringify({ ev: 'done', results: RESULTS }));
