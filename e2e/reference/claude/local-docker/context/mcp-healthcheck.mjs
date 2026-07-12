#!/usr/bin/env node
/**
 * Prove the unerr MCP path works end-to-end against a REAL `unerr --mcp` session:
 *   initialize  ->  tools/list  ->  tools/call file_read
 *
 * Verdicts (exit 0 = all critical checks pass, 1 = any fail):
 *   - tools/list returns the unerr tools         -> MCP up, entitlement honored
 *   - error -32003                               -> repo-cap refusal (daemon never
 *                                                    got UNERR_ENTITLEMENT_* env)
 *   - error -32004                               -> login refusal
 *   - tools/call file_read returns content       -> tools actually EXECUTE
 *
 * Usage: node mcp-healthcheck.mjs <repo-dir> [unerr-bin] [repo-relative-file] [wait-ms]
 */
import { spawn } from "node:child_process";

const repo = process.argv[2];
const bin = process.argv[3] || "unerr";
const probeFile = process.argv[4] || "";
// First file_read triggers a full repo index; on a large repo under x86_64
// emulation (Apple Silicon) that can exceed 45s. Default generously so the
// preflight doesn't false-FAIL on indexing latency (the real run has 1800s).
const WAIT_MS = Number(process.argv[5] || 180000);

if (!repo) {
  process.stderr.write("usage: mcp-healthcheck.mjs <repo-dir> [bin] [file] [wait-ms]\n");
  process.exit(2);
}

const EXPECTED = [
  "search_code", "file_read", "get_references", "unerr_context",
  "file_outline", "file_edit", "fetch_url", "unerr_track",
];

const child = spawn(bin, ["--mcp"], { cwd: repo, stdio: ["pipe", "pipe", "pipe"], env: process.env });
const send = (o) => child.stdin.write(`${JSON.stringify(o)}\n`);

let initialized = false;
let toolsSeen = null;
let refusal = null;
let callResult = null; // true | false | null(not attempted)
let calledOnce = false;
let callRaw = null;    // raw id:3 payload, for diagnosing a file_read failure
let buf = "";

child.stdout.on("data", (d) => {
  buf += d.toString();
  let i;
  while ((i = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, i);
    buf = buf.slice(i + 1);
    handle(line);
  }
});
child.stderr.on("data", () => {});

function handle(line) {
  if (!line.trim().startsWith("{")) return;
  let m;
  try { m = JSON.parse(line); } catch { return; }
  if (m.id === 1 && m.result) initialized = true;
  if (m.id === 2 && m.result?.tools) {
    toolsSeen = m.result.tools.map((t) => t.name);
    if (probeFile && !calledOnce) {
      calledOnce = true;
      send({ jsonrpc: "2.0", id: 3, method: "tools/call",
        params: { name: "file_read", arguments: { file_path: probeFile, token_budget: 400 } } });
    } else {
      finish();
    }
  }
  if (m.error) { refusal = m.error; if (!toolsSeen) finish(); }
  if (m.id === 3) {
    const r = m.result;
    callRaw = m;
    // an MCP tool result is content[]; treat any non-error payload as "executed"
    callResult = !m.error && !!r && !(r.isError === true);
    finish();
  }
}

send({ jsonrpc: "2.0", id: 1, method: "initialize",
  params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "mcp-healthcheck", version: "0" } } });
setTimeout(() => {
  send({ jsonrpc: "2.0", method: "notifications/initialized", params: {} });
  send({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
}, 1500);

let done = false;
function finish() {
  if (done) return;
  done = true;

  const missing = toolsSeen ? EXPECTED.filter((n) => !toolsSeen.includes(n)) : EXPECTED;
  const checks = [
    ["initialize handshake", initialized],
    ["tools/list returned unerr tools", !!toolsSeen && toolsSeen.length > 0],
    ["no repo-cap refusal (-32003) — login-skip worked", refusal?.code !== -32003],
    ["no login refusal (-32004)", refusal?.code !== -32004],
    ["core tools advertised (file_read, search_code)",
      !!toolsSeen && toolsSeen.includes("file_read") && toolsSeen.includes("search_code")],
  ];
  if (probeFile) checks.push(["tools/call file_read executed", callResult === true]);

  const out = ["=== mcp-healthcheck ===", `repo: ${repo}`];
  if (toolsSeen) out.push(`tools (${toolsSeen.length}): ${toolsSeen.join(", ")}`);
  if (missing.length && toolsSeen) out.push(`missing expected: ${missing.join(", ") || "none"}`);
  if (refusal) out.push(`refusal: ${refusal.code} ${refusal.message}`);
  if (probeFile && callResult !== true) {
    out.push(
      callRaw
        ? `file_read raw id:3: ${JSON.stringify(callRaw).slice(0, 800)}`
        : "file_read: NO id:3 response before timeout (tool call never returned)"
    );
  }
  let failed = 0;
  for (const [name, ok] of checks) {
    out.push(`  ${ok ? "[PASS]" : "[FAIL]"} ${name}`);
    if (!ok) failed++;
  }
  out.push(`verdict: ${failed === 0 ? "ALL PASS" : `${failed} FAILED`}`);
  process.stdout.write(`${out.join("\n")}\n`);

  try { child.stdin.end(); } catch {}
  child.kill("SIGTERM");
  process.exit(failed === 0 ? 0 : 1);
}

setTimeout(finish, WAIT_MS);
child.on("exit", () => finish());
