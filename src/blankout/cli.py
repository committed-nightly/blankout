"""Command line entry point.

Exit codes, because the main use for this is a CI gate:

    0  every output reference in this repository resolves to something
    1  at least one of them is the empty string on every run
    2  nothing was looked at

2 is deliberately not 1 and very deliberately not 0. No workflow directory, a
file that is not YAML, a path that does not exist: in all of those nobody
looked, and a tool whose whole subject is silent failure reporting its own
silent failure as a green tick would be a poor joke.

A repository whose workflows read no step outputs is a 0. Plenty of good
workflows never pass a value between two steps.
"""

from __future__ import annotations

import argparse
import json
import sys

from .core import Report, scan
from .workflows import WORKFLOW_DIR, WorkflowError, find_workflows, load

EXIT_OK = 0
EXIT_FOUND = 1
EXIT_ERROR = 2

_HEADLINE = {
    "never-writes": "is always empty: nothing writes it",
    "wrong-key": "is always empty: that key is never written",
    "not-declared": "is always empty: the action declares no such output",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blankout",
        description=(
            "Find the GitHub Actions step outputs that are always empty: the "
            "reference is spelled right, the step is really there, and the "
            "value is never written."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="the repository to check (default: the current directory)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="the same findings, for piping somewhere",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="findings only; do not list what could not be decided",
    )
    return parser


def _text(report: Report, quiet: bool) -> list[str]:
    lines = []
    for finding in report.findings:
        lines.append(f"{finding.path}: {finding.location}")
        lines.append(f"  {finding.expression} {_HEADLINE[finding.kind]}")
        lines.append(f"  {finding.detail}")
        for consequence in finding.consequences:
            lines.append(f"  -> {consequence}")
        lines.append("")
    if not quiet:
        for undecided in report.undecided:
            lines.append(
                f"not checked: {undecided.path}: {undecided.location} "
                f"({undecided.expression}) — {undecided.reason}"
            )
    if not report.findings and not lines:
        lines.append("Every step output that is read is also written.")
    elif not report.findings:
        lines.insert(0, "Every step output that is read is also written.")
        lines.insert(1, "")
    return lines


def _json(report: Report) -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "kind": f.kind,
                    "path": f.path,
                    "job": f.job,
                    "location": f.location,
                    "expression": f.expression,
                    "detail": f.detail,
                    "consequences": f.consequences,
                }
                for f in report.findings
            ],
            "undecided": [
                {
                    "path": u.path,
                    "job": u.job,
                    "location": u.location,
                    "expression": u.expression,
                    "reason": u.reason,
                }
                for u in report.undecided
            ],
        },
        indent=2,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.path

    paths = find_workflows(root)
    if not paths:
        print(
            f"blankout: no workflow files in {root}/{WORKFLOW_DIR}", file=sys.stderr
        )
        return EXIT_ERROR

    workflows = []
    for path in paths:
        try:
            workflows.append(load(root, path))
        except WorkflowError as exc:
            print(f"blankout: {exc}", file=sys.stderr)
            return EXIT_ERROR

    report = scan(workflows)

    if args.json:
        print(_json(report))
    else:
        print("\n".join(_text(report, args.quiet)).rstrip())

    return EXIT_FOUND if report.findings else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
