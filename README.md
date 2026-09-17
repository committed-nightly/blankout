# blankout

Finds the GitHub Actions step outputs that are always empty. It is for anyone
who has a job that used to run and now quietly does not, and a workflow file
that looks completely fine.

An output that is never written is not an error. `${{ steps.check.outputs.ok }}`
becomes the empty string, `if: needs.build.outputs.ok == 'true'` becomes false,
the job it guards is skipped, and a skipped job is a grey tick rather than a red
one. There is no warning in the log, because nothing went wrong — a step asked
for a value nobody had set, which is a thing GitHub lets you do on purpose.

This is the same failure whether the cause was a typo, a renamed key, or a step
that once wrote the output and stopped. All three look identical from outside,
and all three are visible in the workflow file if something reads it carefully
enough.

Every check is a comparison between things already written in your repository,
so it runs offline, in well under a second, with no token.

## Install

```
pip install git+https://github.com/committed-nightly/blankout
```

Python 3.10 or newer. One dependency, PyYAML.

## Use

```
cd your-repo
blankout
```

It reads `.github/workflows` off disk, plus any `action.yml` your workflows
point at with `uses: ./...`. No network, no token, no Actions API.

```
--json     the same findings, for piping somewhere
--quiet    findings only; do not list what could not be decided
```

Exit code is 0 when every output that is read is also written, 1 when one is
not, and 2 when it could not look at all — no workflow directory, a file that
is not YAML. 2 is not 1 and is very deliberately not 0, because a tool whose
subject is silent failure should not fail silently.

## A real example

```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      release: ${{ steps.check.outputs.release }}
    steps:
      - id: check
        run: |
          echo "checking whether to release"
          git describe --tags

  publish:
    needs: build
    if: needs.build.outputs.release == 'true'
    runs-on: ubuntu-latest
    steps:
      - run: ./publish.sh
```

```
$ blankout
.github/workflows/release.yml: jobs.build.outputs.release
  steps.check.outputs.release is always empty: nothing writes it
  step 0 of `build` never writes to $GITHUB_OUTPUT, so it publishes no outputs
  -> job `publish` is gated on it (`if: needs.build.outputs.release == 'true'`) and is skipped on every run
```

That last line is the reason this exists. "Always empty" is a fact about a
string; "your publish job has not run since somebody edited that step" is the
thing you needed to be told.

## What it looks for

Three routes to the same verdict — **this expression is the empty string on
every run**.

**never-writes** — the step is real and the reference is spelled correctly, and
the script never appends anything to `$GITHUB_OUTPUT` at all. Usually a step
that used to set the output and had that line removed.

**wrong-key** — the script does write outputs, every key it writes can be read
off the script, and this is not one of them. A typo, or one half of a rename.

**not-declared** — the step uses an action in this repository, and that action's
`action.yml` declares no output by this name.

Then, for each job output that is empty because of one of those, it says what
reads it: which `if:` conditions downstream go false, and which jobs those
conditions guard.

## What it refuses to decide

Most of the work here is in not answering. A false positive costs you your
trust in the tool once and permanently, where a missed finding costs you
nothing you did not already have. So anything it cannot prove goes in a
`not checked:` line with the specific reason, and never in a finding:

```
not checked: .github/workflows/ci.yml: jobs.build.outputs.sha (steps.co.outputs.commit) — `actions/checkout@v4` is a published action, and reading it needs the network
```

It declines when:

- The step `uses:` a published action. What `actions/checkout@v4` declares is
  a fact about a repository that is not this one.
- The script writes a key it cannot name: `echo "$name=1" >> $GITHUB_OUTPUT`.
  One unnameable write and no key can be proven missing.
- The script runs an interpreter or a file in your repository — `./release.sh`,
  `make`, `python`, `npm run`. Those inherit `$GITHUB_OUTPUT` and know the
  protocol, and no redirection would appear here. Leading `NAME=value` words are
  separated off first, so `FOO=bar ./release.sh` is the same command as
  `./release.sh`; only the words in front count, so `make FOO=bar` still runs
  `make`. A command inside one of those values counts too — `V=$(./gen.sh)`
  runs `./gen.sh` — but a command inside an *argument* does not, so
  `echo "x=$(node y.js)" >> $GITHUB_OUTPUT` is still read as writing `x`.
- The script mentions `GITHUB_OUTPUT` anywhere the scanner could not place, such
  as `OUT=$GITHUB_OUTPUT`. This is the rule the scanner is built on: every
  occurrence in the script is either a write it understood or a reason to stop.
- The step runs under a shell that is not POSIX — which includes a step on
  `windows-latest` that did not ask for one, because the runner default there is
  `pwsh`. A `pwsh` step that mentions no outputs is still decided, since that
  much does not depend on the shell.
- The step's command is an expression: `run: ${{ inputs.script }}`, which is a
  reusable workflow running whatever its caller passed in.
- The script contains a `${{ }}` anywhere and appears to write nothing.
  Interpolation is textual substitution performed before bash starts, so a
  value carrying a `;` becomes a command. `wrong-key` still applies to such a
  script — that verdict rests on what the script visibly does, rather than on
  something not being there.

A job that calls a reusable workflow is read when the call is a path in this
repository, and left alone when it is not.

## How this differs from actionlint

[actionlint](https://github.com/rhysd/actionlint) is excellent and you should
run it. It checks that the *reference* makes sense: that `steps.foo` names a
step that exists and is defined above this point, that `needs.build` is in your
`needs:` list, and that `needs.build.outputs.tag` is a name the job declares.
Run both — they barely overlap, and this one deliberately says nothing when the
step id is unknown, because actionlint already says it better.

What actionlint cannot tell you is whether the value ever arrives. It types a
step's outputs as `{string => string}`, so every key passes, and it does not
read `run:` scripts. So this:

```yaml
outputs:
  deploy: ${{ steps.check.outputs.deploy }}
steps:
  - id: check
    run: echo "checking"
```

is clean under actionlint and always empty in reality. It also does not read
the `action.yml` of a local composite action, so a reference to an output that
action does not declare passes too. Both were checked against actionlint 1.7.7
rather than assumed.

## Where the answers came from

The output protocol was worked out against the runner's behaviour rather than
paraphrased from the docs, and the corner that mattered was
`echo "notes<<EOF" >> $GITHUB_OUTPUT` — the first line of the documented
multi-line value form, which contains a `<<EOF` that is text and not a
heredoc. Reading it as a heredoc swallows the rest of the script and calls
every output below it unknown.

The tool was run against forty-odd large repositories with heavy Actions use
before it was shipped. The only thing it found on the first pass was a false
positive: next.js runs `run: ${{ inputs.afterBuild }}` inside a reusable
workflow and reads real outputs from that step. That case is why an expression
in command position is opaque, and why `never-writes` is withheld from any
interpolated script.

The second false positive came out of review rather than out of the corpus:
`FOO=bar ./release.sh` was reported as a step that writes nothing, because the
opacity check read the first word of the command and the first word was the
assignment. A shell applies those to the environment and runs what follows, and
now so does this. Fixing it moved the verdict on 41 of the 3,000 `run:` steps in
the corpus, and all 41 were read by hand — which is where the line about
arguments above comes from. Reading substitutions in arguments too would have
taken thirty steps' keys away rather than twelve, to prevent false positives
that none of those thirty actually had.

## Licence

MIT.
