"""The checks, end to end from a workflow file on disk."""

from __future__ import annotations

from blankout.core import NEVER_WRITES, NOT_DECLARED, WRONG_KEY, scan
from blankout.workflows import find_workflows, load


def run(repo):
    root = str(repo)
    return scan([load(root, path) for path in find_workflows(root)])


def test_the_step_exists_and_writes_nothing(repo):
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            outputs:
              deploy: ${{ steps.check.outputs.deploy }}
            steps:
              - id: check
                run: echo "checking"
        """,
    )
    report = run(repo)
    assert [f.kind for f in report.findings] == [NEVER_WRITES]
    assert report.findings[0].location == "jobs.build.outputs.deploy"
    assert report.findings[0].expression == "steps.check.outputs.deploy"


def test_a_typo_in_the_key(repo):
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            outputs:
              v: ${{ steps.m.outputs.verison }}
            steps:
              - id: m
                run: echo "version=1.2" >> $GITHUB_OUTPUT
        """,
    )
    report = run(repo)
    assert [f.kind for f in report.findings] == [WRONG_KEY]
    assert "writes version" in report.findings[0].detail


def test_the_key_that_is_written_is_not_reported(repo):
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            outputs:
              v: ${{ steps.m.outputs.version }}
            steps:
              - id: m
                run: echo "version=1.2" >> $GITHUB_OUTPUT
        """,
    )
    report = run(repo)
    assert report.findings == []
    assert report.ok


def test_the_gated_job_is_named(repo):
    """The whole point: an empty output silently skips the job it guards."""
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            outputs:
              release: ${{ steps.check.outputs.release }}
            steps:
              - id: check
                run: echo "checking"
          publish:
            needs: build
            if: needs.build.outputs.release == 'true'
            runs-on: ubuntu-latest
            steps:
              - run: echo publishing
        """,
    )
    report = run(repo)
    assert len(report.findings) == 1
    consequences = report.findings[0].consequences
    assert len(consequences) == 1
    assert "`publish` is gated on it" in consequences[0]
    assert "skipped on every run" in consequences[0]


def test_a_prefixed_output_is_not_empty_so_nothing_is_gated(repo):
    """`v${{ ... }}` is the string `v`, which no `if:` treats as empty."""
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            outputs:
              tag: v${{ steps.check.outputs.version }}
            steps:
              - id: check
                run: echo "checking"
          publish:
            needs: build
            if: needs.build.outputs.tag != ''
            runs-on: ubuntu-latest
            steps:
              - run: echo publishing
        """,
    )
    report = run(repo)
    assert len(report.findings) == 1
    assert report.findings[0].consequences == []


def test_a_local_action_that_declares_no_such_output(repo):
    repo.action(
        ".github/actions/version",
        """
        name: Version
        description: works out a version
        outputs:
          version:
            description: the version
            value: ${{ steps.x.outputs.version }}
        runs:
          using: composite
          steps:
            - id: x
              shell: bash
              run: echo "version=1" >> $GITHUB_OUTPUT
        """,
    )
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            outputs:
              v: ${{ steps.m.outputs.tag }}
            steps:
              - id: m
                uses: ./.github/actions/version
        """,
    )
    report = run(repo)
    assert [f.kind for f in report.findings] == [NOT_DECLARED]
    assert "declares version" in report.findings[0].detail


def test_a_published_action_is_declined_not_guessed(repo):
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            outputs:
              sha: ${{ steps.co.outputs.commit }}
            steps:
              - id: co
                uses: actions/checkout@v4
        """,
    )
    report = run(repo)
    assert report.findings == []
    assert len(report.undecided) == 1
    assert "needs the network" in report.undecided[0].reason


def test_an_unknown_step_id_is_left_to_actionlint(repo):
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            outputs:
              v: ${{ steps.nosuch.outputs.version }}
            steps:
              - id: m
                run: echo "version=1" >> $GITHUB_OUTPUT
        """,
    )
    report = run(repo)
    assert report.findings == []
    assert report.undecided == []


def test_a_reference_in_a_step_condition(repo):
    """Read from an `if:`, which has no braces and is an expression anyway."""
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - id: check
                run: echo hello
              - if: steps.check.outputs.changed == 'true'
                run: echo changed
        """,
    )
    report = run(repo)
    assert [f.kind for f in report.findings] == [NEVER_WRITES]
    assert report.findings[0].location == "jobs.build.steps[1].if"


def test_a_reference_in_a_with_block(repo):
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - id: check
                run: echo hello
              - uses: actions/upload-artifact@v4
                with:
                  name: build-${{ steps.check.outputs.sha }}
        """,
    )
    report = run(repo)
    assert [f.location for f in report.findings] == ["jobs.build.steps[1].with.name"]


def test_the_same_text_in_a_run_script_is_prose(repo):
    """Outside `${{ }}`, `steps.x.outputs.y` is a string being printed."""
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - id: check
                run: echo hello
              - run: echo "set steps.check.outputs.sha to use this"
        """,
    )
    report = run(repo)
    assert report.findings == []


def test_a_windows_step_defaults_to_pwsh_and_is_declined(repo):
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: windows-latest
            outputs:
              v: ${{ steps.m.outputs.version }}
            steps:
              - id: m
                run: echo "version=1" >> $env:GITHUB_OUTPUT
        """,
    )
    report = run(repo)
    assert report.findings == []
    assert "pwsh" in report.undecided[0].reason


def test_a_windows_step_told_to_use_bash_is_read(repo):
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: windows-latest
            outputs:
              v: ${{ steps.m.outputs.version }}
            steps:
              - id: m
                shell: bash
                run: echo "version=1" >> $GITHUB_OUTPUT
        """,
    )
    report = run(repo)
    assert report.findings == []
    assert report.undecided == []


def test_a_job_default_shell_applies_to_its_steps(repo):
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: windows-latest
            defaults:
              run:
                shell: bash
            outputs:
              v: ${{ steps.m.outputs.version }}
            steps:
              - id: m
                run: echo "version=1" >> $GITHUB_OUTPUT
        """,
    )
    report = run(repo)
    assert report.findings == []
    assert report.undecided == []


def test_consequences_do_not_cross_an_undeclared_needs(repo):
    """`needs.build` is only readable by a job that declared the dependency."""
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            outputs:
              release: ${{ steps.check.outputs.release }}
            steps:
              - id: check
                run: echo checking
          publish:
            if: needs.build.outputs.release == 'true'
            runs-on: ubuntu-latest
            steps:
              - run: echo publishing
        """,
    )
    report = run(repo)
    assert len(report.findings) == 1
    assert report.findings[0].consequences == []


def test_a_bracket_subscript_is_the_same_reference(repo):
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            outputs:
              v: ${{ steps.m.outputs['is-release'] }}
            steps:
              - id: m
                run: echo "version=1" >> $GITHUB_OUTPUT
        """,
    )
    report = run(repo)
    assert [f.kind for f in report.findings] == [WRONG_KEY]


def test_a_caller_supplied_script_is_declined(repo):
    """next.js does this: `run:` is an expression, and the outputs are real."""
    repo.write(
        "reusable.yml",
        """
        name: Reusable
        on:
          workflow_call:
            inputs:
              afterBuild:
                type: string
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - id: after-build
                run: ${{ inputs.afterBuild }}
              - if: steps.after-build.outputs.passed_tests_file != ''
                run: echo saving
        """,
    )
    report = run(repo)
    assert report.findings == []
    assert "whatever the caller passes in" in report.undecided[0].reason


def test_an_interpolated_value_withholds_never_writes(repo):
    """A `${{ }}` is substituted in before bash runs and could carry a write."""
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            outputs:
              v: ${{ steps.m.outputs.version }}
            steps:
              - id: m
                run: echo "building ${{ github.event.head_commit.message }}"
        """,
    )
    report = run(repo)
    assert report.findings == []
    assert "substituted in before" in report.undecided[0].reason


def test_an_interpolated_value_does_not_withhold_wrong_key(repo):
    """That verdict rests on what the script does, not on what it omits."""
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            outputs:
              v: ${{ steps.m.outputs.verison }}
            steps:
              - id: m
                run: echo "version=${{ github.sha }}" >> $GITHUB_OUTPUT
        """,
    )
    report = run(repo)
    assert [f.kind for f in report.findings] == [WRONG_KEY]


def test_an_expression_after_a_pipe_is_still_a_command(repo):
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            outputs:
              v: ${{ steps.m.outputs.version }}
            steps:
              - id: m
                run: cat notes.txt | ${{ inputs.filter }}
        """,
    )
    report = run(repo)
    assert report.findings == []
    assert "caller passes in" in report.undecided[0].reason
