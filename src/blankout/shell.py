"""Deciding which output keys a `run:` script writes, and when to give up.

A step publishes an output by appending `name=value` to the file named in
`$GITHUB_OUTPUT`. That is the whole protocol. So the question "does this step
produce `version`?" is the question "does this script ever append a line
starting `version=` to that file?", which is a question about a shell script,
and shell scripts are not a thing you can answer questions about in general.

So this module is built around one rule, and the rule is what makes the
answers trustworthy rather than merely usually right:

    every occurrence of the string GITHUB_OUTPUT in the script is either a
    write this scanner understood, or a reason to decline.

Nothing is inferred from a mention it could not place. A script that does
`OUT=$GITHUB_OUTPUT` and then writes to `$OUT` is not parsed; it is refused,
by name, because the alternative is to claim the step writes nothing and be
confidently wrong. The counters are `total` and `placed`, and the check that
enforces the rule is the `placed < total` at the bottom of `scan_script`.

The second way to be confidently wrong is subtler and it is the reason for
`OPAQUE_COMMANDS`. A script that says only

    ./scripts/release.sh

mentions GITHUB_OUTPUT nowhere and writes outputs anyway, because the env var
is inherited and the script knows what to do with it. A command can reach that
file two ways: a redirection, which is written down here and visible, or by
opening the env var itself, which only a program written for Actions does. So
any interpreter or local executable ends the analysis. `grep` and `git` and
`curl` do not, because they will not go looking.

The third way is to look in the wrong place for the command. `FOO=bar` in
`FOO=bar ./release.sh` is not a command, it is an assignment the shell applies
to the environment of the one that follows — and the one that follows is
exactly the kind that ends the analysis. So the assignments in front are
separated off before anything here asks what is being run, which is the order a
real shell does it in. Separated rather than discarded, because a value can
hold a command too: `V=$(./gen.sh)` runs `./gen.sh`.

That last part stops where it is, deliberately. A substitution in the
assignments in front of a command is read; one in an argument, as in
`echo "x=$(node y.js)" >> $GITHUB_OUTPUT`, is not. Reading both would be the
tidier rule, and against a corpus of 3,000 real `run:` steps it took thirty
steps' keys away rather than twelve, to prevent false positives that appeared
in none of them. Not answering is cheap here, but it is not free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The env var, as it may be spelled in a redirection target.
_TARGETS = {
    "$GITHUB_OUTPUT",
    "${GITHUB_OUTPUT}",
    '"$GITHUB_OUTPUT"',
    '"${GITHUB_OUTPUT}"',
    "'$GITHUB_OUTPUT'",
    # pwsh, which is the default shell on a Windows runner.
    "$env:GITHUB_OUTPUT",
    '"$env:GITHUB_OUTPUT"',
}

#: Commands that end the analysis, because they may write the output file
#: without any redirection being visible here. Interpreters and task runners:
#: anything that runs code this scanner cannot see.
OPAQUE_COMMANDS = {
    "bash", "sh", "zsh", "dash", "ksh", "source", ".", "eval", "exec",
    "python", "python2", "python3", "py", "node", "deno", "bun", "ruby",
    "perl", "php", "pwsh", "powershell", "osascript",
    "npm", "npx", "yarn", "pnpm", "make", "cargo", "go", "mvn", "gradle",
    "dotnet", "task", "just", "rake", "tox", "nox", "poetry", "pipenv",
    "uv", "uvx", "hatch", "docker", "podman", "ansible", "terraform",
    "xargs", "env", "sudo", "time", "nice", "timeout", "parallel",
}

#: Shells whose key extraction this module implements. Everything else can
#: still be asked the weaker question "does it mention GITHUB_OUTPUT at all".
POSIX_SHELLS = {"bash", "sh", "dash", "zsh"}

_HEREDOC = re.compile(r"^<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SET_OUTPUT = re.compile(r"::\s*set-output\s+name\s*=\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*::")
_DYNAMIC = re.compile(r"[$`]|\$\{\{")


@dataclass
class Writes:
    """What a script was found to do to `$GITHUB_OUTPUT`."""

    #: Keys the script provably writes.
    keys: set[str] = field(default_factory=set)
    #: True if the script writes a key this scanner could not pin down, which
    #: makes "key K is missing" unprovable for every K.
    unknown: bool = False
    #: Why the analysis stopped, if it did. Human-readable, goes in the report.
    opaque: str | None = None
    #: True if GITHUB_OUTPUT or set-output appears anywhere in the script.
    mentions: bool = False
    #: True if the script contains a `${{ }}`, which is substituted into the
    #: text before bash ever sees it. See `writes_nothing`.
    interpolated: bool = False

    @property
    def writes_nothing(self) -> bool:
        """Whether this script can be said to publish no outputs at all.

        That claim rests on the absence of something, and an interpolation is
        exactly the thing that can supply it: `${{ }}` is textual substitution
        performed before the shell runs, so a value carrying a `;` becomes a
        command. So a script with any interpolation in it never gets this
        verdict, even though it may still get `wrong-key` — that one rests on
        what the script visibly does rather than on what it does not.
        """
        return self.decided and not self.mentions and not self.interpolated

    @property
    def decided(self) -> bool:
        """Whether this script can answer "is key K missing?" for any K."""
        return self.opaque is None and not self.unknown


def _heredoc_delimiter(line: str) -> str | None:
    """The delimiter of a heredoc this line opens, if it opens one.

    Quote-aware, and it has to be: `echo "notes<<EOF" >> $GITHUB_OUTPUT` is the
    first line of GitHub's own multi-line output protocol, and it contains a
    `<<EOF` that is text rather than a redirection. Reading it as a heredoc
    swallows the rest of the script.
    """
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if line[i : i + 2] == "<<":
            match = _HEREDOC.match(line[i:])
            return match.group(2) if match else None
        i += 1
    return None


def _strip_comment(line: str) -> str:
    """Drop a trailing `# comment`, respecting quotes.

    `echo "a # b"` keeps its hash. `echo a  # b` loses it.
    """
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            break
        out.append(ch)
    return "".join(out)


def _split_commands(line: str) -> list[str]:
    """Break one line into commands on `;`, `&&`, `||` and `|`, outside quotes.

    Pipes count because what matters about a segment is what starts it: the
    right-hand side of a pipe is a command position, and a `${{ }}` sitting in
    one is somebody else's script.
    """
    parts, buf, quote, i = [], [], None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        if line[i : i + 2] in ("&&", "||"):
            parts.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch == "|":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _tokens(cmd: str) -> list[str]:
    """Split a command into whitespace-separated tokens, keeping quotes."""
    toks, buf, quote = [], [], None
    for ch in cmd:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            continue
        if ch.isspace():
            if buf:
                toks.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        toks.append("".join(buf))
    return toks


def _split_assignments(toks: list[str]) -> tuple[list[str], list[str]]:
    """Separate the leading `NAME=value` words from the command a shell runs.

    `FOO=bar ./release.sh` runs `./release.sh` with `FOO` in its environment,
    so the command to judge is the second word. Only *leading* words are
    assignments: in `make FOO=bar` the assignment is an argument, which is why
    this stops at the first word that is not one.

    The match is against the token with its quotes still on, because quoting is
    what decides this in a shell too: bash runs `"FOO=bar" echo hi` and reports
    `FOO=bar: command not found`, the quotes having turned an assignment into a
    command name.

    The command comes back empty for a line that is only assignments. That is a
    real thing a script does and it runs no command at all, so there is nothing
    to judge — but the mention in `OUT=$GITHUB_OUTPUT` is still a mention, and
    the caller counts it before calling this.

    An assignment whose value opens a `$( )` or a `( )` spans the tokens up to
    the one that closes it, because whitespace inside those does not end a word
    in a shell — and it does end one here. Those are joined back up rather than
    read one at a time: `VERSION=$(cat VERSION) ./release.sh` runs
    `./release.sh`, and taking a token at a time would stop at `VERSION)` and
    call *that* the command. Getting this wrong is not a harmless miss.
    `SHA=$(sha256sum pkg/wire_gen.go | cut -f1)` would come out as
    `pkg/wire_gen.go`, an argument from inside the substitution, and decline
    for a reason that is not true — which costs the same trust a false positive
    does.

    The assignments are handed back rather than dropped because a value can
    contain a command: see `_prefix_opacity`.
    """
    assignments: list[str] = []
    i = 0
    while i < len(toks):
        if not _ASSIGNMENT.match(toks[i]):
            return assignments, toks[i:]
        word = toks[i]
        while not _is_whole_word(word):
            i += 1
            if i >= len(toks):
                # Never closed: it runs to the end of the line and takes the
                # command position with it.
                assignments.append(word)
                return assignments, []
            word += " " + toks[i]
        assignments.append(word)
        i += 1
    return assignments, []


def _is_whole_word(tok: str) -> bool:
    """Whether a token closes everything it opens.

    Whitespace inside `$( )`, `( )` or a pair of backticks does not end a word
    in a shell, and it does end one here, so a token can be the front half of
    one. Both spellings turn up in real workflows: `SHA=$(sha256sum f | cut
    -f1)` is a substitution and `FLAGS=(-l "release/latest")` is an array, and
    in each the next token is part of this word rather than the command.

    Quoted parentheses are text: `MSG="(draft)"` is a whole word.
    """
    depth, backticks, quote = 0, 0, None
    for ch in tok:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "`":
            backticks += 1
    return depth == 0 and backticks % 2 == 0


def _substitutions(text: str) -> list[str]:
    """The interiors of the `$( )` and backtick substitutions in some text.

    Single quotes suppress both; double quotes do not, which is why only the
    single-quoted runs are skipped. An unclosed one runs to the end of the
    text, which is what `_split_assignments` hands over after a `$(` that the
    command-splitter cut in half at a pipe.
    """
    out: list[str] = []
    i, quoted = 0, False
    while i < len(text):
        ch = text[i]
        if quoted:
            quoted = ch != "'"
            i += 1
            continue
        if ch == "'":
            quoted = True
            i += 1
            continue
        if text[i : i + 2] == "$(":
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += (text[j] == "(") - (text[j] == ")")
                j += 1
            out.append(text[i + 2 : j - 1] if depth == 0 else text[i + 2 :])
            i = j
            continue
        if ch == "`":
            j = text.find("`", i + 1)
            out.append(text[i + 1 :] if j == -1 else text[i + 1 : j])
            i = len(text) if j == -1 else j + 1
            continue
        i += 1
    return out


def _prefix_opacity(assignments: list[str]) -> str | None:
    """Why the assignments in front of a command end the analysis, if they do.

    `DENO_VERSION=$(./deno -V)` runs `./deno`. A substitution is a command
    position like the right-hand side of a pipe, and the same rule applies to
    it: a local executable or an interpreter in there could append to the
    output file before the assignment ever happens. `SHA=$(git rev-parse HEAD)`
    is left alone, because `git` will not go looking.
    """
    for word in assignments:
        for inner in _substitutions(word):
            for cmd in _split_commands(_strip_comment(inner)):
                nested, toks = _split_assignments(_tokens(cmd))
                reason = _opacity(toks) or _prefix_opacity(nested)
                if reason:
                    return reason
    return None


def _unquote(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "'\"":
        return tok[1:-1]
    return tok


def _redirects_to_output(toks: list[str]) -> bool:
    """Whether this command's tokens contain a redirection to the output file.

    Both `>> $GITHUB_OUTPUT` and the glued `>>$GITHUB_OUTPUT` spelling, and
    `tee`/`tee -a` given the file as an argument.
    """
    for i, tok in enumerate(toks):
        if tok in (">>", ">") and i + 1 < len(toks) and toks[i + 1] in _TARGETS:
            return True
        if tok.startswith(">>") and tok[2:] in _TARGETS:
            return True
        if tok.startswith(">") and not tok.startswith(">>") and tok[1:] in _TARGETS:
            return True
        if tok in _TARGETS and toks and _unquote(toks[0]).rsplit("/", 1)[-1] == "tee":
            return True
    return False


def _key_from_line(text: str) -> tuple[str | None, bool]:
    """The output key a written line declares, and whether it opens a heredoc.

    `version=1.2` is the key `version`. `notes<<EOF` is the key `notes` and the
    start of a multi-line value. `$name=1` is a key this cannot name, which
    comes back as None and is what `unknown` is for.
    """
    text = text.strip()
    if not text:
        return None, False
    # Whichever separator comes first wins. `notes<<EOF` opens a block;
    # `cmd=git log a<<b` is an ordinary key whose value happens to contain
    # angle brackets, and reading that as a block would swallow the script.
    at_heredoc = text.find("<<")
    at_equals = text.find("=")
    if at_heredoc == -1 and at_equals == -1:
        return None, False
    if at_equals == -1 or (at_heredoc != -1 and at_heredoc < at_equals):
        head, heredoc = text[:at_heredoc], True
    else:
        head, heredoc = text[:at_equals], False
    head = head.strip()
    if not head or _DYNAMIC.search(head):
        return None, heredoc
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", head):
        return head, heredoc
    return None, heredoc


def _echoed_text(toks: list[str]) -> str | None:
    """The literal text an `echo`/`printf` command emits, if it is knowable."""
    if not toks:
        return None
    name = _unquote(toks[0]).rsplit("/", 1)[-1]
    args = [t for t in toks[1:] if not t.startswith(">")]
    # Drop the redirection target, and echo's flags.
    args = [t for t in args if t not in _TARGETS]
    if name == "echo":
        args = [t for t in args if t not in ("-n", "-e", "-E")]
        return " ".join(_unquote(t) for t in args)
    if name == "printf" and args:
        fmt = _unquote(args[0])
        # Only the literal head of the format string matters: the key is
        # whatever comes before the first `=`, and a `%s` after that is a
        # value, which nobody here cares about.
        return fmt.replace("\\n", "\n").split("\n")[0]
    return None


def scan_script(script: str, shell: str | None) -> Writes:
    """Work out what `script` writes to `$GITHUB_OUTPUT`.

    `shell` is the step's `shell:`, or the runner default worked out by the
    caller. Key extraction runs for POSIX shells only; for anything else the
    weaker question — does this script touch outputs at all — is still worth
    asking, and is answered the same way.
    """
    writes = Writes()
    writes.mentions = "GITHUB_OUTPUT" in script or "set-output" in script
    writes.interpolated = "${{" in script

    if shell is not None and shell not in POSIX_SHELLS:
        if writes.mentions:
            writes.unknown = True
            writes.opaque = f"the step runs under `{shell}`, not a POSIX shell"
        return writes

    # Every mention has to be accounted for by the end. Both counters only
    # ever see command text: a mention in a comment, or in the body of a
    # heredoc going somewhere else, is not a command and is not counted.
    total = 0
    placed = 0
    # Set while a `name<<EOF` block is being written to the output file one
    # `echo` at a time, to the delimiter that will close it. Lines written
    # while this is set are the value, not new keys.
    output_delim: str | None = None

    lines = script.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1

        # A heredoc body belongs to the command that opened it, never to this
        # loop. Consume it here so its contents are never read as commands.
        opener = _strip_comment(raw)
        delim = _heredoc_delimiter(opener)
        if delim:
            total += opener.count("GITHUB_OUTPUT")
            body, closed = [], False
            while i < len(lines):
                if lines[i].strip() == delim:
                    i += 1
                    closed = True
                    break
                body.append(lines[i])
                i += 1
            assignments, toks = _split_assignments(_tokens(opener))
            if _redirects_to_output(toks):
                placed += opener.count("GITHUB_OUTPUT")
                if not closed:
                    writes.unknown = True
                else:
                    _keys_from_body(body, writes)
                continue

            # The heredoc feeds something else, and that something else is a
            # command like any other. `python3 - <<'EOF'` is how transformers
            # sets three outputs, and skipping the opacity check here read the
            # whole thing as a step that writes nothing.
            reason = _opacity(toks) or _prefix_opacity(assignments)
            if reason:
                writes.opaque = reason
                return writes

            # Whatever the body is, it is not commands this scanner ran, but a
            # mention of the output file in it is still a mention: a script
            # being written to disk now is a script that may be run later.
            total += sum(line.count("GITHUB_OUTPUT") for line in body)
            total += opener.count("GITHUB_OUTPUT")
            continue

        line = opener
        if not line.strip():
            continue

        for cmd in _split_commands(line):
            toks = _tokens(cmd)
            if not toks:
                continue
            total += cmd.count("GITHUB_OUTPUT")
            # Counted first, then narrowed to the command: `OUT=$GITHUB_OUTPUT`
            # is a mention that has to be accounted for even though it leaves
            # nothing to run.
            assignments, toks = _split_assignments(toks)
            reason = _prefix_opacity(assignments)
            if reason:
                writes.opaque = reason
                return writes
            if not toks:
                continue
            name = _unquote(toks[0]).rsplit("/", 1)[-1]

            if toks[0].startswith("${{") or name.startswith("${{"):
                # The command itself is an expression: `run: ${{ inputs.cmd }}`
                # is a reusable workflow running a script its caller passed in,
                # and nothing here knows what that script does. next.js does
                # exactly this and its outputs are real.
                writes.opaque = (
                    "the step's command is a `${{ }}` expression, so the script "
                    "is whatever the caller passes in"
                )
                return writes

            if _redirects_to_output(toks):
                placed += cmd.count("GITHUB_OUTPUT")
                text = _echoed_text(toks)
                if text is None:
                    # Something writes there and this cannot say what.
                    writes.unknown = True
                    continue
                if output_delim is not None:
                    # Body of a `name<<EOF` block: this is the value, not a
                    # new key. Only the delimiter line ends it.
                    if text.strip() == output_delim:
                        output_delim = None
                    continue
                key, opens = _key_from_line(text)
                if key is None:
                    writes.unknown = True
                else:
                    writes.keys.add(key)
                if opens:
                    output_delim = text.split("<<", 1)[1].strip() or None
                continue

            if _SET_OUTPUT.search(cmd):
                for m in _SET_OUTPUT.finditer(cmd):
                    writes.keys.add(m.group(1))
                continue

            reason = _opacity(toks)
            if reason:
                writes.opaque = reason
                return writes

    if output_delim is not None:
        # A multi-line value that never closed. What follows it in the file is
        # anybody's guess, including whether the keys above it survived.
        writes.unknown = True
    if placed < total:
        writes.unknown = True
        if writes.opaque is None:
            writes.opaque = "the step mentions GITHUB_OUTPUT in a way this could not read"
    return writes


def _keys_from_body(body: list[str], writes: Writes) -> None:
    """Read the body of a heredoc that was redirected into the output file."""
    delim: str | None = None
    for line in body:
        if delim is not None:
            if line.strip() == delim:
                delim = None
            continue
        if not line.strip():
            continue
        key, opens = _key_from_line(line)
        if key is None:
            writes.unknown = True
        else:
            writes.keys.add(key)
        if opens:
            delim = line.split("<<", 1)[1].strip()
    if delim is not None:
        writes.unknown = True


def _opacity(toks: list[str]) -> str | None:
    """Why this command ends the analysis, or None if it does not."""
    if not toks:
        return None
    first = _unquote(toks[0])
    name = first.rsplit("/", 1)[-1]
    if name in OPAQUE_COMMANDS or _looks_like_path(toks[0]):
        return f"the step runs `{first}`, which could write outputs this cannot see"
    return None


def _looks_like_path(tok: str) -> bool:
    """Whether a command is a file in this repository rather than a utility."""
    quoted = len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "'\""
    tok = _unquote(tok)
    # Anything still shaped like `VAR=value/thing` here got past
    # `_split_assignments`, so it is not a name a shell would assign to. Still
    # not a path: `./x.sh` is, `a.b=c/d` is somebody's typo.
    if "=" in tok.split("/")[0]:
        return False
    # A word with a space in it is a word this module's quote tracking lost
    # its place in, not a filename — `$line" | sed -E 's/.../g'; done)"` came
    # out of a `$( )` whose pipes were read as though they were not quoted.
    # Naming that as the command produces a decline for a reason that is not
    # true. A path that really does contain a space arrives quoted whole.
    if not quoted and any(ch.isspace() for ch in tok):
        return False
    return tok.startswith(("./", "../", "/")) or "/" in tok
