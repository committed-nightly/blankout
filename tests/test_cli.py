"""Exit codes and output, which are the whole interface when this runs in CI."""

from __future__ import annotations

import json

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
