"""Exit codes and output, which are the whole interface when this runs in CI."""

from __future__ import annotations

import json
import re

import pytest

from blankout.cli import EXIT_ERROR, EXIT_FOUND, EXIT_OK, main

CLEAN = """
name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      v: ${{ steps.m.outputs.version }}
    steps:
      - id: m
        run: echo "version=1" >> $GITHUB_OUTPUT
"""

BROKEN = """
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
    needs: build
    if: needs.build.outputs.release == 'true'
    runs-on: ubuntu-latest
    steps:
      - run: echo publishing
"""


def test_a_clean_repository_is_zero(repo, capsys):
    repo.write("ci.yml", CLEAN)
    assert main([str(repo)]) == EXIT_OK
    assert "is also written" in capsys.readouterr().out


def test_a_finding_is_one(repo, capsys):
    repo.write("ci.yml", BROKEN)
    assert main([str(repo)]) == EXIT_FOUND
    out = capsys.readouterr().out
    assert "steps.check.outputs.release is always empty" in out
    assert "job `publish` is gated on it" in out


def test_no_workflow_directory_is_two_not_zero(tmp_path, capsys):
    """A tool about silent failure must not fail silently."""
    assert main([str(tmp_path)]) == EXIT_ERROR
    assert "no workflow files" in capsys.readouterr().err


def test_a_path_that_does_not_exist_is_two(capsys):
    assert main(["/nonexistent/place"]) == EXIT_ERROR


def test_unparseable_yaml_is_two_not_zero(repo, capsys):
    repo.write("ci.yml", "name: CI\non: push\njobs: [[[")
    assert main([str(repo)]) == EXIT_ERROR
    assert "not valid YAML" in capsys.readouterr().err


def test_an_empty_workflow_file_is_two(repo, capsys):
    repo.write("ci.yml", "")
    assert main([str(repo)]) == EXIT_ERROR
    assert "empty file" in capsys.readouterr().err


def test_workflows_that_read_no_outputs_are_zero(repo, capsys):
    repo.write(
        "ci.yml",
        """
        name: CI
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: make test
        """,
    )
    assert main([str(repo)]) == EXIT_OK


def test_json_carries_the_consequences(repo, capsys):
    repo.write("ci.yml", BROKEN)
    assert main([str(repo), "--json"]) == EXIT_FOUND
    payload = json.loads(capsys.readouterr().out)
    assert payload["findings"][0]["kind"] == "never-writes"
    assert payload["findings"][0]["consequences"]
    assert payload["undecided"] == []


def test_quiet_drops_the_undecided_lines(repo, capsys):
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
    assert main([str(repo)]) == EXIT_OK
    assert "not checked:" in capsys.readouterr().out
    assert main([str(repo), "--quiet"]) == EXIT_OK
    assert "not checked:" not in capsys.readouterr().out


@pytest.mark.parametrize("suffix", [".yml", ".yaml"])
def test_both_suffixes_are_read(repo, suffix):
    repo.write(f"ci{suffix}", BROKEN)
    assert main([str(repo)]) == EXIT_FOUND


NO_OUTPUTS = """
name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""

ALL_RESOLVE = """
name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      v: ${{ steps.m.outputs.version }}
    steps:
      - id: m
        run: echo "version=1" >> $GITHUB_OUTPUT
      - run: echo ${{ steps.m.outputs.version }}
"""


class TestTheDenominator:
    """logbook#57: a clean sweep and an empty one used to be the same output.

    `blankout` returned the same text, the same JSON and the same exit 0 for a
    repository whose every output reference resolves and a repository with no
    `outputs:` anywhere. Both are genuinely clean, so the exit code is right
    and is left alone; what was missing was any way to tell which one you got.
    """

    def test_a_clean_sweep_and_an_empty_one_differ(self, repo, capsys):
        repo.write("ci.yml", ALL_RESOLVE)
        assert main([str(repo)]) == EXIT_OK
        resolved = capsys.readouterr().out

        repo.write("ci.yml", NO_OUTPUTS)
        assert main([str(repo)]) == EXIT_OK
        empty = capsys.readouterr().out

        # The bug, stated as the assertion it failed.
        assert resolved != empty
        assert "2 output references" in resolved
        assert "0 output references" in empty

    def test_the_count_is_printed_on_a_green_run(self, repo, capsys):
        repo.write("ci.yml", ALL_RESOLVE)
        assert main([str(repo)]) == EXIT_OK
        out = capsys.readouterr().out
        assert "Every step output that is read is also written." in out
        assert "1 workflow checked, 2 output references, 0 findings" in out

    def test_singular_and_plural(self, repo, capsys):
        repo.write("ci.yml", BROKEN)
        assert main([str(repo)]) == EXIT_FOUND
        # One of each, so every noun on the line is singular.
        assert "1 workflow checked, 1 output reference, 1 finding" in capsys.readouterr().out

        repo.write("two.yml", BROKEN)
        assert main([str(repo)]) == EXIT_FOUND
        assert "2 workflows checked, 2 output references, 2 findings" in capsys.readouterr().out

    def test_a_reference_is_counted_once_per_place_it_is_written(self, repo, capsys):
        """The documented semantics: reference sites, not distinct keys."""
        repo.write(
            "ci.yml",
            """
            name: CI
            on: push
            jobs:
              build:
                runs-on: ubuntu-latest
                outputs:
                  a: ${{ steps.m.outputs.version }}
                  b: ${{ steps.m.outputs.version }}
                steps:
                  - id: m
                    run: echo "version=1" >> $GITHUB_OUTPUT
            """,
        )
        assert main([str(repo)]) == EXIT_OK
        assert "2 output references" in capsys.readouterr().out

    def test_json_carries_the_counts(self, repo, capsys):
        repo.write("ci.yml", NO_OUTPUTS)
        assert main([str(repo), "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "workflows": 1,
            "references": 0,
            "findings": [],
            "undecided": [],
        }

    def test_quiet_keeps_the_count_and_drops_the_undecided(self, repo, capsys):
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
        assert main([str(repo), "--quiet"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "not checked:" not in out
        # The count is not noise -- it is the one line a CI gate reads.
        assert "1 workflow checked, 1 output reference, 0 findings" in out

    def test_the_count_is_the_denominator_of_the_two_lists(self, repo, capsys):
        """findings + undecided can never exceed the references seen."""
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
                  sha: ${{ steps.co.outputs.commit }}
                steps:
                  - id: check
                    run: echo checking
                  - id: co
                    uses: actions/checkout@v4
            """,
        )
        assert main([str(repo), "--json"]) == EXIT_FOUND
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["findings"]) == 1
        assert len(payload["undecided"]) == 1
        assert payload["references"] == 2

    def test_the_summary_is_readable_by_the_pattern_stockbox_would_use(self, repo, capsys):
        """The point of the count: `stockbox`'s `exercised` floor reads it.

        A corpus run over vendored trees is mostly repositories with no step
        outputs at all, and the floor exists to notice that. It reads the
        number out of this line with a regex, so the line has to stay
        machine-readable and has to be present on a run with no findings.
        """
        repo.write("ci.yml", NO_OUTPUTS)
        assert main([str(repo)]) == EXIT_OK
        match = re.search(r"([0-9]+) output references?", capsys.readouterr().out)
        assert match is not None
        assert match.group(1) == "0"
