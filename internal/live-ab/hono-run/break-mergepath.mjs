/**
 * Synthetic break for Track 4 task t1: makes `mergePath` insert a slash even when
 * the base already ends in one, so `mergePath('/book/', '/hey')` yields
 * `/book//hey`. Exact-string replace; throws if the anchor is gone (hono drifted)
 * so the break can never silently no-op. Run with cwd = the hono worktree.
 */
import { readFileSync, writeFileSync } from "node:fs";

const file = "src/utils/url.ts";
const anchor = "base?.at(-1) === '/' ? '' : '/'";
const broken = "'/'";

const src = readFileSync(file, "utf8");
if (!src.includes(anchor)) {
  throw new Error(`break-mergepath: anchor not found in ${file} — hono drifted`);
}
writeFileSync(file, src.replace(anchor, broken));
process.stderr.write("break-mergepath: applied\n");
