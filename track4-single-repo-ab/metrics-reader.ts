/**
 * Reads one arm's `.unerr/metrics.db` for a run window and rolls the rows up
 * into the platform-pillar counts the A/B report scores. Pure read-only; never
 * writes. Used only for the unerr arms — the baseline arm has no metrics.db.
 *
 * @sem domain=benchmark role=reader
 */
import Database from "better-sqlite3";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { type PlatformEvents, emptyPlatform } from "./types.js";

/** A half-open run window in epoch-ms; rows with `ts` in [startTs, endTs] count. */
export interface MetricsWindow {
  startTs: number;
  endTs: number;
}

/**
 * `behavior_events.type` values that represent a guardrail actually firing —
 * a signal that stopped or flagged a risky edit. Kept as a default set the
 * caller can override; any type NOT in here still shows in `eventsByType`, so
 * a mis-set list under-counts guardrails but never hides an event.
 */
export const DEFAULT_GUARDRAIL_TYPES = new Set<string>([
  "cascade_guard",
  "boundary_violation_flagged",
  "incomplete_work_flagged",
  "blast_radius",
  "comment_drift",
  "convention_drift",
  "drift",
  "signature_edit_denied",
  "read_routing_denied",
]);

/**
 * Roll up the unerr telemetry written to `<unerrDir>/metrics.db` during a run
 * window. Returns an all-zero record if the DB does not exist (e.g. the unerr
 * proxy never started, or this is a dry run).
 */
export function readPlatformEvents(
  unerrDir: string,
  window: MetricsWindow,
  guardrailTypes: Set<string> = DEFAULT_GUARDRAIL_TYPES
): PlatformEvents {
  const dbPath = join(unerrDir, "metrics.db");
  if (!existsSync(dbPath)) {
    return emptyPlatform();
  }

  const db = new Database(dbPath, { readonly: true, fileMustExist: true });
  try {
    const { startTs, endTs } = window;

    const eventsByType: Record<string, number> = {};
    let behaviorRows = 0;
    let guardrailFires = 0;
    const behaviorRowsResult = db
      .prepare(
        `SELECT type, COUNT(*) AS n FROM behavior_events
         WHERE ts >= ? AND ts <= ? GROUP BY type`
      )
      .all(startTs, endTs) as Array<{ type: string; n: number }>;
    for (const row of behaviorRowsResult) {
      eventsByType[row.type] = row.n;
      behaviorRows += row.n;
      if (guardrailTypes.has(row.type)) {
        guardrailFires += row.n;
      }
    }

    const navRow = db
      .prepare(
        `SELECT COALESCE(SUM(tokens_saved), 0) AS saved FROM token_flow_events
         WHERE ts >= ? AND ts <= ?`
      )
      .get(startTs, endTs) as { saved: number };

    const compRow = db
      .prepare(
        `SELECT AVG(saved_pct) AS avg_pct FROM compression_events
         WHERE ts >= ? AND ts <= ?`
      )
      .get(startTs, endTs) as { avg_pct: number | null };

    return {
      guardrailFires,
      eventsByType,
      navTokensSaved: navRow.saved,
      compressSavedPct: compRow.avg_pct,
      behaviorRows,
    };
  } finally {
    db.close();
  }
}
