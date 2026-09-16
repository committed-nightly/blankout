"""The checks, and the part that matters: what the empty value goes on to do.

Every finding here is the same claim — **this expression is the empty string
on every run** — arrived at three ways:

    never-writes   the step exists, the reference is spelled right, and its
                   script never appends anything to $GITHUB_OUTPUT at all
    wrong-key      the script writes outputs, all of them nameable, and this
                   is not one of them
    not-declared   the step uses an action in this repository whose action.yml
                   declares no such output

The reason to build this rather than grep for it is the second half. An empty
string is not itself a failure; `if: needs.build.outputs.deploy == 'true'` is
false, the job it guards is skipped, the run is green, and nobody is told.
`_consequences` walks from an empty job output to whatever reads it and says
that out loud, because "always empty" is a fact and "your deploy job has not
run since March" is the thing you needed to know.

Anything that cannot be decided is not a finding. It goes in `undecided` with
the reason, and the reason is specific enough to act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .expressions import (
    is_condition,
    needs_outputs,
    sole_expression,
    step_outputs,
    walk_strings,
)
from .shell import scan_script
from .workflows import Job, Step, Workflow

NEVER_WRITES = "never-writes"
WRONG_KEY = "wrong-key"
NOT_DECLARED = "not-declared"


@dataclass
class Finding:
    """One expression that is the empty string on every run."""

    kind: str
    path: str
    job: str
    #: Where in the file, as you would type it: `jobs.build.outputs.version`.
    location: str
    #: The reference as written: `steps.check.outputs.deploy`.
    expression: str
    detail: str
    #: What the empty value goes on to break, in reading order.
    consequences: list[str] = field(default_factory=list)


@dataclass
class Undecided:
    """A reference this declined to rule on, and why."""

    path: str
    job: str
    location: str
    expression: str
    reason: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    undecided: list[Undecided] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


def _step_by_id(job: Job, step_id: str) -> Step | None:
    for step in job.steps:
        if step.id == step_id:
            return step
    return None


def _references(job: Job) -> list[tuple[str, str, str, str]]:
    """Every step-output reference in a job: (location, step id, key, as written)."""
    found = []
    for location, text in walk_strings(job.raw, f"jobs.{job.id}"):
        for step_id, key, written in step_outputs(text, bare=is_condition(location)):
            found.append((location, step_id, key, written))
    return found


def _verdict(job: Job, step_id: str, key: str) -> tuple[str | None, str]:
    """Rule on one reference. Returns (finding kind or None, detail or reason).

    A kind of None with a non-empty detail means undecided — say so and move
    on. A kind of None with an empty detail means the output is fine.
    """
    step = _step_by_id(job, step_id)
    if step is None:
        # actionlint reports this one, and reports it well. Two tools saying
        # the same thing about the same line is noise, so this says nothing.
        return None, ""

    where = f"step {step.index} of `{job.id}`"

    if step.uses is not None:
        if step.declared_outputs is None:
            return None, step.undeclarable or f"{where} uses an action this cannot read"
        if key in step.declared_outputs:
            return None, ""
        declared = ", ".join(sorted(step.declared_outputs)) or "nothing"
        return NOT_DECLARED, (
            f"{where} uses `{step.uses}`, whose action file declares {declared}"
        )

    if step.run is None:
        return None, step.undeclarable or f"{where} is neither a script nor an action"

    writes = scan_script(step.run, step.shell)
    if writes.opaque:
        return None, writes.opaque
    if key in writes.keys:
        return None, ""
    if writes.unknown:
        return None, f"{where} writes an output whose name this could not read"
    if not writes.mentions:
        if not writes.writes_nothing:
            return None, (
                f"{where} writes no outputs in the text of its script, but the "
                f"script contains a `${{{{ }}}}` that is substituted in before "
                f"bash runs, and could carry one"
            )
        return NEVER_WRITES, (
            f"{where} never writes to $GITHUB_OUTPUT, so it publishes no outputs"
        )
    written = ", ".join(sorted(writes.keys)) or "nothing"
    return WRONG_KEY, f"{where} writes {written}"


def _empty_job_outputs(job: Job, findings: list[Finding]) -> set[str]:
    """Job outputs that are empty because their whole value is an empty one.

    `${{ steps.a.outputs.b }}` inherits the emptiness. `v${{ ... }}` does not:
    it is still the string `v`, which is wrong but is not what breaks an `if:`.
    """
    broken = {f.expression for f in findings if f.location.startswith(f"jobs.{job.id}.outputs.")}
    empty = set()
    for name, value in job.outputs.items():
        inner = sole_expression(value)
        if inner is not None and inner in broken:
            empty.add(name)
    return empty


def _consequences(workflow: Workflow, producer: str, output: str) -> list[str]:
    """What reads `needs.<producer>.outputs.<output>` in this workflow.

    Scoped to the one file because that is where `needs` can reach. A reusable
    workflow's caller lives elsewhere and is not claimed about.
    """
    said = []
    for job in workflow.jobs.values():
        if producer not in job.needs:
            continue
        for location, text in walk_strings(job.raw, f"jobs.{job.id}"):
            for owner, key, _written in needs_outputs(text, bare=is_condition(location)):
                if owner != producer or key != output:
                    continue
                if location == f"jobs.{job.id}.if":
                    said.append(
                        f"job `{job.id}` is gated on it (`if: {text.strip()}`) and is "
                        f"skipped on every run"
                    )
                elif location.endswith(".if"):
                    said.append(f"`{location}` is gated on it and is skipped on every run")
                else:
                    said.append(f"`{location}` reads it and gets the empty string")
    return said


def scan(workflows: list[Workflow]) -> Report:
    """Run every check over every workflow."""
    report = Report()
    for workflow in workflows:
        file_findings: list[Finding] = []
        for job in workflow.jobs.values():
            for location, step_id, key, written in _references(job):
                kind, detail = _verdict(job, step_id, key)
                if kind is None:
                    if detail:
                        report.undecided.append(
                            Undecided(workflow.path, job.id, location, written, detail)
                        )
                    continue
                file_findings.append(
                    Finding(
                        kind=kind,
                        path=workflow.path,
                        job=job.id,
                        location=location,
                        expression=written,
                        detail=detail,
                    )
                )

        for job in workflow.jobs.values():
            for output in sorted(_empty_job_outputs(job, file_findings)):
                said = _consequences(workflow, job.id, output)
                if not said:
                    continue
                for finding in file_findings:
                    if finding.location == f"jobs.{job.id}.outputs.{output}":
                        finding.consequences = said
        report.findings.extend(file_findings)

    report.findings.sort(key=lambda f: (f.path, f.job, f.location))
    report.undecided.sort(key=lambda u: (u.path, u.job, u.location))
    return report
