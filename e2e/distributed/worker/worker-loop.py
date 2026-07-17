#!/usr/bin/env python3
"""Distributed SWE-bench WORKER loop (Slice C of the distributed runner).

A work-stealing worker: pull ONE instance from the coordinator's queue, resolve it
(shell out to the arm's `run-benchmark.py --ids <one>`), grade it in place with the
swebench harness, and POST the {patch, report.json, meta} back. Repeat until the
queue drains, then exit 0 so the `--restart no` machine stops (billing ends).

Never reimplements resolve or grade — it drives the existing per-arm tools:
  RESOLVE:  python3 <runner> --ids <iid> --out <scratch> --label <run_id>
                            --timeout <PER_INSTANCE_TIMEOUT> --parallel 1
  GRADE:    python3 -m swebench.harness.run_evaluation --dataset_name <DATASET>
                            --split <SPLIT> --predictions_path <preds.json>
                            --run_id <run_id> --instance_ids <iid>
                            --max_workers <GRADE_WORKERS> --cache_level env
                            --clean True --timeout <PER_INSTANCE_TIMEOUT>

Lease model (PLAN.md decision 5): a background heartbeat thread POSTs /heartbeat
every 30s; if the coordinator answers {stale:true} the lease was reaped (another
worker owns the instance now) → set the ABANDON flag and DO NOT report, so the new
owner's result is never clobbered (at-least-once + idempotent = effectively-once).

Stdlib only (urllib for HTTP, subprocess, json, threading, os, time, plus
tempfile/glob/shutil/socket). Config comes entirely from the environment — see
Worker.__init__ and worker-entrypoint.sh.
"""
from __future__ import annotations

import base64
import glob
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

# The benchmark descriptor registry (verified|pro|terminal) lives next to suite.py
# under /work/distributed/tools. Add it to the path so the worker dispatches grade,
# timeout, and trace-collection per benchmark. benchmarks.py is itself stdlib-only
# at import, so the worker's "stdlib only" property holds. DIST_TOOLS_DIR overrides
# the baked path (used when running from the repo checkout in tests).
for _tools_dir in (
    os.environ.get("DIST_TOOLS_DIR", "/work/distributed/tools"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"),
):
    if os.path.isdir(_tools_dir) and _tools_dir not in sys.path:
        sys.path.insert(0, _tools_dir)
import benchmarks  # noqa: E402  — sibling module (added to sys.path just above)


# run-benchmark.py and the swebench harness live in the image's /work/.venv, where
# `datasets` + `swebench` are installed (NOT on the system python — Dockerfile.dist).
# worker-loop itself is stdlib-only and runs on system python3, but it MUST invoke
# those two subprocesses with the venv interpreter or they die instantly with
# ModuleNotFoundError (this is the "resolve rc=1 in ~1s" the smoke hit). Mirrors the
# single-machine entrypoint's PY=/work/.venv/bin/python.
VENV_PY = os.environ.get("VENV_PY", "/work/.venv/bin/python")

# Size caps for the per-instance artifact sync (S7b): events.jsonl/err.txt ride
# the /complete POST as plain text, opencode.db as base64 — bounded so one
# instance's transcript can't balloon the coordinator's queue.db unbounded.
# Typical sizes observed on the fullresolve path: events.jsonl 50KB-1.3MB,
# err.txt 4-16KB, opencode.db 0.4-5MB — these caps cover the normal case and
# only truncate/skip pathological outliers.
MAX_ARTIFACT_TEXT_BYTES = int(os.environ.get("MAX_ARTIFACT_TEXT_BYTES", 5_000_000))
MAX_ARTIFACT_DB_BYTES = int(os.environ.get("MAX_ARTIFACT_DB_BYTES", 8_000_000))
# engine.log is already tail-capped in-container by run-instance.sh, but the
# worker caps again defensively — mirrors the MAX_ARTIFACT_TEXT_BYTES pattern.
MAX_ENGINE_LOG_BYTES = int(os.environ.get("MAX_ENGINE_LOG_BYTES", 10_000_000))


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _load_difficulty(dirpath: str) -> "dict[str, str]":
    """Map each instance_id -> SWE-bench difficulty tier by reading the shipped
    swebench-verified-difficulty/<tier>.ids.txt lists (easy/medium/hard/
    veryhard). Returns {} when the dir is absent, so callers fall back to the
    flat PER_INSTANCE_TIMEOUT and behaviour is unchanged off the distributed
    image (Lite/mini/local runs)."""
    tiers: "dict[str, str]" = {}
    for tier in ("easy", "medium", "hard", "veryhard"):
        try:
            with open(os.path.join(dirpath, f"{tier}.ids.txt"), encoding="utf-8") as fh:
                for line in fh:
                    iid = line.strip()
                    if iid:
                        tiers[iid] = tier
        except OSError:
            continue
    return tiers


# Stall watchdog (S8): the resolve subprocess's stdout+stderr are captured to a
# file (never streamed directly, since Popen replaces subprocess.run here) and
# polled for progress. run-instance.sh's hb_loop appends an `HB events_bytes=<n>`
# line to this SAME file every 240s regardless of whether claude is actually
# doing anything, so heartbeat-LINE growth is EXCLUDED from the progress
# signal — progress = EITHER a non-heartbeat log line changing (build/index/
# claude tool output/grade output) OR the heartbeat's events_bytes VALUE
# itself increasing (claude emitting new turns). Otherwise a hung claude
# (events_bytes frozen, no non-heartbeat output) would still tick the file
# size every 4 minutes and the stall clock would never fire. No progress for
# stall_kill_s -> the resolve is killed so the attempt fails and the
# coordinator re-leases it (a fresh restart).
STALL_KILL_S = int(os.environ.get("STALL_KILL_S", "2700"))
_HB_RE = re.compile(rb"HB events_bytes=")
_EVENTS_BYTES_RE = re.compile(rb"events_bytes=(\d+)")


def _progress_signal(logpath: str, tail_bytes: int = 65_536) -> tuple[int | None, bytes]:
    """Read the last `tail_bytes` of `logpath` ONCE (never the whole file) and
    derive BOTH progress signals from that single read: the last
    `events_bytes=<n>` heartbeat value (None if no heartbeat has landed yet)
    and the last line that is NOT a heartbeat line (b"" if the tail is empty
    or entirely heartbeat lines). Heartbeat lines tick on a fixed timer
    regardless of real progress, so they're excluded from the second signal —
    only events_bytes' own value and non-heartbeat output count."""
    try:
        with open(logpath, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            chunk = f.read()
    except OSError:
        return None, b""
    matches = _EVENTS_BYTES_RE.findall(chunk)
    last_events_bytes = int(matches[-1]) if matches else None
    last_non_hb_line = b""
    for line in chunk.splitlines():
        if not _HB_RE.search(line):
            last_non_hb_line = line
    return last_events_bytes, last_non_hb_line


def _kill_process_group(proc: "subprocess.Popen") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _watch_resolve(proc: "subprocess.Popen", logpath: str, deadline: float,
                    timeout_s: float, *, stall_kill_s: int = STALL_KILL_S,
                    poll_s: float = 30.0, extra_paths: "tuple[str, ...]" = (),
                    sleep_fn=time.sleep, now_fn=time.time) -> str | None:
    """Poll `proc` (writing to `logpath`) every `poll_s` until it exits, the
    epoch `deadline` passes, or no progress — via `_progress_signal`: the
    log's last `events_bytes=` heartbeat value increasing OR its last
    non-heartbeat line changing (heartbeat-LINE growth alone does not count,
    or a truly hung claude would never trip the clock) — is seen for
    `stall_kill_s` seconds.

    A deadline kills the process group and raises subprocess.TimeoutExpired
    (mirroring subprocess.run(timeout=...), which this replaces). A stall
    kills the process group and RETURNS an error string instead of raising,
    so the caller can fold it into the existing error-reporting path rather
    than a distinct exception type. Returns None when the process exited on
    its own (the normal case)."""
    last_sig: object = None
    last_progress = now_fn()
    while True:
        rc = proc.poll()
        if rc is not None:
            return None
        now = now_fn()
        if now >= deadline:
            _kill_process_group(proc)
            raise subprocess.TimeoutExpired(cmd=getattr(proc, "args", "resolve"),
                                             timeout=timeout_s)
        # Progress = the captured driver-log signal (heartbeat value / non-HB
        # line) OR any live artifact file growing (econ's host-mounted
        # events.jsonl/err.txt). extra_paths absent -> just the driver signal.
        sig = (_progress_signal(logpath),
               tuple(os.path.getsize(p) if os.path.exists(p) else 0
                     for p in extra_paths))
        if sig != last_sig:
            last_sig = sig
            last_progress = now
        elif now - last_progress >= stall_kill_s:
            _kill_process_group(proc)
            return f"stalled: no log growth for {stall_kill_s}s (killed for restart)"
        sleep_fn(poll_s)


class CoordinatorError(Exception):
    """The coordinator could not be reached / answered an error status."""


class Worker:
    """One work-stealing worker: claim -> resolve -> grade -> report, until drain."""

    def __init__(self) -> None:
        self.coordinator = (os.environ.get("COORDINATOR_URL") or "").rstrip("/")
        # WORKER_ID identifies the lease holder; default to the fly machine id.
        self.worker_id = (
            os.environ.get("WORKER_ID")
            or os.environ.get("FLY_MACHINE_ID")
            or socket.gethostname()
        )
        self.arm = os.environ.get("ARM", "econ")
        # BENCHMARK (verified|pro|terminal) is orthogonal to ARM: the arm picks the
        # runner, the benchmark descriptor picks the dataset/grade/timeout/traces/flow.
        self.benchmark = os.environ.get("BENCHMARK", benchmarks.DEFAULT_BENCHMARK)
        self.bench = benchmarks.get(self.benchmark)
        self.flow = self.bench["flow"]
        self.run_id = os.environ.get("RUN_ID", "dist")
        # Grade dataset/split come from the descriptor (Verified keeps its exact
        # defaults); env still overrides for ad-hoc runs.
        self.dataset = os.environ.get(
            "DATASET", self.bench.get("grade_dataset") or self.bench.get("dataset"))
        self.split = os.environ.get("SPLIT", self.bench.get("split", "test"))
        _tmo = self.bench.get("timeout", {})
        self.timeout = _int_env(_tmo.get("default_env", "PER_INSTANCE_TIMEOUT"),
                                _tmo.get("default", 2700))
        self.grade_workers = _int_env("GRADE_WORKERS", 6)
        # Difficulty-aware per-instance ceiling: SWE-bench Verified tasks span
        # "<15 min" to ">4 hours" of expected effort, so one flat timeout either
        # starves hard tasks or over-waits on easy ones. For Verified the tiers come
        # from the shipped difficulty id-lists (DIFFICULTY_DIR); benchmarks whose
        # descriptor doesn't use that source keep the flat self.timeout until their
        # adapter loads per-instance tiers. Unknown ids always keep self.timeout.
        self.difficulty = (
            _load_difficulty(os.environ.get(
                "DIFFICULTY_DIR", "/work/swebench-verified-difficulty"))
            if _tmo.get("difficulty") == "verified_difficulty_dir" else {})
        self.tier_timeouts = {
            tier: _int_env(_tmo.get("tier_env", {}).get(tier, ""), secs)
            for tier, secs in _tmo.get("tiers", {}).items()
        }

    # ── logging ──────────────────────────────────────────────────────────────
    def log(self, msg: str) -> None:
        print(f"[worker-loop {self.worker_id}] {msg}", file=sys.stderr, flush=True)

    # ── HTTP to the coordinator (6PN), transient-retry with backoff ──────────
    def _request(self, path: str, body: dict, max_attempts: int = 6,
                 base_delay: float = 2.0, timeout: int = 120) -> dict:
        """POST JSON, return parsed JSON. Retries transient connection errors
        (and 5xx) with exponential backoff; raises CoordinatorError on give-up
        or a 4xx. The coordinator may restart, so connection refused is transient."""
        url = self.coordinator + path
        data = json.dumps(body).encode("utf-8")
        last: object = None
        for attempt in range(1, max_attempts + 1):
            try:
                req = urllib.request.Request(
                    url, data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as e:
                if e.code >= 500 and attempt < max_attempts:
                    last = e  # server hiccup / restart → retry
                else:
                    detail = ""
                    try:
                        detail = e.read().decode("utf-8", "replace")[:200]
                    except Exception:
                        pass
                    raise CoordinatorError(f"HTTP {e.code} on {path}: {detail}")
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                last = e
            if attempt < max_attempts:
                time.sleep(min(base_delay * (2 ** (attempt - 1)), 30.0))
        raise CoordinatorError(f"{path}: no response after {max_attempts} attempt(s): {last}")

    # ── main loop ────────────────────────────────────────────────────────────
    def run(self) -> int:
        self.log(
            f"online: coordinator={self.coordinator} arm={self.arm} run_id={self.run_id} "
            f"dataset={self.dataset} split={self.split} timeout={self.timeout} "
            f"grade_workers={self.grade_workers}"
        )
        consecutive_fail = 0
        while True:
            try:
                # One attempt per loop; the loop itself provides the backoff so we
                # can count 10 consecutive connection failures -> exit non-zero.
                resp = self._request("/claim", {"worker_id": self.worker_id},
                                     max_attempts=1, timeout=60)
                consecutive_fail = 0
            except CoordinatorError as e:
                consecutive_fail += 1
                self.log(f"claim connect failed {consecutive_fail}/10: {e}")
                if consecutive_fail >= 10:
                    self.log("coordinator unreachable 10x in a row — exiting non-zero")
                    return 1
                time.sleep(min(2.0 * consecutive_fail, 30.0))
                continue

            if resp.get("done"):
                self.log("queue drained (done) — exiting 0; machine will stop")
                return 0
            if resp.get("wait"):
                # Leases still in flight; they may be requeued if a worker dies.
                self.log("coordinator says wait (leases in flight) — polling again in ~10s")
                time.sleep(10)
                continue

            iid = resp.get("instance_id")
            if not iid:
                self.log(f"unexpected /claim response {resp!r} — retrying in 10s")
                time.sleep(10)
                continue

            self.log(f"claimed {iid}")
            try:
                self._process(iid)
            except CoordinatorError as e:
                # Reporting failed after retries — leave it: the reaper requeues the
                # stale lease and another worker (or a later claim) redoes it.
                self.log(f"{iid}: could not reach coordinator to report ({e}); "
                         f"lease will expire & requeue")
            except Exception as e:  # noqa: BLE001 — one instance must not sink the loop
                self.log(f"{iid}: unhandled error {type(e).__name__}: {e}")
                try:
                    self._post_fail(iid, f"{type(e).__name__}: {str(e)[:500]}")
                except CoordinatorError:
                    pass
            finally:
                # Reclaim ephemeral-rootfs space between instances (no volume —
                # PLAN.md decision 2).
                self._prune_images()

    # ── one instance: resolve -> grade -> report (respecting abandon) ─────────
    def _process(self, iid: str) -> None:
        stop = threading.Event()
        abandon = threading.Event()
        hb = threading.Thread(target=self._heartbeat_loop,
                              args=(iid, stop, abandon), daemon=True)
        hb.start()

        scratch = tempfile.mkdtemp(prefix=f"dist-{iid}-")
        patch = ""
        meta_text = ""
        report_text = ""
        resolved = False
        error: str | None = None
        artifacts: dict = {}
        # Computed before the try so `finally` can always read the instance's
        # raw transcript prior to scratch cleanup, regardless of which branch
        # (success/timeout/exception) this run took.
        run_dir = os.path.join(scratch, self.run_id)
        try:
            if self.flow == benchmarks.FLOW_HARNESS_RUN:
                # Fused run+grade (Terminal-Bench): the benchmark harness runs the
                # agent INSIDE its task container and grades with pytest — there is
                # no intermediate git patch. Delegated to the vendored harness
                # module; it returns the verdict + its own report/transcript/patch.
                resolved, report_text, patch, meta_text = self._run_harness(
                    iid, scratch, abandon)
                if not abandon.is_set() and not resolved and not report_text:
                    error = "harness run produced no result"
            else:
                # resolve_then_grade (Verified/Pro): arm runner -> preds.json -> grade.
                preds_path = os.path.join(run_dir, "preds.json")
                meta_path = os.path.join(run_dir, "meta.jsonl")

                rc = self._resolve(iid, scratch)
                patch, model_name = self._read_patch(preds_path, iid)
                meta_text = self._read_meta(meta_path, iid)

                if not patch:
                    # Surface WHY: run-benchmark writes the container's driver rc + stderr
                    # tail into meta.jsonl even on an empty diff. Fold it into the /fail
                    # reason so it reaches the coordinator log (the only reliable sink —
                    # worker stdout is lost when the machine self-stops on drain).
                    error = f"resolve produced no patch (rc={rc}); {self._meta_diag(meta_text)}"
                elif abandon.is_set():
                    pass  # lease reaped mid-resolve — skip grade + report below
                else:
                    resolved, report_text = self._grade(iid, scratch, preds_path, model_name)
        except subprocess.TimeoutExpired as e:
            error = f"timeout: {str(e)[:300]}"
        except Exception as e:  # noqa: BLE001 — convert to /fail below (unless abandoned)
            error = f"{type(e).__name__}: {str(e)[:500]}"
        finally:
            # Read the raw transcript (events.jsonl/err.txt/opencode.db) while
            # it still exists on the ephemeral fly VM — S7b: the distributed
            # path has no synced volume, so anything not sent to the
            # coordinator here vanishes with scratch below.
            artifacts = self._read_artifacts(run_dir, iid)
            stop.set()
            hb.join(timeout=10)
            shutil.rmtree(scratch, ignore_errors=True)

        # Idempotency guard: if the lease was reaped, another worker owns this
        # instance now — do NOT report, or we clobber the new owner's result.
        if abandon.is_set():
            self.log(f"{iid}: lease reaped (stale heartbeat) — NOT reporting; "
                     f"new owner will finish it")
            return

        # resolve_then_grade requires a patch to /complete; harness_run has none —
        # a graded result (resolved True or False, with a report) IS a completion.
        harness = self.flow == benchmarks.FLOW_HARNESS_RUN
        if error or (not harness and not patch):
            self._post_fail(iid, error or "resolve produced no patch")
            self.log(f"{iid}: reported /fail ({error or 'empty patch'})")
        else:
            self._post_complete(iid, patch, report_text, meta_text, resolved, artifacts)
            self.log(f"{iid}: reported /complete resolved={resolved}")

    # ── fused run+grade (harness_run flow, e.g. Terminal-Bench) ──────────────
    def _run_harness(self, iid: str, scratch: str,
                     abandon: threading.Event) -> tuple[bool, str, str, str]:
        """Run+grade one task via the benchmark's OWN harness and return
        (resolved, report_text, patch, meta_text). Delegates to the module named by
        the descriptor's `harness_module` (e.g. Terminal -> harness_terminal), which
        exposes `run(worker, iid, scratch, abandon)`. A missing/broken module raises,
        which _process turns into a /fail with the reason — never a silent pass."""
        mod_name = self.bench.get("harness_module")
        if not mod_name:
            raise RuntimeError(
                f"benchmark '{self.benchmark}' uses harness_run flow but its "
                f"descriptor names no harness_module")
        mod = __import__(mod_name)
        return mod.run(self, iid, scratch, abandon)

    # ── difficulty-aware per-instance ceiling ────────────────────────────────
    def _instance_timeout(self, iid: str) -> int:
        """Per-instance resolve ceiling in seconds by SWE-bench difficulty tier
        (easy/medium 3h, hard 5h, veryhard 8h — all env-overridable). Falls back
        to the flat PER_INSTANCE_TIMEOUT for ids with no tier data."""
        tier = self.difficulty.get(iid)
        if not tier:
            return self.timeout
        return self.tier_timeouts.get(tier, self.timeout)

    # ── resolve step (shell out to the arm's runner) ─────────────────────────
    def _resolve(self, iid: str, scratch: str) -> int:
        runner = self._runner_path()
        inst_timeout = self._instance_timeout(iid)
        cmd = [VENV_PY, runner, "--ids", iid, "--out", scratch,
               "--label", self.run_id, "--timeout", str(inst_timeout), "--parallel", "1"]
        # Both resolve_then_grade runners (econ, claude) are dataset-parameterized
        # (their --dataset defaults to Verified). Hand them the ACTIVE benchmark's
        # dataset/split + — for Pro — the vendored id-source, the sweap image
        # namespace, and the /app repo dir off the descriptor, so it stops
        # resolving Pro ids against Verified (the smoke's "no instances after
        # filter" → empty-patch bug). For Verified this is a no-op — --dataset
        # equals the runner's own default.
        if self.arm in ("econ", "claude"):
            cmd += ["--dataset", self.dataset, "--split", self.split]
            ids_jsonl = self.bench.get("ids_jsonl")
            if ids_jsonl:
                cmd += ["--ids-jsonl", ids_jsonl]
            dh_user = self.bench.get("dockerhub_username")
            if dh_user:
                cmd += ["--dockerhub-username", dh_user]
            repo_dir = self.bench.get("repo_dir")
            if repo_dir:
                cmd += ["--repo-dir", repo_dir]
            # resolve-side eval-image org (runner defaults to swebench; starryzhang for SWE-bench Live)
            ns = self.bench.get("image_namespace")
            if ns:
                cmd += ["--image-namespace", ns]
        env = os.environ.copy()
        # Mirror the proven single-machine econ path (fullresolve/entrypoint.sh §3):
        # clear DOCKER_DEFAULT_PLATFORM so run-benchmark's os.environ.setdefault stays
        # a no-op (the fly VM is already x86_64; forcing linux/amd64 is unnecessary).
        env["DOCKER_DEFAULT_PLATFORM"] = ""
        self.log(f"{iid}: resolve (ceiling={inst_timeout}s "
                 f"tier={self.difficulty.get(iid, 'default')}) -> {' '.join(cmd)}")
        # Captured (not streamed) so the stall watchdog can poll it for growth —
        # start_new_session=True makes proc.pid a process-group leader so a
        # stall/deadline kill takes the whole tree, not just the driver.
        logpath = os.path.join(scratch, "resolve-output.log")
        # The econ arm writes its event stream straight onto the host-mounted
        # artifact dir (run-instance.sh -> $OUT/events.jsonl), so it grows LIVE
        # here even though run-benchmark buffers the container's stdout/stderr.
        # Watching it gives the stall clock a real in-agent progress signal the
        # captured driver log alone lacks during the (buffered) agent phase;
        # absent (claude arm, pre-agent phases) it stays 0 and the driver-log
        # signal stands — so this only ADDS liveness, never removes it.
        art_dir = os.path.join(scratch, self.run_id, "artifacts", iid)
        live_paths = (os.path.join(art_dir, "events.jsonl"),
                      os.path.join(art_dir, "err.txt"))
        timeout_s = inst_timeout + 300
        deadline = time.time() + timeout_s
        with open(logpath, "wb") as logf:
            proc = subprocess.Popen(cmd, env=env, stdout=logf, stderr=subprocess.STDOUT,
                                     start_new_session=True)
            try:
                stall_error = _watch_resolve(proc, logpath, deadline, timeout_s,
                                             extra_paths=live_paths)
            finally:
                rc = proc.wait()
                self._tail_log_to_stdout(iid, logpath)
        if stall_error:
            self.log(f"{iid}: resolve {stall_error}")
            raise RuntimeError(stall_error)
        self.log(f"{iid}: resolve rc={rc}")
        return rc

    # ── stall-watchdog debuggability: output no longer streams directly ──────
    def _tail_log_to_stdout(self, iid: str, logpath: str, n: int = 40) -> None:
        """Echo the last `n` lines of the captured resolve log to worker
        stdout, prefixed with the instance id — preserves the pre-watchdog
        fly-logs visibility now that resolve output is captured to a file
        instead of streaming through this process directly."""
        try:
            with open(logpath, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            return
        for line in lines[-n:]:
            print(f"[{iid}] {line.rstrip()}", flush=True)

    # ── grade step (benchmark-dispatched) ────────────────────────────────────
    def _grade(self, iid: str, scratch: str, preds_path: str,
               model_name: str | None) -> tuple[bool, str]:
        """Grade one prediction for the ACTIVE benchmark and return
        (resolved, report_text). Verified/Lite use the swebench harness in-process
        (below); a benchmark whose descriptor names a `grade_module` (e.g. Pro ->
        grade_pro) delegates to that vendored module. Best-effort throughout: any
        grader failure yields (False, "") so a valid patch still /completes."""
        mod_name = self.bench.get("grade_module")
        if mod_name:
            return self._grade_via_module(mod_name, iid, scratch, preds_path, model_name)
        return self._grade_swebench(iid, scratch, preds_path, model_name)

    def _grade_via_module(self, mod_name: str, iid: str, scratch: str,
                          preds_path: str, model_name: str | None) -> tuple[bool, str]:
        """Delegate grading to a benchmark-specific module (e.g. grade_pro) that
        exposes `grade(worker, iid, scratch, preds_path, model_name) -> (bool, str)`.
        The module is vendored by that benchmark's adapter; if it isn't present yet,
        log and treat as unresolved rather than sinking the instance."""
        try:
            mod = __import__(mod_name)
        except Exception as e:  # noqa: BLE001 — module not vendored / import error
            self.log(f"{iid}: grade module '{mod_name}' unavailable ({e}) — "
                     f"treating as unresolved")
            return False, ""
        try:
            return mod.grade(self, iid, scratch, preds_path, model_name)
        except Exception as e:  # noqa: BLE001
            self.log(f"{iid}: grade via {mod_name} crashed ({e}) — unresolved")
            return False, ""

    # ── grade step (swebench harness, per-instance, in a scratch CWD) ────────
    def _grade_swebench(self, iid: str, scratch: str, preds_path: str,
                        model_name: str | None) -> tuple[bool, str]:
        grade_cwd = os.path.join(scratch, "grade")
        os.makedirs(grade_cwd, exist_ok=True)
        cmd = [VENV_PY, "-m", "swebench.harness.run_evaluation",
               "--dataset_name", self.dataset, "--split", self.split,
               "--predictions_path", preds_path, "--run_id", self.run_id,
               "--instance_ids", iid, "--max_workers", str(self.grade_workers),
               "--cache_level", "env", "--clean", "True", "--timeout", str(self.timeout)]
        # Optional private-registry namespace: SWEBENCH_NAMESPACE=51jaswanth15 makes the
        # harness pull eval images from the private Verified mirror
        # (51jaswanth15/sweb.eval.x86_64.*, the _1776_-normalized names the mirror
        # produced) instead of the public swebench org. Unset -> harness default, so
        # Verified grading is unchanged unless the launcher opts in.
        namespace = os.environ.get("SWEBENCH_NAMESPACE")
        if namespace:
            cmd += ["--namespace", namespace]
        self.log(f"{iid}: grade -> {' '.join(cmd)}")
        # Grade is best-effort: a grade crash/timeout still leaves us a valid patch
        # to /complete (resolved=False) rather than requeuing a solved instance.
        try:
            subprocess.run(cmd, cwd=grade_cwd, timeout=self.timeout + 600)
        except subprocess.TimeoutExpired:
            self.log(f"{iid}: grade timed out — treating as unresolved")
            return False, ""
        except Exception as e:  # noqa: BLE001
            self.log(f"{iid}: grade crashed ({e}) — treating as unresolved")
            return False, ""

        report_path = self._find_report(grade_cwd, iid, model_name)
        if not report_path:
            self.log(f"{iid}: no report.json produced — treating as unresolved")
            return False, ""
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                report_text = f.read()
            rep = json.loads(report_text)
            # The harness writes {"<iid>": {"resolved": bool, ...}}; tolerate a
            # top-level {"resolved": ...} too.
            inner = rep.get(iid, rep) if isinstance(rep, dict) else {}
            resolved = bool(inner.get("resolved")) if isinstance(inner, dict) else False
            return resolved, report_text
        except Exception as e:  # noqa: BLE001
            self.log(f"{iid}: could not parse report.json ({e})")
            return False, ""

    # ── per-instance artifact sync (S7b: events.jsonl/err.txt/opencode.db; ───
    # S7c: engine.log) ────────────────────────────────────────────────────────
    def _read_artifacts(self, run_dir: str, iid: str) -> dict:
        """Read the instance's raw transcript out of run-benchmark.py's
        artifact dir (run_dir/artifacts/<iid>/ — same layout the single-
        machine fullresolve path leaves on disk) so it can ride the /complete
        POST. Bounded by MAX_ARTIFACT_TEXT_BYTES/MAX_ARTIFACT_DB_BYTES/
        MAX_ENGINE_LOG_BYTES; a missing or oversized file yields None for that
        key (tail-capped, not dropped, for the text logs) rather than failing
        the instance."""
        art_dir = os.path.join(run_dir, "artifacts", iid)

        def _read_text(name: str, max_bytes: int = MAX_ARTIFACT_TEXT_BYTES) -> str | None:
            path = os.path.join(art_dir, name)
            if not os.path.isfile(path):
                return None
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                return None
            if len(data) > max_bytes:
                data = data[-max_bytes:]
            return data.decode("utf-8", "replace")

        db_path = os.path.join(art_dir, "opencode.db")
        db_b64 = None
        if os.path.isfile(db_path):
            try:
                if os.path.getsize(db_path) <= MAX_ARTIFACT_DB_BYTES:
                    with open(db_path, "rb") as f:
                        db_b64 = base64.b64encode(f.read()).decode("ascii")
                else:
                    self.log(f"{iid}: opencode.db too large to sync — skipping")
            except OSError:
                db_b64 = None

        # Descriptor-driven text traces: each (filename, /complete field) in the
        # active benchmark's trace list. engine.log keeps the larger cap. Verified/Pro
        # yield exactly events_jsonl/err_txt/engine_log (unchanged); Terminal's extra
        # traces (trajectory.json/sessions.cast) are read here too and ride once the
        # coordinator accepts those fields (wired by the Terminal adapter).
        out: dict = {}
        for fname, field in self.bench.get("traces", ()):
            cap = MAX_ENGINE_LOG_BYTES if fname == "engine.log" else MAX_ARTIFACT_TEXT_BYTES
            out[field] = _read_text(fname, cap)
        out["db_b64"] = db_b64
        return out

    def _find_report(self, grade_cwd: str, iid: str, model_name: str | None) -> str | None:
        # logs/run_evaluation/<run_id>/<model>/<instance_id>/report.json
        base = os.path.join(grade_cwd, "logs", "run_evaluation", self.run_id)
        if model_name:
            direct = os.path.join(base, model_name, iid, "report.json")
            if os.path.isfile(direct):
                return direct
        # Fallback: the harness sanitizes the model name ('/' -> '__') — glob it.
        matches = glob.glob(os.path.join(base, "*", iid, "report.json"))
        return matches[0] if matches else None

    # ── read run-benchmark.py output ─────────────────────────────────────────
    def _read_patch(self, preds_path: str, iid: str) -> tuple[str, str | None]:
        if not os.path.isfile(preds_path):
            return "", None
        try:
            with open(preds_path, "r", encoding="utf-8") as f:
                preds = json.load(f)
        except Exception:
            return "", None
        entry = preds.get(iid) if isinstance(preds, dict) else None
        if not isinstance(entry, dict):
            return "", None
        return entry.get("model_patch") or "", entry.get("model_name_or_path")

    def _read_meta(self, meta_path: str, iid: str) -> str:
        """Return the raw JSON line for this instance (last wins) from meta.jsonl."""
        if not os.path.isfile(meta_path):
            return ""
        last = ""
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("instance_id") == iid:
                        last = line
        except Exception:
            return last
        return last

    @staticmethod
    def _meta_diag(meta_text: str) -> str:
        """One-line failure diagnostic pulled from run-benchmark's meta.jsonl: the
        container driver rc, patch size, and the TAIL of the container's stderr — the
        actual reason a resolve produced an empty diff (opencode error vs ran-clean).
        Folded into the /fail reason so the root cause survives to the coordinator log."""
        if not meta_text:
            return "no meta.jsonl written (run-benchmark produced nothing — resolve likely crashed pre-container)"
        try:
            m = json.loads(meta_text)
        except Exception:  # noqa: BLE001
            return f"meta.jsonl unparseable: {meta_text[:200]}"
        rc = m.get("rc")
        pb = m.get("patch_bytes")
        tail = (m.get("stderr_tail") or "").replace("\n", " ⏎ ")
        return f"driver_rc={rc} patch_bytes={pb} stderr_tail: {tail[-10000:]}"

    # ── report back to the coordinator ───────────────────────────────────────
    def _post_complete(self, iid: str, patch: str, report_json: str,
                       meta_json: str, resolved: bool,
                       artifacts: dict | None = None) -> None:
        artifacts = artifacts or {}
        payload = {
            "instance_id": iid,
            "worker_id": self.worker_id,
            "patch": patch,
            "report_json": report_json,
            "meta_json": meta_json,
            "resolved": bool(resolved),
            # S7b/S7c: the per-instance transcript, synced here because the
            # distributed worker has no volume that survives the machine.
            "db_b64": artifacts.get("db_b64"),
        }
        # Forward exactly the trace fields the active benchmark's descriptor
        # declares, under their /complete field names — resolve_then_grade sends
        # events_jsonl/err_txt/engine_log; harness_run (Terminal) sends
        # events_jsonl/err_txt/trajectory_json/sessions_cast. Descriptor-driven so
        # a new trace type needs a descriptor entry + a coordinator column only,
        # never a worker edit. Unknown fields the coordinator ignores.
        for _fname, field in self.bench.get("traces", ()):
            payload[field] = artifacts.get(field)
        self._request("/complete", payload, max_attempts=8)

    def _post_fail(self, iid: str, error: str) -> None:
        # Capped above _meta_diag's 10000-char stderr tail so widening that cap
        # (dead-instance failure capture) isn't silently re-truncated here.
        self._request("/fail", {
            "instance_id": iid,
            "worker_id": self.worker_id,
            "error": str(error)[:10000],
        }, max_attempts=8)

    # ── heartbeat thread ─────────────────────────────────────────────────────
    def _heartbeat_loop(self, iid: str, stop: threading.Event,
                        abandon: threading.Event) -> None:
        # Beat every 30s until the instance finishes (stop set). A {stale:true}
        # answer means the reaper requeued our lease -> flag abandon and quit.
        while not stop.wait(30):
            try:
                resp = self._request("/heartbeat",
                                     {"instance_id": iid, "worker_id": self.worker_id},
                                     max_attempts=3, base_delay=1.0, timeout=30)
            except CoordinatorError as e:
                self.log(f"{iid}: heartbeat error (ignored this beat): {e}")
                continue
            if resp.get("stale"):
                self.log(f"{iid}: heartbeat says lease STALE — flagging abandon")
                abandon.set()
                return

    # ── docker housekeeping ──────────────────────────────────────────────────
    def _prune_images(self) -> None:
        try:
            subprocess.run(["docker", "image", "prune", "-f"], timeout=300,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            self.log(f"docker image prune failed (ignored): {e}")

    # ── arm-agnostic runner path ─────────────────────────────────────────────
    def _runner_path(self) -> str:
        # Each arm ships its resolver at a fixed /work path in the distributed image
        # (Slice E COPYs the arm's local-docker context there). econ is the v1 arm.
        runners = {
            "econ": "/work/local-docker/run-benchmark.py",
            "claude": "/work/claude/local-docker/run-benchmark.py",
        }
        return runners.get(self.arm, "/work/local-docker/run-benchmark.py")


def main() -> int:
    if not (os.environ.get("COORDINATOR_URL") or "").strip():
        print("ERROR: COORDINATOR_URL is required "
              "(e.g. http://<coord_id>.vm.<app>.internal:8080)", file=sys.stderr)
        return 2
    return Worker().run()


if __name__ == "__main__":
    raise SystemExit(main())
