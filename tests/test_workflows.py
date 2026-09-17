"""Loading, and the two shapes of job this must not be confident about."""

from __future__ import annotations

import pytest

from blankout.core import scan
from blankout.workflows import WorkflowError, find_workflows, load


def load_all(repo):
    root = str(repo)
    return [load(root, path) for path in find_workflows(root)]


def test_on_is_a_boolean_key_and_the_file_still_loads(repo):
    """YAML 1.1: the bare word `on` parses as True, not the string "on"."""
    import yaml

    path = repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: echo hi
        """,
    )
    doc = yaml.safe_load(path.read_text())
    assert "on" not in doc and True in doc  # the gotcha, asserted directly

    workflow = load_all(repo)[0]
    assert list(workflow.jobs) == ["build"]


def test_a_reusable_workflow_call_read_by_path(repo):
    repo.write(
        "reusable.yml",
        """
        name: Reusable
        on:
          workflow_call:
            outputs:
              version:
                description: the version
                value: ${{ jobs.x.outputs.version }}
        jobs:
          x:
            runs-on: ubuntu-latest
            steps:
              - run: echo hi
        """,
    )
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          call:
            uses: ./.github/workflows/reusable.yml
        """,
    )
    workflows = load_all(repo)
    caller = next(w for w in workflows if w.path.endswith("ci.yml"))
    assert caller.jobs["call"].called_outputs == {"version"}


def test_a_remote_reusable_workflow_is_unknown_not_empty(repo):
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          call:
            uses: other/repo/.github/workflows/build.yml@main
        """,
    )
    assert load_all(repo)[0].jobs["call"].called_outputs is None


def test_a_job_that_calls_a_workflow_reports_nothing_about_its_steps(repo):
    """It has no steps, so no step output reference can be blamed on it."""
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          call:
            uses: other/repo/.github/workflows/build.yml@main
          after:
            needs: call
            if: needs.call.outputs.version != ''
            runs-on: ubuntu-latest
            steps:
              - run: echo hi
        """,
    )
    report = scan(load_all(repo))
    assert report.findings == []


@pytest.mark.parametrize(
    "labels,windows",
    [
        ("ubuntu-latest", False),
        ("windows-latest", True),
        ("[self-hosted, windows, x64]", True),
        ("[self-hosted, linux]", False),
        ("${{ matrix.os }}", False),
    ],
)
def test_which_runners_default_to_pwsh(repo, labels, windows):
    repo.write(
        "ci.yml",
        f"""
        name: CI
        on: push
        jobs:
          build:
            runs-on: {labels}
            steps:
              - run: echo hi
        """,
    )
    assert load_all(repo)[0].jobs["build"].windows is windows


def test_a_directory_with_no_workflows_finds_none(tmp_path):
    assert find_workflows(str(tmp_path)) == []


def test_a_non_mapping_top_level_is_an_error(repo):
    repo.write("ci.yml", "- one\n- two\n")
    with pytest.raises(WorkflowError, match="not a mapping"):
        load_all(repo)


def test_files_that_are_not_yaml_are_ignored(repo):
    repo.write("ci.yml", "name: CI\non: push\njobs: {}\n")
    (repo.root / ".github" / "workflows" / "README.md").write_text("notes")
    assert find_workflows(str(repo)) == [".github/workflows/ci.yml"]
