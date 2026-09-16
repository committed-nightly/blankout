"""Reading workflow files into something the checks can ask questions of.

The one thing to know before touching this file, and it is the same thing in
every tool in this family: in YAML 1.1, which is what PyYAML implements, the
bare word `on` is a **boolean**. The key every workflow file on GitHub starts
with does not come back as `"on"`, it comes back as `True`.

The rest is the bits of a workflow that bear on where an output comes from:
which step has which `id`, what its `run:` script is, which shell that script
runs under, and what a job promised in its `outputs:` block.

Two things here exist only to stop the checks being wrong about jobs they
cannot see into. A job with `uses:` has no steps of its own — its outputs come
from the called workflow's `on.workflow_call.outputs`, which is readable when
the call is a path in this repository and not otherwise. A step with `uses:`
is the same shape of problem one level down: a local action's `action.yml`
says what it declares, a published one would need the network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

WORKFLOW_DIR = ".github/workflows"
WORKFLOW_SUFFIXES = (".yml", ".yaml")

#: `runs-on` label prefixes whose default shell is pwsh rather than bash.
#: GitHub picks the shell from the runner when a step does not say.
WINDOWS_PREFIXES = ("windows", "win-")


class WorkflowError(ValueError):
    """A file that could not be read as a workflow."""


@dataclass
class Step:
    """One step of one job."""

    #: Zero based, as GitHub counts them in `steps[N]`.
    index: int
    id: str | None
    name: str | None
    run: str | None
    uses: str | None
    shell: str | None
    #: Outputs the step declares, when that is knowable: a local action's
    #: `action.yml`. None means nobody knows, which is not the same as none.
    declared_outputs: set[str] | None = None
    #: Why `declared_outputs` is None, for the report.
    undeclarable: str | None = None

    @property
    def label(self) -> str:
        if self.id:
            return self.id
        return self.name or (self.uses or "run")


@dataclass
class Job:
    """One job, and the outputs it promises the jobs downstream of it."""

    id: str
    name: str | None
    runs_on: Any
    needs: list[str]
    #: `outputs:` as written: output name -> the expression producing it.
    outputs: dict[str, str]
    steps: list[Step]
    #: Set when the job is a call to a reusable workflow.
    uses: str | None = None
    #: Output names the called workflow declares, when readable.
    called_outputs: set[str] | None = None
    if_: str | None = None
    #: The job body exactly as written, for walking in search of expressions.
    raw: dict = field(default_factory=dict)

    @property
    def windows(self) -> bool:
        labels = self.runs_on
        if isinstance(labels, str):
            labels = [labels]
        elif isinstance(labels, dict):
            labels = labels.get("labels") or []
        elif not isinstance(labels, list):
            return False
        return any(
            isinstance(x, str) and x.lower().startswith(WINDOWS_PREFIXES) for x in labels
        )


@dataclass
class Workflow:
    """One workflow file."""

    path: str
    name: str
    jobs: dict[str, Job] = field(default_factory=dict)


def is_workflow_path(path: str) -> bool:
    return path.endswith(WORKFLOW_SUFFIXES)


def find_workflows(root: str) -> list[str]:
    """Every workflow file under `root`, sorted, by path relative to it."""
    directory = os.path.join(root, WORKFLOW_DIR)
    if not os.path.isdir(directory):
        return []
    found = []
    for entry in sorted(os.listdir(directory)):
        full = os.path.join(directory, entry)
        if os.path.isfile(full) and is_workflow_path(entry):
            found.append(os.path.join(WORKFLOW_DIR, entry))
    return found


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _shell_for(step_shell: Any, job_defaults: Any, wf_defaults: Any, windows: bool) -> str:
    """The shell a step's script actually runs under.

    A step's own `shell:` wins, then the job's `defaults.run.shell`, then the
    workflow's, then the runner's default — which is bash everywhere except
    Windows, where it is pwsh. Getting that last one wrong would mean reading
    `"key=$value" >> $env:GITHUB_OUTPUT` as bash and drawing conclusions.
    """
    for candidate in (step_shell, job_defaults, wf_defaults):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return "pwsh" if windows else "bash"


def _defaults_shell(block: Any) -> Any:
    if isinstance(block, dict):
        run = block.get("run")
        if isinstance(run, dict):
            return run.get("shell")
    return None


def _local_action_outputs(root: str, uses: str) -> tuple[set[str] | None, str | None]:
    """The outputs a local action declares, or why they are not knowable."""
    if not uses.startswith("./"):
        return None, f"`{uses}` is a published action, and reading it needs the network"
    base = os.path.normpath(os.path.join(root, uses[2:]))
    for filename in ("action.yml", "action.yaml"):
        candidate = os.path.join(base, filename)
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding="utf-8") as handle:
                    doc = yaml.safe_load(handle)
            except (OSError, yaml.YAMLError) as exc:
                return None, f"`{uses}` has an action file that would not parse: {exc}"
            outputs = (doc or {}).get("outputs")
            if isinstance(outputs, dict):
                return {str(k) for k in outputs}, None
            return set(), None
    return None, f"`{uses}` points at no action file in this repository"


def _called_workflow_outputs(root: str, uses: str) -> set[str] | None:
    """Outputs declared by a reusable workflow called by path."""
    if not uses.startswith("./"):
        return None
    candidate = os.path.normpath(os.path.join(root, uses[2:].split("@")[0]))
    if not os.path.isfile(candidate):
        return None
    try:
        with open(candidate, encoding="utf-8") as handle:
            doc = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError):
        return None
    triggers = (doc or {}).get(True, (doc or {}).get("on"))
    if not isinstance(triggers, dict):
        return None
    call = triggers.get("workflow_call")
    if not isinstance(call, dict):
        return None
    outputs = call.get("outputs")
    if isinstance(outputs, dict):
        return {str(k) for k in outputs}
    return set()


def load(root: str, relative_path: str) -> Workflow:
    """Read one workflow file. Raises `WorkflowError` on anything unreadable."""
    full = os.path.join(root, relative_path)
    try:
        with open(full, encoding="utf-8") as handle:
            doc = yaml.safe_load(handle)
    except OSError as exc:
        raise WorkflowError(f"{relative_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise WorkflowError(f"{relative_path}: not valid YAML: {exc}") from exc

    if doc is None:
        raise WorkflowError(f"{relative_path}: empty file")
    if not isinstance(doc, dict):
        raise WorkflowError(f"{relative_path}: top level is not a mapping")

    workflow = Workflow(path=relative_path, name=str(doc.get("name") or relative_path))
    wf_shell = _defaults_shell(doc.get("defaults"))

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return workflow

    for job_id, body in jobs.items():
        if not isinstance(body, dict):
            continue
        outputs = body.get("outputs")
        job = Job(
            id=str(job_id),
            name=body.get("name") if isinstance(body.get("name"), str) else None,
            runs_on=body.get("runs-on"),
            needs=_as_list(body.get("needs")),
            outputs={
                str(k): v if isinstance(v, str) else ""
                for k, v in (outputs or {}).items()
            }
            if isinstance(outputs, dict)
            else {},
            steps=[],
            uses=body.get("uses") if isinstance(body.get("uses"), str) else None,
            if_=body.get("if") if isinstance(body.get("if"), str) else None,
            raw=body,
        )
        if job.uses:
            job.called_outputs = _called_workflow_outputs(root, job.uses)

        job_shell = _defaults_shell(body.get("defaults"))
        steps = body.get("steps")
        if isinstance(steps, list):
            for index, raw in enumerate(steps):
                if not isinstance(raw, dict):
                    continue
                uses = raw.get("uses") if isinstance(raw.get("uses"), str) else None
                run = raw.get("run") if isinstance(raw.get("run"), str) else None
                step = Step(
                    index=index,
                    id=str(raw["id"]) if raw.get("id") is not None else None,
                    name=raw.get("name") if isinstance(raw.get("name"), str) else None,
                    run=run,
                    uses=uses,
                    shell=_shell_for(raw.get("shell"), job_shell, wf_shell, job.windows),
                )
                if uses is not None:
                    step.declared_outputs, step.undeclarable = _local_action_outputs(
                        root, uses
                    )
                elif run is None:
                    step.undeclarable = "the step neither runs a script nor uses an action"
                job.steps.append(step)

        workflow.jobs[job.id] = job

    return workflow
