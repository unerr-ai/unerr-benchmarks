#!/usr/bin/env node
/**
 * dev-entitlement — local-only entitlement minter for testing paid tiers.
 *
 * Lets you exercise every plan (free / pro / team / enterprise) and every
 * feature gate on your own machine, with no server, no Stripe, and no real
 * subscription. It mints the exact same Ed25519 compact-JWS token the server
 * issues (unerr-web-service `lib/cli/entitlement-token.ts`) and writes it to
 * the CLI's entitlement cache, so `effectiveTier()` / `gate()` behave online
 * and offline precisely as they would for a paying org.
 *
 * Trust model — UNCHANGED, on purpose. The CLI only trusts a non-shipped key
 * through the env override (`UNERR_ENTITLEMENT_KID` + `UNERR_ENTITLEMENT_PUBKEY`,
 * see src/cloud/entitlement-keys.ts). This script does NOT add a pinned key and
 * does NOT introduce a file-based trust path — forging a plan by dropping a file
 * stays impossible. You opt in deliberately by exporting the two env vars this
 * script prints (or wiring them into `.mcp.json` with `--wire-mcp`).
 *
 *   node scripts/dev-entitlement.mjs mint pro          # mint + cache a Pro token
 *   node scripts/dev-entitlement.mjs mint team --wire-mcp
 *   node scripts/dev-entitlement.mjs status            # what the cache says now
 *   node scripts/dev-entitlement.mjs clear             # back to free (delete cache)
 *   node scripts/dev-entitlement.mjs derive-pubkey --from-env ../unerr-web-service/.env.local
 *
 * Mint flags:
 *   --features a,b,c   add feature flags (set true) on top of the plan's set
 *   --grace-days N     offline grace window (default 7, mirrors ENTITLEMENT_GRACE_DAYS)
 *   --fresh-hours N    freshness window (default 24, mirrors ENTITLEMENT_FRESH_HOURS)
 *   --age-hours N      backdate `iat` by N hours — test grace (N>fresh) / expiry (N>fresh+grace*24)
 *   --org ID           org_id claim (default dev-org)
 *   --machine ID       machine_id claim (default dev-machine)
 *   --wire-mcp [path]  also write the override env into a project .mcp.json (default ./.mcp.json)
 *
 * The pure builders below are exported so src/__tests__/dev-entitlement.test.ts
 * can prove a minted token passes the PRODUCTION verifier and unlocks the tier.
 */

import {
  createPrivateKey,
  createPublicKey,
  generateKeyPairSync,
  sign,
  verify,
} from "node:crypto";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// ── Grounded constants (single source of truth lives in the server) ──────────
// Mirror of unerr-web-service lib/billing/plans.ts `PLANS` (limits + features)
// and lib/constants.ts (ENTITLEMENT_FRESH_HOURS / ENTITLEMENT_GRACE_DAYS).
// The server stays authoritative; this table only has to match it for dev.
export const PLANS = {
  free: {
    maxMembers: 1,
    maxMachines: 2,
    maxActiveRepos: 1,
    features: { conventions_sync: true },
  },
  pro: {
    maxMembers: 1,
    maxMachines: 10,
    maxActiveRepos: -1,
    features: { conventions_sync: true },
  },
  team: {
    maxMembers: -1,
    maxMachines: -1,
    maxActiveRepos: -1,
    features: { conventions_sync: true },
  },
  enterprise: {
    maxMembers: 500,
    maxMachines: 1000,
    maxActiveRepos: -1,
    features: { conventions_sync: true },
  },
};

export const DEFAULT_FRESH_HOURS = 24; // ENTITLEMENT_FRESH_HOURS
export const DEFAULT_GRACE_DAYS = 7; // ENTITLEMENT_GRACE_DAYS

/** kid for the local dev key. Distinct from any pinned/production kid. */
export const DEV_KID = "k-dev-local";

const UNERR_DIR = join(homedir(), ".unerr");
const DEV_DIR = join(UNERR_DIR, "dev");
const KEY_PATH = join(DEV_DIR, "entitlement-key.json");
const CACHE_PATH = join(UNERR_DIR, "entitlements.json");
const FILE_MODE = 0o600;

// ── Pure builders (no IO — exported for tests) ───────────────────────────────

const b64url = (data) => Buffer.from(data).toString("base64url");

/** Fresh Ed25519 key material as base64 DER (PKCS#8 private, SPKI public). */
export function createDevKeyMaterial(kid = DEV_KID) {
  const { privateKey, publicKey } = generateKeyPairSync("ed25519");
  return {
    kid,
    privateKey: privateKey
      .export({ format: "der", type: "pkcs8" })
      .toString("base64"),
    publicKey: publicKey
      .export({ format: "der", type: "spki" })
      .toString("base64"),
    createdAt: new Date().toISOString(),
  };
}

/** Derive the base64 SPKI public key from a base64 PKCS#8 Ed25519 private key. */
export function publicKeyFromPrivate(privateKeyBase64) {
  const priv = createPrivateKey({
    key: Buffer.from(privateKeyBase64, "base64"),
    format: "der",
    type: "pkcs8",
  });
  return createPublicKey(priv)
    .export({ format: "der", type: "spki" })
    .toString("base64");
}

/**
 * Build entitlement claims identical to the server's `buildEntitlementClaims`.
 * `ageHours` backdates `iat` so you can land inside the fresh window, the grace
 * window, or past grace without waiting real time.
 */
export function buildClaims({
  plan,
  orgId = "dev-org",
  machineId = "dev-machine",
  freshHours = DEFAULT_FRESH_HOURS,
  graceDays = DEFAULT_GRACE_DAYS,
  ageHours = 0,
  extraFeatures = {},
  now = Date.now(),
}) {
  const def = PLANS[plan];
  if (!def) throw new Error(`unknown plan: ${plan} (free|pro|team|enterprise)`);
  const iat = Math.floor(now / 1000) - Math.round(ageHours * 3_600);
  const graceUntil = iat + graceDays * 86_400;
  return {
    iss: "unerr",
    org_id: orgId,
    machine_id: machineId,
    plan,
    limits: {
      max_members: def.maxMembers,
      max_machines: def.maxMachines,
      max_active_repos: def.maxActiveRepos,
    },
    features: { ...def.features, ...extraFeatures },
    iat,
    fresh_until: iat + freshHours * 3_600,
    grace_until: graceUntil,
    exp: graceUntil,
  };
}

/** Compact JWS, header `{ alg:"EdDSA", typ:"JWT", kid }` — mirrors the server. */
export function signToken(claims, privateKeyBase64, kid) {
  const key = createPrivateKey({
    key: Buffer.from(privateKeyBase64, "base64"),
    format: "der",
    type: "pkcs8",
  });
  const header = b64url(JSON.stringify({ alg: "EdDSA", typ: "JWT", kid }));
  const payload = b64url(JSON.stringify(claims));
  const signature = sign(null, Buffer.from(`${header}.${payload}`), key);
  return `${header}.${payload}.${signature.toString("base64url")}`;
}

/** Self-check: verify a token against a base64 SPKI public key (mirrors the CLI). */
export function verifyToken(token, publicKeyBase64) {
  const parts = token.split(".");
  if (parts.length !== 3) return false;
  const [header, payload, signature] = parts;
  try {
    const key = createPublicKey({
      key: Buffer.from(publicKeyBase64, "base64"),
      format: "der",
      type: "spki",
    });
    return verify(
      null,
      Buffer.from(`${header}.${payload}`),
      key,
      Buffer.from(signature, "base64url")
    );
  } catch {
    return false;
  }
}

/** The exact record shape `writeEntitlementCache` persists (src/cloud/entitlements.ts). */
export function cacheRecord(token, claims, now = Date.now()) {
  return { token, claims, fetched_at: now, max_server_time: 0 };
}

// ── IO helpers (used by the CLI, not by tests) ───────────────────────────────

function loadOrCreateDevKey() {
  if (existsSync(KEY_PATH)) {
    return JSON.parse(readFileSync(KEY_PATH, "utf-8"));
  }
  const material = createDevKeyMaterial();
  mkdirSync(DEV_DIR, { recursive: true, mode: 0o700 });
  writeFileSync(KEY_PATH, `${JSON.stringify(material, null, 2)}\n`, {
    mode: FILE_MODE,
  });
  chmodSync(KEY_PATH, FILE_MODE);
  return material;
}

function writeCache(record) {
  mkdirSync(UNERR_DIR, { recursive: true, mode: 0o700 });
  writeFileSync(CACHE_PATH, `${JSON.stringify(record, null, 2)}\n`, {
    mode: FILE_MODE,
  });
  chmodSync(CACHE_PATH, FILE_MODE);
}

function parseDotenv(path) {
  const out = {};
  for (const line of readFileSync(path, "utf-8").split("\n")) {
    const m = /^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/.exec(line);
    if (!m) continue;
    out[m[1]] = m[2].replace(/^["']|["']$/g, "");
  }
  return out;
}

/** Add the override env to the unerr server entry in a project .mcp.json. */
function wireMcp(mcpPath, kid, pubkey) {
  if (!existsSync(mcpPath)) throw new Error(`no .mcp.json at ${mcpPath}`);
  const cfg = JSON.parse(readFileSync(mcpPath, "utf-8"));
  const servers = cfg.mcpServers ?? {};
  const entries = Object.entries(servers).filter(
    ([, s]) => Array.isArray(s?.args) && s.args.includes("--mcp")
  );
  if (entries.length === 0)
    throw new Error(`no unerr (--mcp) server entry in ${mcpPath}`);
  for (const [, s] of entries) {
    s.env = {
      ...(s.env ?? {}),
      UNERR_ENTITLEMENT_KID: kid,
      UNERR_ENTITLEMENT_PUBKEY: pubkey,
    };
  }
  writeFileSync(mcpPath, `${JSON.stringify(cfg, null, 2)}\n`);
  return entries.map(([name]) => name);
}

function unwireMcp(mcpPath) {
  if (!existsSync(mcpPath)) return [];
  const cfg = JSON.parse(readFileSync(mcpPath, "utf-8"));
  const cleared = [];
  for (const [name, s] of Object.entries(cfg.mcpServers ?? {})) {
    if (
      s?.env &&
      ("UNERR_ENTITLEMENT_KID" in s.env || "UNERR_ENTITLEMENT_PUBKEY" in s.env)
    ) {
      const kept = Object.fromEntries(
        Object.entries(s.env).filter(
          ([k]) =>
            k !== "UNERR_ENTITLEMENT_KID" && k !== "UNERR_ENTITLEMENT_PUBKEY"
        )
      );
      if (Object.keys(kept).length === 0) s.env = undefined;
      else s.env = kept;
      cleared.push(name);
    }
  }
  if (cleared.length)
    writeFileSync(mcpPath, `${JSON.stringify(cfg, null, 2)}\n`);
  return cleared;
}

// ── CLI ──────────────────────────────────────────────────────────────────────

function flag(args, name) {
  const i = args.indexOf(name);
  if (i === -1) return undefined;
  const next = args[i + 1];
  return next && !next.startsWith("--") ? next : true;
}

function printEnv(kid, pubkey) {
  process.stderr.write(
    "\n  Trust this dev key by exporting (one machine, one time):\n\n"
  );
  process.stdout.write(`export UNERR_ENTITLEMENT_KID=${kid}\n`);
  process.stdout.write(`export UNERR_ENTITLEMENT_PUBKEY=${pubkey}\n`);
}

function cmdMint(args) {
  const plan = args[0];
  if (!plan || !PLANS[plan]) {
    process.stderr.write(
      "usage: dev-entitlement mint <free|pro|team|enterprise> [flags]\n"
    );
    process.exit(1);
  }
  const key = loadOrCreateDevKey();
  const extraFeatures = {};
  const feats = flag(args, "--features");
  if (typeof feats === "string")
    for (const f of feats.split(",")) extraFeatures[f.trim()] = true;

  const claims = buildClaims({
    plan,
    orgId:
      typeof flag(args, "--org") === "string" ? flag(args, "--org") : undefined,
    machineId:
      typeof flag(args, "--machine") === "string"
        ? flag(args, "--machine")
        : undefined,
    freshHours: flag(args, "--fresh-hours")
      ? Number(flag(args, "--fresh-hours"))
      : undefined,
    graceDays: flag(args, "--grace-days")
      ? Number(flag(args, "--grace-days"))
      : undefined,
    ageHours: flag(args, "--age-hours") ? Number(flag(args, "--age-hours")) : 0,
    extraFeatures,
  });
  const token = signToken(claims, key.privateKey, key.kid);
  if (!verifyToken(token, key.publicKey)) {
    process.stderr.write("✗ self-verify failed — not writing cache\n");
    process.exit(1);
  }
  writeCache(cacheRecord(token, claims));

  process.stderr.write(`✓ minted ${plan} token → ${CACHE_PATH}\n`);
  process.stderr.write(
    `  plan=${plan} features=${Object.keys(claims.features).join(",")} ` +
      `fresh_until=${new Date(claims.fresh_until * 1000).toISOString()} ` +
      `grace_until=${new Date(claims.grace_until * 1000).toISOString()}\n`
  );

  const wire = flag(args, "--wire-mcp");
  if (wire) {
    const mcpPath = resolve(typeof wire === "string" ? wire : ".mcp.json");
    const names = wireMcp(mcpPath, key.kid, key.publicKey);
    process.stderr.write(
      `✓ wired override into ${mcpPath} (${names.join(", ")})\n`
    );
    process.stderr.write(
      "  → restart unerr for the daemon to pick it up (you own restart).\n"
    );
  } else {
    printEnv(key.kid, key.publicKey);
    process.stderr.write(
      "\n  Or wire it into this repo's .mcp.json with --wire-mcp, then restart unerr.\n"
    );
  }
}

function cmdStatus() {
  if (!existsSync(CACHE_PATH)) {
    process.stderr.write("no entitlement cache — effective tier is free.\n");
    return;
  }
  const rec = JSON.parse(readFileSync(CACHE_PATH, "utf-8"));
  const c = rec.claims ?? {};
  const nowSec = Math.floor(Date.now() / 1000);
  const window =
    nowSec <= (c.fresh_until ?? 0)
      ? "fresh"
      : nowSec <= (c.grace_until ?? 0)
        ? "grace"
        : "expired (→ free)";
  process.stderr.write(
    `cache: plan=${c.plan} window=${window} features=${Object.keys(c.features ?? {}).join(",")}\n` +
      `  org=${c.org_id} machine=${c.machine_id} fresh_until=${new Date((c.fresh_until ?? 0) * 1000).toISOString()}\n`
  );
  const envSet =
    process.env.UNERR_ENTITLEMENT_KID && process.env.UNERR_ENTITLEMENT_PUBKEY;
  process.stderr.write(
    envSet
      ? "  env override is set in THIS shell.\n"
      : "  env override NOT set in this shell — the daemon must have it for the token to verify.\n"
  );
}

function cmdClear(args) {
  if (existsSync(CACHE_PATH)) {
    rmSync(CACHE_PATH);
    process.stderr.write(`✓ removed ${CACHE_PATH} — effective tier is free.\n`);
  } else {
    process.stderr.write("no cache to remove.\n");
  }
  const wire = flag(args, "--unwire-mcp");
  if (wire) {
    const mcpPath = resolve(typeof wire === "string" ? wire : ".mcp.json");
    const names = unwireMcp(mcpPath);
    process.stderr.write(
      names.length
        ? `✓ removed override env from ${mcpPath} (${names.join(", ")})\n`
        : `no override env in ${mcpPath}\n`
    );
  }
}

function cmdDerivePubkey(args) {
  let priv = args[0];
  let kid;
  const fromEnv = flag(args, "--from-env");
  if (typeof fromEnv === "string") {
    const e = parseDotenv(resolve(fromEnv));
    priv = e.ENTITLEMENT_SIGNING_KEY;
    kid = e.ENTITLEMENT_SIGNING_KID;
    if (!priv)
      throw new Error(`ENTITLEMENT_SIGNING_KEY not found in ${fromEnv}`);
  }
  if (!priv) {
    process.stderr.write(
      "usage: dev-entitlement derive-pubkey <base64-pkcs8-priv> [kid]\n" +
        "       dev-entitlement derive-pubkey --from-env <path-to-.env>\n"
    );
    process.exit(1);
  }
  kid = kid ?? args[1] ?? "k-server-local";
  const pubkey = publicKeyFromPrivate(priv);
  process.stderr.write(
    `\n  Trust the server's signing key (${kid}) so its real tokens verify locally:\n\n`
  );
  printEnv(kid, pubkey);
}

function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  try {
    switch (cmd) {
      case "mint":
        return cmdMint(rest);
      case "status":
        return cmdStatus();
      case "clear":
        return cmdClear(rest);
      case "derive-pubkey":
        return cmdDerivePubkey(rest);
      default:
        process.stderr.write(
          "dev-entitlement — local entitlement minter (dev only)\n\n" +
            "  mint <free|pro|team|enterprise> [--features a,b] [--age-hours N] [--wire-mcp [path]]\n" +
            "  status\n" +
            "  clear [--unwire-mcp [path]]\n" +
            "  derive-pubkey <base64-pkcs8-priv>|--from-env <.env path> [kid]\n"
        );
        process.exit(cmd ? 1 : 0);
    }
  } catch (err) {
    process.stderr.write(
      `✗ ${err instanceof Error ? err.message : String(err)}\n`
    );
    process.exit(1);
  }
}

if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  main();
}
