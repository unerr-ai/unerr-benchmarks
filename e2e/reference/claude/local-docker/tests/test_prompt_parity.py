"""HARNESS_UNIVERSAL.md §9 enforcement: the ON-harness prompt block must
stay byte-identical between harbor_agents.py:_build_autonomy_prompt() and
run-instance.sh's inline prompt-assembly fragment, for BOTH escalation
shapes (ladder / panel).

This repo has no pytest.ini/pyproject.toml/CI config at the time this test
was added (checked: no pytest config, no pre-commit hook, no lint entry
point wired to this path) — so §9's checker lives here, alongside the
existing harness test suite, and runs the same way pytest already does.

The actual render + diff logic lives in
e2e/distributed/tools/check_prompt_parity.py (also runnable standalone:
`python3 e2e/distributed/tools/check_prompt_parity.py`); this file just
wires it into the suite `pytest e2e/reference/claude/local-docker/tests/`
already runs, imported by file path like debug_instance.py imports
collect-failed.py (a cousin directory, not a normal package).

Run: python3 -m pytest e2e/reference/claude/local-docker/tests/test_prompt_parity.py -q
"""
import importlib.util
import sys
from pathlib import Path

_CHECKER_PATH = (
    Path(__file__).resolve().parents[4]  # -> e2e/
    / "distributed" / "tools" / "check_prompt_parity.py"
)


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_prompt_parity", _CHECKER_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec: check_prompt_parity.py's
    # @dataclass VariantResult needs `sys.modules[cls.__module__]` resolvable
    # during class creation (a stdlib dataclasses quirk on dynamic imports).
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


checker = _load_checker()


def _assert_variant(panel):
    py_text = checker.render_python(panel)
    sh_text = checker.render_bash(panel)
    variant = "panel (ESCALATION_PANEL=1)" if panel else "ladder (ESCALATION_PANEL unset/0)"
    assert py_text == sh_text, (
        f"harbor_agents.py and run-instance.sh's {variant} ON-harness prompt "
        f"block have drifted (harbor_agents.py={len(py_text)} chars, "
        f"run-instance.sh={len(sh_text)} chars) — see HARNESS_UNIVERSAL.md §9. "
        f"Run `python3 e2e/distributed/tools/check_prompt_parity.py` for a diff."
    )


def test_ladder_variant_byte_identical():
    _assert_variant(False)


def test_panel_variant_byte_identical():
    _assert_variant(True)
