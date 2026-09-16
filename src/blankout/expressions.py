"""Finding output references inside `${{ }}`, and where in the file they sit.

Everything in a job body is walked rather than a list of the places outputs
are usually read, because they are read in more places than a list would ever
finish: `if:`, `with:`, `env:`, `run:`, a step `name:`, a matrix value, the
`outputs:` block itself. Walking means the answer to "did you look in X" is
yes for every X.

The paths that come back — `jobs.build.steps[2].if` — are what the report
prints, so they are written the way you would type them into the file.
"""

from __future__ import annotations

import re
from typing import Any, Iterator

#: `${{ ... }}`, possibly spanning lines, which multi-line `if:` blocks do.
INTERPOLATION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)

#: `steps.build.outputs.sha`, and the `steps.build.outputs['sha']` spelling
#: that people reach for when a key has a dash in it.
STEP_OUTPUT = re.compile(
    r"\bsteps\s*\.\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\.\s*outputs\s*"
    r"(?:\.\s*([A-Za-z_][A-Za-z0-9_-]*)|\[\s*'([^']*)'\s*\]|\[\s*\"([^\"]*)\"\s*\])"
)

#: The same shape one level up, between jobs.
NEEDS_OUTPUT = re.compile(
    r"\bneeds\s*\.\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\.\s*outputs\s*"
    r"(?:\.\s*([A-Za-z_][A-Za-z0-9_-]*)|\[\s*'([^']*)'\s*\]|\[\s*\"([^\"]*)\"\s*\])"
)


def walk_strings(node: Any, prefix: str) -> Iterator[tuple[str, str]]:
    """Every string in `node`, with a dotted path describing where it was."""
    if isinstance(node, str):
        yield prefix, node
    elif isinstance(node, dict):
        for key, value in node.items():
            # `on:` comes back as the boolean True; say so rather than `True`.
            name = "on" if key is True else str(key)
            yield from walk_strings(value, f"{prefix}.{name}" if prefix else name)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk_strings(value, f"{prefix}[{index}]")


def _pick(match: re.Match) -> tuple[str, str]:
    """The (owner, key) out of a match, whichever spelling it used."""
    owner = match.group(1)
    key = match.group(2) or match.group(3) or match.group(4) or ""
    return owner, key


def is_condition(location: str) -> bool:
    """Whether a location is an `if:`, where the braces are optional.

    `if: needs.build.outputs.ok == 'true'` has no `${{ }}` anywhere in it and
    is an expression regardless — GitHub evaluates the whole value. That is
    also how nearly everyone writes them, so a scanner that only looked inside
    braces would miss the conditions, which are the entire reason to care.
    """
    return location == "if" or location.endswith(".if")


def step_outputs(text: str, bare: bool = False) -> list[tuple[str, str, str]]:
    """Every `steps.<id>.outputs.<key>` read as an expression.

    Returns (step id, key, the reference as written). With `bare` false, only
    matches inside `${{ }}` count, so `steps.x.outputs.y` in a `run:` log line
    or a comment stays prose. With `bare` true the whole string is expression.
    """
    return _collect(text, STEP_OUTPUT, bare)


def needs_outputs(text: str, bare: bool = False) -> list[tuple[str, str, str]]:
    """Every `needs.<job>.outputs.<name>` read as an expression."""
    return _collect(text, NEEDS_OUTPUT, bare)


def _collect(text: str, pattern: re.Pattern, bare: bool) -> list[tuple[str, str, str]]:
    found = []
    if bare and "${{" not in text:
        regions = [text]
    else:
        regions = [m.group(1) for m in INTERPOLATION.finditer(text)]
    for region in regions:
        for match in pattern.finditer(region):
            owner, key = _pick(match)
            if key:
                found.append((owner, key, match.group(0).strip()))
    return found


def sole_expression(value: str) -> str | None:
    """The inside of `value`, when `value` is one interpolation and nothing else.

    `${{ steps.a.outputs.b }}` gives `steps.a.outputs.b`. `v${{ ... }}` gives
    None, because such a value is never empty however empty the output is —
    which is the difference between a job output that breaks a downstream
    `if:` and one that merely looks wrong.
    """
    stripped = value.strip()
    match = INTERPOLATION.fullmatch(stripped)
    return match.group(1).strip() if match else None
