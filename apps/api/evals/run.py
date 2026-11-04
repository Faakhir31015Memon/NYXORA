"""CLI: python -m evals.run [--model tag] [--real] [--out report.md]

Defaults to the deterministic mock adapter, because CI has no Ollama and a
green mock run proves the pipeline is wired, not that a model is clever. Pass
--real to measure against the installed model; the report always states which
mode produced the numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import Settings
from app.core.orchestrator import RunRequest
from app.deps import build_container
from evals.suite import CASES, EvalCase


@dataclass
class Result:
    case: EvalCase
    passed: bool
    latency_ms: float
    state: str
    detail: str


async def _one(case: EvalCase, root: Path, preferred_model: str | None) -> Result:
    workspace = root / case.id / "workspace"
    settings = Settings(
        NYXORA_DB_PATH=str(root / case.id / "evals.db"),
        NYXORA_WORKSPACE_ROOT=str(workspace),
        NYXORA_API_TOKEN="evals-token",
        PRIVACY_LEVEL="local_only",
    )
    workspace.mkdir(parents=True, exist_ok=True)
    container = build_container(settings, force_mock=True)
    started = time.perf_counter()
    try:
        outcome = await container.orchestrator.run(
            RunRequest(message=case.prompt, user_preferred_model=preferred_model)
        )
        latency = (time.perf_counter() - started) * 1000
    except Exception as exc:  # noqa: BLE001 - a crash is a failed case, not a stack dump
        return Result(case, False, 0.0, "crashed", f"{type(exc).__name__}: {exc}")
    finally:
        container.engine.dispose()  # Windows will not delete an open SQLite file
    try:
        passed = bool(case.check(outcome, workspace))
    except Exception as exc:  # noqa: BLE001
        return Result(
            case, False, latency, outcome.state.value, f"check raised {type(exc).__name__}"
        )
    return Result(case, passed, latency, outcome.state.value, outcome.honest_summary)


def _markdown(results: list[Result], *, mode: str, model: str | None) -> str:
    passed = sum(1 for r in results if r.passed)
    lines = [
        "# NYXORA evaluation report",
        "",
        f"Generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"Mode: **{mode}**",
        f"Model requested: `{model or 'router decides'}`",
        f"Pass: {passed}/{len(results)}",
        "",
        "> Internal measurement only. These are pipeline checks against whichever",
        "> model happened to be installed. They are not capability claims and must",
        "> not be quoted as benchmarks or marketing numbers.",
        "",
        "| case | kind | pass | state | latency ms | note |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.case.id} | {r.case.kind} | {'yes' if r.passed else 'NO'} "
            f"| {r.state} | {r.latency_ms:.0f} | {r.detail[:60]} |"
        )
    if mode == "mock":
        lines += [
            "",
            "## What this mode proves, and what it does not",
            "",
            "The mock adapter returns deterministic text. Passing cases mean routing,",
            "permission gating, safety pre-checks and verification fire in the right",
            "order. They say nothing about answer quality - re-run with Ollama reachable",
            "and --real for that.",
        ]
    return "\n".join(lines) + "\n"


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="evals.run", description=__doc__)
    parser.add_argument("--model", default=None, help="prefer this model tag")
    parser.add_argument(
        "--real",
        action="store_true",
        help="label the run as a real-model measurement (still needs Ollama)",
    )
    parser.add_argument("--out", default=None, help="write markdown to this path")
    return parser.parse_args(argv)


async def _run(argv: list[str]) -> dict[str, object]:
    args = _parse(argv)
    results: list[Result] = []
    with TemporaryDirectory(prefix="nyxora-evals-") as tmp:
        root = Path(tmp)
        for case in CASES:
            results.append(await _one(case, root, args.model))
    report = _markdown(results, mode="real" if args.real else "mock", model=args.model)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(report)
    return {
        "total": len(results),
        "failed": [r.case.id for r in results if not r.passed],
        "report": report,
    }


def main() -> int:
    summary = asyncio.run(_run(sys.argv[1:]))
    failed = summary["failed"]
    assert isinstance(failed, list)
    if failed:
        print(f"failed: {', '.join(str(f) for f in failed)}", file=sys.stderr)
        return 1
    print(f"{summary['total']} cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
