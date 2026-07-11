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

import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request


# run-benchmark.py and the swebench harness live in the image's /work/.venv, where
# `datasets` + `swebench` are installed (NOT on the system python — Dockerfile.dist).
# worker-loop itself is stdlib-only and runs on system python3, but it MUST invoke
# those two subprocesses with the venv interpreter or they die instantly with
# ModuleNotFoundError (this is the "resolve rc=1 in ~1s" the smoke hit). Mirrors the
# single-machine entrypoint's PY=/work/.venv/bin/python.
VENV_PY = os.environ.get("VENV_PY", "/work/.venv/bin/python")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


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
        self.run_id = os.environ.get("RUN_ID", "dist")
        self.dataset = os.environ.get("DATASET", "princeton-nlp/SWE-bench_Verified")
        self.split = os.environ.get("SPLIT", "test")
        self.timeout = _int_env("PER_INSTANCE_TIMEOUT", 2700)
        self.grade_workers = _int_env("GRADE_WORKERS", 6)

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
        try:
            run_dir = os.path.join(scratch, self.run_id)
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
            stop.set()
            hb.join(timeout=10)
            shutil.rmtree(scratch, ignore_errors=True)

        # Idempotency guard: if the lease was reaped, another worker owns this
        # instance now — do NOT report, or we clobber the new owner's result.
        if abandon.is_set():
            self.log(f"{iid}: lease reaped (stale heartbeat) — NOT reporting; "
                     f"new owner will finish it")
            return

        if error or not patch:
            self._post_fail(iid, error or "resolve produced no patch")
            self.log(f"{iid}: reported /fail ({error or 'empty patch'})")
        else:
            self._post_complete(iid, patch, report_text, meta_text, resolved)
            self.log(f"{iid}: reported /complete resolved={resolved}")

    # ── resolve step (shell out to the arm's runner) ─────────────────────────
    def _resolve(self, iid: str, scratch: str) -> int:
        runner = self._runner_path()
        cmd = [VENV_PY, runner, "--ids", iid, "--out", scratch,
               "--label", self.run_id, "--timeout", str(self.timeout), "--parallel", "1"]
        env = os.environ.copy()
        # Mirror the proven single-machine econ path (fullresolve/entrypoint.sh §3):
        # clear DOCKER_DEFAULT_PLATFORM so run-benchmark's os.environ.setdefault stays
        # a no-op (the fly VM is already x86_64; forcing linux/amd64 is unnecessary).
        env["DOCKER_DEFAULT_PLATFORM"] = ""
        self.log(f"{iid}: resolve -> {' '.join(cmd)}")
        proc = subprocess.run(cmd, env=env, timeout=self.timeout + 300)
        self.log(f"{iid}: resolve rc={proc.returncode}")
        return proc.returncode

    # ── grade step (swebench harness, per-instance, in a scratch CWD) ────────
    def _grade(self, iid: str, scratch: str, preds_path: str,
               model_name: str | None) -> tuple[bool, str]:
        grade_cwd = os.path.join(scratch, "grade")
        os.makedirs(grade_cwd, exist_ok=True)
        cmd = [VENV_PY, "-m", "swebench.harness.run_evaluation",
               "--dataset_name", self.dataset, "--split", self.split,
               "--predictions_path", preds_path, "--run_id", self.run_id,
               "--instance_ids", iid, "--max_workers", str(self.grade_workers),
               "--cache_level", "env", "--clean", "True", "--timeout", str(self.timeout)]
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
        return f"driver_rc={rc} patch_bytes={pb} stderr_tail: {tail[-2600:]}"

    # ── report back to the coordinator ───────────────────────────────────────
    def _post_complete(self, iid: str, patch: str, report_json: str,
                       meta_json: str, resolved: bool) -> None:
        self._request("/complete", {
            "instance_id": iid,
            "worker_id": self.worker_id,
            "patch": patch,
            "report_json": report_json,
            "meta_json": meta_json,
            "resolved": bool(resolved),
        }, max_attempts=8)

    def _post_fail(self, iid: str, error: str) -> None:
        self._request("/fail", {
            "instance_id": iid,
            "worker_id": self.worker_id,
            "error": str(error)[:4000],
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
        runners = {"econ": "/work/local-docker/run-benchmark.py"}
        return runners.get(self.arm, "/work/local-docker/run-benchmark.py")


def main() -> int:
    if not (os.environ.get("COORDINATOR_URL") or "").strip():
        print("ERROR: COORDINATOR_URL is required "
              "(e.g. http://<coord_id>.vm.<app>.internal:8080)", file=sys.stderr)
        return 2
    return Worker().run()


if __name__ == "__main__":
    raise SystemExit(main())
