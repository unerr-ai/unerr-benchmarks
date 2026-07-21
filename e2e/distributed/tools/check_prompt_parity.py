#!/usr/bin/env python3
"""check_prompt_parity.py — mechanically enforce HARNESS_UNIVERSAL.md §9: the
ON-harness prompt block (TRACK -> SHAPE -> ONBOARD -> FIX DISCIPLINE ->
DELEGATION -> ESCALATION -> FINISH CONTRACT) must be byte-identical between
the two sites that author it:
  1. harbor_agents.py:_build_autonomy_prompt()      (Python, terminal flow)
  2. run-instance.sh's inline prompt-assembly block  (bash, SWE flow)

WHY THIS EXISTS
---------------
§9 previously said to "verify byte-identity with the scratch check" — an
ad-hoc, unversioned, never-actually-run step. It silently failed: the
ESCALATION_PANEL=1 variant drifted by ~5 words between the two sites (see
HARNESS_UNIVERSAL.md §9/§12 for the fix) even though (1)'s own docstring
calls that paragraph a "frozen contract ... byte-identical — never re-word
it". This script replaces the honor system with a real, runnable check.

HOW IT RENDERS "FOR REAL" (not by eyeballing or regex-slicing the rendered
prompt text):
  - Site 1: imports harbor_agents.py and calls _build_autonomy_prompt()
    directly. The only obstacle is its module-level `from harbor...import`
    lines — the `harbor` pip package is a Docker-toolbox-only dependency,
    installed over the network, that _build_autonomy_prompt() itself never
    touches — so _stub_harbor() below registers a minimal permissive stand-in
    package tree in sys.modules before import, letting the REAL function run.
  - Site 2: run-instance.sh has no importable function — the block is built
    by plain bash variable assignment/interpolation inline in the script
    (read live: it is NOT a `cat <<EOF` heredoc, despite the name). This
    script locates that fragment between two unique, content-based anchors
    (re-read from the live file every call, never a cached line range) and
    hands the literal extracted source to a REAL `bash -c` subprocess with
    PANEL preset, so bash itself performs the string interpolation.

Checks BOTH escalation variants — ladder (ESCALATION_PANEL unset/0, the
default run-distributed.sh uses) and panel (ESCALATION_PANEL=1) — since
checking only the default is exactly how the panel drift survived.

Standalone: no fly/docker/network dependency. Pure local file reads +
one bash subprocess.

CLI:
    check_prompt_parity.py

Exit: 0 both variants match; 1 at least one variant mismatches (diff printed
to stderr); 2 a render itself failed (anchor/import broke, not a text diff).

Run: python3 e2e/distributed/tools/check_prompt_parity.py
"""
from __future__ import annotations

import difflib
import importlib.util
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent
_E2E_DIR = _TOOLS_DIR.parents[1]  # e2e/

PY_SITE = _TOOLS_DIR / "harbor_agents.py"
SH_SITE = _E2E_DIR / "reference" / "claude" / "local-docker" / "context" / "run-instance.sh"

# The shared block both sites must render byte-identically starts here —
# everything before this (the per-flow BASE line + operator policy) is
# legitimately allowed to differ per §9.
TRACK_MARKER = "TRACK — before your first edit"

# Content anchors bracketing the prompt-assembly fragment inside
# run-instance.sh's `if [ "$HARNESS_ON" = "1" ]` branch. Two other,
# unrelated `$HARNESS_ON` branches exist in the file, so a line-number or
# generic-pattern anchor is not unique enough — these two literal strings
# are.
_SH_START_ANCHOR = '    TEST_FILES_BULLET="'
_SH_END_ANCHOR = '$FINISH_CONTRACT"'


def _stub_harbor() -> None:
    """Register a minimal fake `harbor` package tree in sys.modules so
    harbor_agents.py's module-level `from harbor... import ...` lines
    resolve without the real harbor-eval pip package. Permissive stand-ins
    only (empty ENV_VARS list, *args/**kwargs constructors) — safe because
    _build_autonomy_prompt() never touches any of these symbols; a no-op if
    the real package is already importable.
    @sem domain=benchmark-harness role=test-fixture
    """
    if "harbor.agents.installed.base" in sys.modules:
        return

    class _StubBase:
        ENV_VARS: list = []

        def __init__(self, *a, **k):
            pass

        def build_cli_flags(self, *a, **k):
            return []

    class _StubPermissive:
        def __init__(self, *a, **k):
            pass

    def _stub_nvm(*a, **k):
        return ""

    for name in (
        "harbor", "harbor.agents", "harbor.agents.installed",
        "harbor.agents.installed.base", "harbor.agents.installed.claude_code",
        "harbor.agents.installed.node_install", "harbor.environments",
        "harbor.environments.base", "harbor.models", "harbor.models.task",
        "harbor.models.task.config",
    ):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["harbor.agents.installed.base"].EnvVar = _StubPermissive
    sys.modules["harbor.agents.installed.claude_code"].ClaudeCode = _StubBase
    sys.modules["harbor.agents.installed.node_install"].nvm_node_install_snippet = _stub_nvm
    sys.modules["harbor.environments.base"].BaseEnvironment = object
    sys.modules["harbor.models.task.config"].MCPServerConfig = _StubPermissive


def _load_harbor_agents():
    _stub_harbor()
    spec = importlib.util.spec_from_file_location("check_prompt_parity_harbor_agents", PY_SITE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def render_python(panel: bool) -> str:
    """Site 1, for real: import harbor_agents.py and call
    _build_autonomy_prompt() with hooks_on=True (the FINISH CONTRACT
    paragraph is part of the required byte-identical block), then slice at
    the shared TRACK_MARKER — never a hand-copied approximation of the
    output.
    @sem domain=benchmark-harness role=parity-check
    """
    mod = _load_harbor_agents()
    full = mod._build_autonomy_prompt(True, panel)
    idx = full.find(TRACK_MARKER)
    if idx < 0:
        raise RuntimeError(
            f"TRACK marker {TRACK_MARKER!r} not found in {PY_SITE}'s rendered "
            "output — _build_autonomy_prompt() changed shape, update this checker"
        )
    return full[idx:]


def render_bash(panel: bool) -> str:
    """Site 2, for real: locate the prompt-assembly fragment inside
    run-instance.sh's HARNESS_ON branch between _SH_START_ANCHOR/
    _SH_END_ANCHOR (re-read from the live file every call), then hand that
    literal extracted source to a real `bash -c` subprocess with PANEL
    preset so bash itself performs the string interpolation — never a
    regex reconstruction of the result.
    @sem domain=benchmark-harness role=parity-check
    """
    text = SH_SITE.read_text()
    start = text.find(_SH_START_ANCHOR)
    if start < 0:
        raise RuntimeError(
            f"start anchor {_SH_START_ANCHOR!r} not found in {SH_SITE} — "
            "file structure changed, update _SH_START_ANCHOR"
        )
    end_marker_at = text.find(_SH_END_ANCHOR, start)
    if end_marker_at < 0:
        raise RuntimeError(
            f"end anchor {_SH_END_ANCHOR!r} not found in {SH_SITE} after the "
            "start anchor — file structure changed, update _SH_END_ANCHOR"
        )
    fragment = text[start:end_marker_at + len(_SH_END_ANCHOR)]

    script = (
        'set -u\n'
        'AUTONOMY_PROMPT=""\n'
        f'PANEL={"1" if panel else "0"}\n'
        + fragment + '\n'
        'printf "%s" "$AUTONOMY_PROMPT"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=15
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"bash render of the run-instance.sh fragment exited "
            f"{proc.returncode}: {proc.stderr}"
        )
    idx = proc.stdout.find(TRACK_MARKER)
    if idx < 0:
        raise RuntimeError(
            f"TRACK marker {TRACK_MARKER!r} not found in run-instance.sh's "
            "rendered output — the fragment's shape changed, update this checker"
        )
    return proc.stdout[idx:]


@dataclass
class VariantResult:
    name: str
    python_text: str
    bash_text: str

    @property
    def matches(self) -> bool:
        return self.python_text == self.bash_text


VARIANTS = (("ladder", False), ("panel", True))


def run_all() -> list[VariantResult]:
    """Render + collect both ESCALATION_PANEL variants. Raises RuntimeError
    (not a diff — a genuine render failure) if either site's shape has
    moved out from under the extraction anchors."""
    return [
        VariantResult(name, render_python(panel), render_bash(panel))
        for name, panel in VARIANTS
    ]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    try:
        results = run_all()
    except RuntimeError as exc:
        print(f"[check_prompt_parity] ERROR: {exc}", file=sys.stderr)
        return 2

    failed = False
    for r in results:
        if r.matches:
            print(f"[check_prompt_parity] {r.name}: MATCH ({len(r.python_text)} chars)")
        else:
            failed = True
            print(
                f"[check_prompt_parity] {r.name}: MISMATCH — "
                f"harbor_agents.py={len(r.python_text)} chars, "
                f"run-instance.sh={len(r.bash_text)} chars",
                file=sys.stderr,
            )
            diff = difflib.unified_diff(
                r.python_text.splitlines(keepends=True),
                r.bash_text.splitlines(keepends=True),
                fromfile="harbor_agents.py:_build_autonomy_prompt()",
                tofile="run-instance.sh (bash-rendered fragment)",
            )
            sys.stderr.writelines(diff)

    if failed:
        print(
            "[check_prompt_parity] FAIL — see HARNESS_UNIVERSAL.md §9",
            file=sys.stderr,
        )
        return 1
    print("[check_prompt_parity] PASS — both sites byte-identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
