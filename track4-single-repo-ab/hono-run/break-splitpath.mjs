/**
 * Synthetic break for Track 4 task t2: makes `splitPath` stop dropping the empty
 * leading segment, so `splitPath('/foo/bar')` returns `['', 'foo', 'bar']` and
 * route-path splitting downstream breaks too. Exact-string replace; throws if the
 * anchor is gone so the break can never silently no-op. cwd = the hono worktree.
 */
import { readFileSync, writeFileSync } from "node:fs";

const file = "src/utils/url.ts";
const anchor = "if (paths[0] === '') {";
const broken = "if (paths[0] === ' ') {";

const src = readFileSync(file, "utf8");
if (!src.includes(anchor)) {
  throw new Error(`break-splitpath: anchor not found in ${file} — hono drifted`);
}
writeFileSync(file, src.replace(anchor, broken));
process.stderr.write("break-splitpath: applied\n");
