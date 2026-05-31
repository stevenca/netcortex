#!/usr/bin/env python3
"""Forbid self-rewrite primitives in agentic and policy code paths.

This lint exists to enforce a security and regulatory invariant: the agent
layer (when it lands) is not permitted to mutate the policy library, contracts,
or its own source code at runtime. Pliable behavior happens through approved
configuration changes and human-reviewed PRs — never by eval/exec/dynamic import.

Rules
-----
1. ``exec(...)``, ``eval(...)``, ``compile(...)`` are forbidden anywhere under
   ``netcortex/`` and ``tools/``, with a narrow allow-list for legitimate uses
   (Jinja, template rendering). Allow-list entries must be explicit.

2. ``importlib.import_module(<non-literal>)`` is forbidden in the AGENT_PATHS
   listed below — these directories may not perform dynamic plugin loading;
   that responsibility belongs to a single, audited bootstrap module.

3. ``open(...).write`` or any other write to paths matching ``netcortex/policy/``
   or ``netcortex/contracts/`` from inside AGENT_PATHS is forbidden.

The lint operates on AST, not text, to be robust against formatting changes.

Run it from the repo root: ``python tools/lint_no_self_rewrite.py``. Non-zero
exit on violation.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

AGENT_PATHS = [
    "netcortex/prefrontal",
    "netcortex/conductor",
    "netcortex/language",
    "netcortex/association",
    "netcortex/reflex",
    "netcortex/hippocampus",
    "netcortex/plasticity",
]

PROTECTED_PATHS = [
    "netcortex/policy",
    "netcortex/contracts",
]

SCAN_ROOTS = [
    "netcortex",
    "tools",
]

EXEC_ALLOWLIST: set[tuple[str, int]] = {
    # (path-relative-to-repo, line-number) entries permitted to use exec/eval/compile.
    # MUST be reviewed by a CODEOWNER. Empty for now.
}


class Finding:
    __slots__ = ("path", "lineno", "rule", "message")

    def __init__(self, path: Path, lineno: int, rule: str, message: str) -> None:
        self.path = path
        self.lineno = lineno
        self.rule = rule
        self.message = message

    def __str__(self) -> str:
        rel = self.path.relative_to(REPO_ROOT)
        return f"{rel}:{self.lineno}: [{self.rule}] {self.message}"


def _is_in_agent_path(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT).as_posix()
    return any(rel.startswith(p + "/") for p in AGENT_PATHS)


def _is_protected_path_literal(s: str) -> bool:
    return any(p in s for p in PROTECTED_PATHS)


def _check_call(call: ast.Call, path: Path, findings: list[Finding]) -> None:
    rel = path.relative_to(REPO_ROOT).as_posix()
    allow_key = (rel, call.lineno)

    func = call.func
    # Direct name calls: exec(), eval(), compile()
    if isinstance(func, ast.Name) and func.id in {"exec", "eval", "compile"}:
        if allow_key in EXEC_ALLOWLIST:
            return
        findings.append(
            Finding(
                path,
                call.lineno,
                "no-exec",
                f"forbidden call to `{func.id}(...)` — agents must not self-rewrite. "
                "If genuinely required, add an explicit allow-list entry in "
                "tools/lint_no_self_rewrite.py and get CODEOWNERS approval.",
            )
        )
        return

    # importlib.import_module(non-literal)
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "import_module"
        and isinstance(func.value, ast.Name)
        and func.value.id == "importlib"
    ):
        if _is_in_agent_path(path):
            if not (call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)):
                findings.append(
                    Finding(
                        path,
                        call.lineno,
                        "no-dynamic-import",
                        "dynamic `importlib.import_module(<non-literal>)` is forbidden in "
                        "agent paths. Plugin loading must go through the audited bootstrap.",
                    )
                )

    # write to a protected path: open("netcortex/policy/...", "w").write(...) style.
    if (
        isinstance(func, ast.Attribute)
        and func.attr in {"write", "write_text", "write_bytes"}
        and _is_in_agent_path(path)
    ):
        # Heuristic: look for string literals in the call chain that look like protected paths.
        for node in ast.walk(call):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _is_protected_path_literal(node.value):
                    findings.append(
                        Finding(
                            path,
                            call.lineno,
                            "no-write-protected",
                            f"agent path is writing to a protected location: {node.value!r}. "
                            "Policy and contracts evolve only via reviewed PRs or the plasticity "
                            "approval flow.",
                        )
                    )


def scan_file(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return findings
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        # Defer syntax errors to mypy/ruff. The lint itself should not crash.
        sys.stderr.write(f"warn: skipping {path}: {exc}\n")
        return findings
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            _check_call(node, path, findings)
    return findings


def main() -> int:
    findings: list[Finding] = []
    for root_rel in SCAN_ROOTS:
        root = REPO_ROOT / root_rel
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            findings.extend(scan_file(path))

    if not findings:
        print("lint_no_self_rewrite: ok")
        return 0

    for f in findings:
        print(str(f))
    print(f"\nlint_no_self_rewrite: {len(findings)} violation(s)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
