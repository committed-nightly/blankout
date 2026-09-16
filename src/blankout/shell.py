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
confidently wrong. The counter is `unplaced` and the check that enforces it is
at the bottom of `scan_script`.

The second way to be confidently wrong is subtler and it is the reason for
`OPAQUE_COMMANDS`. A script that says only

    ./scripts/release.sh

mentions GITHUB_OUTPUT nowhere and writes outputs anyway, because the env var
is inherited and the script knows what to do with it. A command can reach that
file two ways: a redirection, which is written down here and visible, or by
opening the env var itself, which only a program written for Actions does. So
any interpreter or local executable ends the analysis. `grep` and `git` and
`curl` do not, because they will not go looking.
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

_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
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

    @property
    def decided(self) -> bool:
        """Whether this script can answer "is key K missing?" for any K."""
        return self.opaque is None and not self.unknown


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
    """Break one line into commands on `;`, `&&` and `||`, outside quotes.

    Pipes are deliberately not split on: `echo x | tee $GITHUB_OUTPUT` is one
    command as far as "what does it write" is concerned, and the redirection
    target lives at the end of it.
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
    for sep, heredoc in (("<<", True), ("=", False)):
        head, found, _ = text.partition(sep)
        if not found:
            continue
        head = head.strip()
        if not head or _DYNAMIC.search(head):
            return None, heredoc
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", head):
            return head, heredoc
        return None, heredoc
    return None, False


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

    if shell is not None and shell not in POSIX_SHELLS:
        if writes.mentions:
            writes.unknown = True
            writes.opaque = f"the step runs under `{shell}`, not a POSIX shell"
        return writes

    # Every mention has to be accounted for by the end. Count them first.
    total = script.count("GITHUB_OUTPUT")
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
        stripped = raw.strip()

        # A heredoc body belongs to the command that opened it, never to this
        # loop. Consume it here so its contents are never read as commands.
        heredoc = _HEREDOC.search(_strip_comment(raw))
        if heredoc:
            delim = heredoc.group(2)
            body, closed = [], False
            while i < len(lines):
                if lines[i].strip() == delim:
                    i += 1
                    closed = True
                    break
                body.append(lines[i])
                i += 1
            toks = _tokens(_strip_comment(raw))
            if _redirects_to_output(toks):
                placed += raw.count("GITHUB_OUTPUT")
                if not closed:
                    writes.unknown = True
                else:
                    _keys_from_body(body, writes)
            continue

        line = _strip_comment(raw)
        if not line.strip():
            continue

        for cmd in _split_commands(line):
            toks = _tokens(cmd)
            if not toks:
                continue
            name = _unquote(toks[0]).rsplit("/", 1)[-1]

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

            if name in OPAQUE_COMMANDS or _looks_like_path(toks[0]):
                writes.opaque = (
                    f"the step runs `{_unquote(toks[0])}`, which could write "
                    f"outputs this cannot see"
                )
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


def _looks_like_path(tok: str) -> bool:
    """Whether a command is a file in this repository rather than a utility."""
    tok = _unquote(tok)
    if "=" in tok.split("/")[0]:  # a VAR=value prefix, not a command
        return False
    return tok.startswith(("./", "../", "/")) or "/" in tok
