"""The scanner, which is where being wrong would be embarrassing.

Two failure modes, and they are not equally bad. Missing a real always-empty
output costs the user nothing they did not already have. Reporting a working
step as broken costs them their trust in the tool, once, permanently. So the
table below is mostly the second kind: scripts that do write the key, in every
spelling anyone actually uses, each of which must come back decided and
containing that key.
"""

from __future__ import annotations

import pytest

from blankout.shell import POSIX_SHELLS, scan_script

#: (script, keys it provably writes) — for scripts that are fully decidable.
WRITES = [
    ('echo "version=1.2" >> $GITHUB_OUTPUT', {"version"}),
    ("echo version=1.2 >> $GITHUB_OUTPUT", {"version"}),
    ('echo "version=1.2" >> "$GITHUB_OUTPUT"', {"version"}),
    ('echo "version=1.2" >> ${GITHUB_OUTPUT}', {"version"}),
    ('echo "version=1.2" >>$GITHUB_OUTPUT', {"version"}),
    ('echo "version=$V" >> $GITHUB_OUTPUT', {"version"}),
    ('echo "version=${{ github.sha }}" >> $GITHUB_OUTPUT', {"version"}),
    ("printf 'version=%s\\n' \"$V\" >> $GITHUB_OUTPUT", {"version"}),
    ('echo "a=1" >> $GITHUB_OUTPUT\necho "b=2" >> $GITHUB_OUTPUT', {"a", "b"}),
    ('echo "a=1" >> $GITHUB_OUTPUT; echo "b=2" >> $GITHUB_OUTPUT', {"a", "b"}),
    ('echo "a=1" >> $GITHUB_OUTPUT && echo "b=2" >> $GITHUB_OUTPUT', {"a", "b"}),
    # A dash in the key, which GitHub allows and people use.
    ('echo "is-release=true" >> $GITHUB_OUTPUT', {"is-release"}),
    # Indented inside a conditional.
    (
        'if [ -f VERSION ]; then\n  echo "found=yes" >> $GITHUB_OUTPUT\nfi',
        {"found"},
    ),
    # A comment mentioning the file is not a write, but it is accounted for.
    ('echo "a=1" >> $GITHUB_OUTPUT  # writes to $GITHUB_OUTPUT', {"a"}),
    # The deprecated command still produces a real output.
    ('echo "::set-output name=version::1.2"', {"version"}),
    # Heredoc straight into the file.
    ('cat >> $GITHUB_OUTPUT <<EOF\na=1\nb=2\nEOF', {"a", "b"}),
    ("cat <<'EOF' >> \"$GITHUB_OUTPUT\"\na=1\nEOF", {"a"}),
    # The multi-line value protocol, written a line at a time.
    (
        'echo "notes<<EOF" >> $GITHUB_OUTPUT\n'
        'echo "line one" >> $GITHUB_OUTPUT\n'
        'echo "EOF" >> $GITHUB_OUTPUT\n'
        'echo "n=1" >> $GITHUB_OUTPUT',
        {"notes", "n"},
    ),
    # The same, inside a heredoc body.
    (
        "cat >> $GITHUB_OUTPUT <<EOF\nnotes<<BODY\nnot=a=key\nBODY\nn=1\nEOF",
        {"notes", "n"},
    ),
    # A leading assignment sets the environment; the command is what follows,
    # and it is still an `echo` whose key can be read.
    ('TZ=UTC echo "version=1.2" >> $GITHUB_OUTPUT', {"version"}),
    ('LC_ALL=C LANG=C echo "a=1" >> $GITHUB_OUTPUT', {"a"}),
    ('FOO="a b" echo "a=1" >> $GITHUB_OUTPUT', {"a"}),
    ('CI=true echo "::set-output name=version::1.2"', {"version"}),
    # An assignment on its own line runs nothing, and the echo after it is
    # read as normal.
    ('VERSION=1.2\necho "v=$VERSION" >> $GITHUB_OUTPUT', {"v"}),
    # A variable may be named after a command. Position decides which it is,
    # so this is an assignment to `env` and not a run of `env`.
    ('env=production echo "a=1" >> $GITHUB_OUTPUT', {"a"}),
]

#: Scripts that write nothing at all and can be said to write nothing.
WRITES_NOTHING = [
    'echo "building"',
    "grep -c x file.txt",
    'if [ -f VERSION ]; then echo "found"; fi',
    "git log --oneline -1\ncurl -sS https://example.com -o out.json",
    # A heredoc to somewhere else: its body is content, not commands.
    "cat > notes.txt <<EOF\necho a=1 >> $NOT_THE_FILE\nEOF",
]

#: Scripts that must come back undecided, with the reason each one is.
UNDECIDABLE = [
    # A computed key. Nothing can be said to be missing.
    'echo "$name=1" >> $GITHUB_OUTPUT',
    'echo "${PREFIX}_sha=abc" >> $GITHUB_OUTPUT',
    # A local script inherits the env var and knows the protocol.
    "./scripts/release.sh",
    # An interpreter fed by a heredoc. transformers sets three outputs this
    # way, and the heredoc body is not shell for this scanner to read.
    'python3 - <<\'EOF\'\nimport os\nwith open(os.environ["GITHUB_OUTPUT"], "a") as f:\n    f.write("matrix=[]\\n")\nEOF',
    "node <<'EOF'\nconsole.log('hi')\nEOF",
    "bash scripts/release.sh",
    "python -c 'import os; open(os.environ[\"GITHUB_OUTPUT\"], \"a\")'",
    "make release",
    "npm run build",
    # The env var used somewhere this cannot place.
    "OUT=$GITHUB_OUTPUT\necho a=1 >> $OUT",
    # Piped in rather than redirected.
    "echo a=1 | tee -a $GITHUB_OUTPUT",
    # A block redirect, whose closing brace is the command.
    "{\n  echo a=1\n  echo b=2\n} >> $GITHUB_OUTPUT",
    # A multi-line value whose delimiter never arrives.
    'echo "notes<<EOF" >> $GITHUB_OUTPUT\necho "body" >> $GITHUB_OUTPUT',
    # The same opaque commands, behind a leading assignment. A shell runs the
    # script either way, so declining either way is the only consistent answer.
    "FOO=bar ./release.sh",
    "GITHUB_TOKEN=x ./scripts/deploy.sh",
    "CI=true npm test",
    "VERSION=1 python3 script.py",
    "LANG=C bash scripts/release.sh",
    "NODE_ENV=production make release",
    "DEBUG=1 FORCE_COLOR=0 ./build.sh",
    # An assignment as an argument rather than a prefix: `make` is still what
    # runs, and `make` is still opaque.
    "make FOO=bar release",
    # A heredoc fed to an interpreter that has one in front of it.
    "PYTHONPATH=. python3 - <<'EOF'\nimport os\nEOF",
]


@pytest.mark.parametrize("script,keys", WRITES)
def test_known_writes_are_read_exactly(script, keys):
    writes = scan_script(script, "bash")
    assert writes.decided, writes.opaque
    assert writes.keys == keys


@pytest.mark.parametrize("script,keys", WRITES)
def test_a_written_key_is_never_called_missing(script, keys):
    """The false positive that would matter most, asserted directly."""
    writes = scan_script(script, "bash")
    for key in keys:
        assert key in writes.keys
        assert not (writes.decided and key not in writes.keys)


@pytest.mark.parametrize("script", WRITES_NOTHING)
def test_scripts_that_write_nothing(script):
    writes = scan_script(script, "bash")
    assert writes.decided, writes.opaque
    assert writes.keys == set()
    assert not writes.mentions


@pytest.mark.parametrize("script", UNDECIDABLE)
def test_undecidable_scripts_decline(script):
    writes = scan_script(script, "bash")
    assert not writes.decided, f"claimed to decide: {script!r} -> {writes.keys}"


def test_a_heredoc_to_an_interpreter_names_the_interpreter():
    """Found in transformers: the heredoc branch skipped the opacity check,
    so three outputs written by the Python on stdin were called missing."""
    writes = scan_script(
        'python3 - <<\'EOF\'\nimport os\n'
        'open(os.environ["GITHUB_OUTPUT"], "a").write("a=1")\nEOF',
        "bash",
    )
    assert not writes.decided
    assert "python3" in (writes.opaque or "")


def test_a_heredoc_to_a_file_still_counts_its_mentions():
    """A script written to disk now is a script that may be run later."""
    writes = scan_script(
        "cat > emit.sh <<'EOF'\necho a=1 >> $GITHUB_OUTPUT\nEOF\nchmod +x emit.sh",
        "bash",
    )
    assert not writes.decided


def test_opaque_reason_names_the_command():
    writes = scan_script("./scripts/release.sh", "bash")
    assert "release.sh" in (writes.opaque or "")


def test_an_env_prefixed_script_is_opaque_and_names_the_script():
    """The false positive this tool exists not to have: `FOO=bar ./release.sh`
    was read as a step that writes nothing, confidently, because the opacity
    check only ever looked at the first word."""
    writes = scan_script("FOO=bar ./release.sh", "bash")
    assert not writes.decided
    assert not writes.writes_nothing
    assert "./release.sh" in (writes.opaque or "")
    assert "FOO=bar" not in (writes.opaque or "")


def test_an_assignment_is_only_an_assignment_in_front():
    """`make FOO=bar` runs make. Stripping past the first non-assignment word
    would lose the command and read this as a step that writes nothing."""
    writes = scan_script("make FOO=bar release", "bash")
    assert not writes.decided
    assert "make" in (writes.opaque or "")


def test_a_quoted_assignment_is_a_command_name():
    """Which is why the match runs against the token with its quotes on: bash
    answers `FOO=bar: command not found` here, and a command that never ran
    wrote no outputs."""
    writes = scan_script('"FOO=bar" hello', "bash")
    assert writes.decided
    assert writes.keys == set()


def test_a_substitution_in_an_assignment_is_not_the_command():
    """Found in grafana: `SHA=$(sha256sum pkg/server/wire_gen.go | cut -f1)`
    stripped down to `pkg/server/wire_gen.go`, which is an argument inside the
    substitution. A decline whose stated reason is untrue costs the same trust
    a false positive does."""
    writes = scan_script(
        'SHA=$(sha256sum pkg/server/wire_gen.go | cut -d" " -f1)\n'
        'echo "sha=$SHA" >> $GITHUB_OUTPUT',
        "bash",
    )
    assert writes.decided, writes.opaque
    assert writes.keys == {"sha"}


def test_an_array_assignment_is_not_the_command():
    """The same leak, spelled `FLAGS=(-l "release/latest")` — a bash array,
    found in grafana's release-pr workflow."""
    writes = scan_script(
        'FLAGS=(-l "release/latest")\necho "a=1" >> $GITHUB_OUTPUT',
        "bash",
    )
    assert writes.decided, writes.opaque
    assert writes.keys == {"a"}


def test_a_closed_substitution_still_leaves_the_command_visible():
    """The guard must not give up on the ordinary case it sits in front of."""
    writes = scan_script('VERSION=$(cat VERSION) ./release.sh', "bash")
    assert not writes.decided
    assert "./release.sh" in (writes.opaque or "")


def test_a_script_run_inside_a_substitution_is_opaque():
    """`V=$(./gen.sh -x)` runs `./gen.sh`, which inherits the env var like any
    other. Found while checking the fix against deno, whose release workflow
    does `DENO_VERSION=$(./deno -V | cut -d ' ' -f 2)`."""
    for script in ("V=$(./gen.sh -x)", "V=$(./gen.sh)", "V=`./gen.sh`"):
        writes = scan_script(script, "bash")
        assert not writes.decided, script
        assert "./gen.sh" in (writes.opaque or ""), script


def test_a_substitution_that_cannot_write_is_left_alone():
    """The point of naming commands rather than refusing all of them: `git`
    will not go looking for the output file, so this step is still checkable."""
    writes = scan_script(
        'SHA=$(git rev-parse HEAD)\necho "sha=$SHA" >> $GITHUB_OUTPUT', "bash"
    )
    assert writes.decided, writes.opaque
    assert writes.keys == {"sha"}


def test_a_substitution_in_an_argument_is_not_treated_as_a_command():
    """The deliberate asymmetry: an interpreter in the assignments in front of
    a command ends the analysis, and the same interpreter in an argument does
    not. Applying it everywhere costs far more steps their keys than the
    false positives it would prevent, so this stays as it shipped."""
    writes = scan_script('echo "x=$(node y.js)" >> $GITHUB_OUTPUT', "bash")
    assert writes.decided, writes.opaque
    assert writes.keys == {"x"}


def test_a_single_quoted_substitution_does_not_run():
    writes = scan_script("V='$(./gen.sh)' ./keep.sh", "bash")
    assert "./keep.sh" in (writes.opaque or "")


def test_a_command_name_with_a_space_in_it_is_not_a_command():
    """Found in grafana: a `$( )` holding a pipe inside quotes puts this
    module's quote tracking out of phase, and a whole fragment of a `sed`
    lands where the command should be. Naming that in a decline is a reason
    that is not true, so it is not a path and not a reason."""
    writes = scan_script(
        """OUT="$(echo x | while read -r line; do echo "- $line" | sed -E 's/a/b/g'; done)"\n"""
        'echo "out=$OUT" >> $GITHUB_OUTPUT',
        "bash",
    )
    assert "done)" not in (writes.opaque or "")


def test_a_quoted_path_with_a_space_is_still_a_path():
    """The other side of it: a real filename can contain a space, and one that
    does arrives quoted whole."""
    writes = scan_script('"./my scripts/release.sh"', "bash")
    assert not writes.decided
    assert "my scripts/release.sh" in (writes.opaque or "")


def test_an_expression_command_behind_an_assignment_is_still_an_expression():
    writes = scan_script("FOO=bar ${{ inputs.cmd }}", "bash")
    assert not writes.decided
    assert "caller passes in" in (writes.opaque or "")


def test_a_bare_assignment_runs_nothing_but_still_counts_its_mention():
    """`_strip_assignments` empties this line. The mention has to survive it,
    or the rule the module is built on has a hole in it."""
    writes = scan_script("OUT=$GITHUB_OUTPUT\necho a=1 >> $OUT", "bash")
    assert not writes.decided
    assert "could not read" in (writes.opaque or "")


def test_unplaced_mention_is_the_catch_all():
    """The rule the module is built on: an unaccounted mention means decline."""
    writes = scan_script("OUT=$GITHUB_OUTPUT\necho a=1 >> $OUT", "bash")
    assert not writes.decided
    assert "could not read" in (writes.opaque or "")


def test_a_non_posix_shell_that_writes_is_declined():
    writes = scan_script('"a=1" >> $env:GITHUB_OUTPUT', "pwsh")
    assert not writes.decided
    assert "pwsh" in (writes.opaque or "")


def test_a_non_posix_shell_that_writes_nothing_is_still_decided():
    """Not mentioning the file is a shell-independent fact."""
    writes = scan_script('Write-Host "hello"', "pwsh")
    assert writes.decided
    assert writes.keys == set()


@pytest.mark.parametrize("shell", sorted(POSIX_SHELLS))
def test_every_posix_shell_gets_key_extraction(shell):
    assert scan_script('echo "a=1" >> $GITHUB_OUTPUT', shell).keys == {"a"}


def test_hash_inside_quotes_is_not_a_comment():
    writes = scan_script('echo "title=fixes #12" >> $GITHUB_OUTPUT', "bash")
    assert writes.decided
    assert writes.keys == {"title"}


def test_a_step_can_both_write_and_be_opaque():
    """An opaque command anywhere ends it, even after good writes."""
    writes = scan_script('echo "a=1" >> $GITHUB_OUTPUT\n./build.sh', "bash")
    assert not writes.decided
