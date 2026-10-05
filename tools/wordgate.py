#!/usr/bin/env python3
"""Word gate: hard-fails whenever a banned term appears.

The banned terms are stored hex-encoded so that this file never spells
them out. Matching is case-insensitive and also catches common
variations: camelCase / snake_case / kebab-case identifiers, plurals,
separated compounds, full-width letters, accents and invisible
characters.

Modes:
  hook            check one tool call (JSON on stdin); exit 2 blocks it
  files PATH...   check names and contents of files or directories
  tree            check every tracked and untracked (non-ignored) file
  staged          check the git index (names and contents)
  msg FILE        check a commit message plus author/committer identity
  prepush REMOTE URL   check everything a push would send (pre-push stdin)
  history         check every ref, commit, tag, path and blob in the repo
  stdin [LABEL]   check text read from stdin
  selftest        run the built-in tests
  install         selftest, then install the frozen copy the session hook runs
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata

_HEX = [
    "636c61756465",
    "616e7468726f706963",
    "63686174677074",
    "636f646578",
    "6f70656e6169",
    "6169",
    "6d6c",
    "6172746966696369616c20696e74656c6c6967656e6365",
    "6d616368696e65206c6561726e696e67",
    "6172746966616374",
    "70726f76656e616e6365",
    "63616e6f6e6963616c",
    "617574686f72697479",
    "70696e6e6564",
]
TERMS = [bytes.fromhex(h).decode("ascii") for h in _HEX]
SHORT = [t for t in TERMS if len(t) <= 2]
FROZEN_DIR = "/usr/local/lib/zxgate"

_SEP = r"[\s_\-.]*"


def _build_patterns():
    pats = []
    for i, t in enumerate(TERMS):
        if t in SHORT:
            continue
        if " " in t:
            a, b = t.split(" ", 1)
            pats.append((i, re.compile(re.escape(a) + _SEP + re.escape(b))))
        else:
            pats.append((i, re.compile(re.escape(t))))
    # Compound names written apart ("xx yy" for "xxyy").
    for i, cut in ((2, 4), (4, 4)):
        t = TERMS[i]
        pats.append((i, re.compile(
            re.escape(t[:cut]) + r"[\s_\-.]+" + re.escape(t[cut:]) + r"(?![a-z])")))
    return pats


_PATTERNS = _build_patterns()
_TOKEN = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_SHORT_FORMS = {}
for _i, _t in enumerate(TERMS):
    if _t in SHORT:
        _SHORT_FORMS[_t] = _i
        _SHORT_FORMS[_t + "s"] = _i
_SHORT_PLURAL_CAPS = re.compile(
    r"(?<![A-Za-z])(" + "|".join(t.upper() for t in SHORT) + r")s(?![a-z])")
_INVISIBLE = dict.fromkeys(map(ord, "­͏᠎​‌‍⁠﻿"), None)


def normalize(s):
    s = s.translate(_INVISIBLE)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return unicodedata.normalize("NFKC", s)


def find(text):
    """Return a list of (term_index, line_number) hits."""
    text = normalize(text)
    low = text.casefold()
    hits = []

    def line_of(pos):
        return text.count("\n", 0, pos) + 1

    for i, pat in _PATTERNS:
        for m in pat.finditer(low):
            hits.append((i, line_of(m.start())))
    for m in _TOKEN.finditer(text):
        i = _SHORT_FORMS.get(m.group(0).lower())
        if i is not None:
            hits.append((i, line_of(m.start())))
    for m in _SHORT_PLURAL_CAPS.finditer(text):
        hits.append((TERMS.index(m.group(1).lower()), line_of(m.start())))
    return sorted(set(hits), key=lambda h: (h[1], h[0]))


def mask(i):
    t = TERMS[i]
    return "#%d (%s%s)" % (i + 1, t[0], "*" * (len(t) - 1))


def text_of(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    if b"\0" in data:
        runs = re.findall(rb"[\x20-\x7e\t]{3,}", data)
        return "\n".join(r.decode("ascii") for r in runs)
    return data.decode("utf-8", errors="replace")


class Report:
    def __init__(self):
        self.lines = []

    def check(self, label, text):
        for i, line in find(text):
            self.lines.append("%s:%d: banned term %s" % (label, line, mask(i)))

    def check_bytes(self, label, data):
        self.check(label, text_of(data))

    def finish(self, code=1):
        if self.lines:
            print("WORD GATE: BLOCKED", file=sys.stderr)
            for line in self.lines:
                print("  " + line, file=sys.stderr)
            sys.exit(code)
        return 0


# ---------------------------------------------------------------- git helpers

def git(*args, data=None):
    return subprocess.run(["git", *args], input=data, capture_output=True, check=True).stdout


def git_text(*args):
    return git(*args).decode("utf-8", errors="replace")


def cat_blob(sha):
    return git("cat-file", "blob", sha)


def obj_type(sha):
    return git_text("cat-file", "-t", sha).strip()


def check_commit(rep, sha):
    raw = git_text("cat-file", "commit", sha)
    head, _, msg = raw.partition("\n\n")
    kept, skipping = [], False
    for line in head.split("\n"):
        if line.startswith("gpgsig") or line.startswith("mergetag"):
            skipping = True
            continue
        if skipping and line.startswith(" "):
            continue
        skipping = False
        if line.startswith(("author ", "committer ", "encoding ")):
            kept.append(line)
    rep.check("commit %s header" % sha[:10], "\n".join(kept))
    rep.check("commit %s message" % sha[:10], msg)


def check_tag(rep, sha):
    raw = git_text("cat-file", "tag", sha)
    head, _, msg = raw.partition("\n\n")
    msg = msg.split("-----BEGIN", 1)[0]
    rep.check("tag %s" % sha[:10], head + "\n" + msg)


def check_objects(rep, rev_args):
    seen = set()
    out = git_text("rev-list", "--objects", *rev_args)
    for line in out.splitlines():
        sha, _, path = line.partition(" ")
        if not path or sha in seen:
            if path:
                rep.check("path", path)
            continue
        seen.add(sha)
        rep.check("path", path)
        if obj_type(sha) == "blob":
            rep.check_bytes(path, cat_blob(sha))


# ---------------------------------------------------------------- modes

def mode_files(paths):
    rep = Report()
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d != ".git"]
                for f in files:
                    fp = os.path.join(root, f)
                    rep.check("path", fp)
                    with open(fp, "rb") as fh:
                        rep.check_bytes(fp, fh.read())
        else:
            rep.check("path", p)
            with open(p, "rb") as fh:
                rep.check_bytes(p, fh.read())
    return rep.finish()


def mode_tree():
    rep = Report()
    names = git("ls-files", "-co", "--exclude-standard", "-z").split(b"\0")
    for raw in names:
        if not raw:
            continue
        p = raw.decode("utf-8", errors="replace")
        rep.check("path", p)
        if os.path.isfile(p) and not os.path.islink(p):
            with open(p, "rb") as fh:
                rep.check_bytes(p, fh.read())
    return rep.finish()


def mode_staged():
    rep = Report()
    for entry in git("ls-files", "-s", "-z").split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.decode("utf-8", errors="replace").partition("\t")
        mode, sha, _stage = meta.split()
        rep.check("path", path)
        if mode != "160000":
            rep.check_bytes(path, cat_blob(sha))
    return rep.finish()


def mode_msg(path):
    rep = Report()
    with open(path, "rb") as fh:
        rep.check_bytes("commit message", fh.read())
    for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        try:
            rep.check(var.lower(), git_text("var", var))
        except subprocess.CalledProcessError:
            rep.lines.append("%s: could not be read" % var.lower())
    return rep.finish()


ZERO = "0" * 40


def mode_prepush(remote="", url=""):
    rep = Report()
    rep.check("remote", remote + " " + url)
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if set(local_sha) == {"0"}:
            continue
        rep.check("ref", local_ref + " " + remote_ref)
        if set(remote_sha) != {"0"} and subprocess.run(
                ["git", "cat-file", "-e", remote_sha], capture_output=True).returncode == 0:
            rng = [local_sha, "^" + remote_sha]
        else:
            rng = [local_sha, "--not", "--remotes"]
        if obj_type(local_sha) == "tag":
            check_tag(rep, local_sha)
        for sha in git_text("rev-list", *rng).split():
            check_commit(rep, sha)
        check_objects(rep, rng)
    return rep.finish()


def mode_history():
    rep = Report()
    refs = git_text("for-each-ref", "--format=%(objectname) %(objecttype) %(refname)")
    for line in refs.splitlines():
        sha, typ, name = line.split(" ", 2)
        rep.check("ref", name)
        if typ == "tag":
            check_tag(rep, sha)
    try:
        commits = git_text("rev-list", "--all").split()
    except subprocess.CalledProcessError:
        commits = []
    for sha in commits:
        check_commit(rep, sha)
    if commits:
        check_objects(rep, ["--all"])
    return rep.finish()


def mode_stdin(label="stdin"):
    rep = Report()
    rep.check_bytes(label, sys.stdin.buffer.read())
    return rep.finish()


# ---------------------------------------------------------------- session hook

_GUARDS = [
    (re.compile(r"--no-verify"), "bypassing git hooks is not allowed"),
    (re.compile(r"hookspath"), "changing the git hooks path is not allowed"),
    (re.compile(r"\bcommit\b[^|;&\n]*\s-[a-z]*n"), "bypassing git hooks is not allowed"),
    (re.compile(r"settings(\.local)?\.json"), "touching session settings is not allowed"),
    (re.compile(r"(?:\brm\b|\bmv\b|\bchmod\b|\bchown\b|\bchattr\b|\btruncate\b|\bunlink\b"
                r"|\bcp\b|\btee\b|\bln\b|\bsed\s+-i|>)[^|;&\n]*(?:\.githooks|zxgate|wordgate)"),
     "changing the gate outside its own install step is not allowed"),
]


def _walk(obj, key, out, skip_keys):
    if isinstance(obj, str):
        if key not in skip_keys:
            out.append((key or "input", obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _walk(v, k, out, skip_keys)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, key, out, skip_keys)


def mode_hook():
    try:
        event = json.loads(sys.stdin.read())
        tool = str(event.get("tool_name", ""))
        inp = event.get("tool_input") or {}
        strings = []
        # Text being removed by an edit cannot introduce anything.
        skip = {"old_string"} if tool in ("Edit", "MultiEdit") else set()
        _walk(inp, "", strings, skip)
    except Exception as exc:  # fail closed
        print("WORD GATE: could not read the tool call (%s); blocked" % exc, file=sys.stderr)
        sys.exit(2)

    rep = Report()
    for key, value in strings:
        rep.check("%s.%s" % (tool, key), value)
    for key, value in strings:
        low = normalize(value).lower()
        if tool == "Bash" and key == "command":
            for pat, why in _GUARDS:
                if pat.search(low):
                    rep.lines.append("Bash.command: %s" % why)
        elif key in ("file_path", "notebook_path", "path", "local_path"):
            if FROZEN_DIR in low or re.search(r"settings(\.local)?\.json", low):
                rep.lines.append("%s.%s: this path is protected" % (tool, key))
    return rep.finish(code=2)


# ---------------------------------------------------------------- tests

def selftest(verbose=True):
    t = TERMS
    full_width = "".join(chr(ord(c) + 0xFEE0) for c in t[3])
    positives = [
        t[0], t[0].upper(), "My" + t[0].capitalize() + "Tool", "x_" + t[1] + "_y",
        t[2][:4] + " " + t[2][4:].upper(), t[2].upper(),
        t[4][:4].capitalize() + "-" + t[4][4:].upper(), t[4],
        "the " + t[5].upper() + " opponent", "player" + t[5].upper(),
        t[5] + "Level", "zx_" + t[5] + "_mode", t[5] + "2", t[5].upper() + "s",
        t[6].upper() + "Model", "use " + t[6] + " here",
        t[7].replace(" ", "_"), t[7].upper(), t[8].title(), t[8].replace(" ", "-"),
        t[9] + "s", t[10].capitalize(), "non" + t[11], t[12].upper(), "un" + t[13],
        t[0][:3] + "​" + t[0][3:], full_width,
        t[0][:2] + "á" + t[0][3:],
    ]
    negatives = [
        "main", "email", "html", "xml", "yaml", "yml", "toml", "detail", "again",
        "rain", "mail", "Main", "MAIN", "HTML", "XmlParser", "open air",
        "chat about it", "opcode xx", "authored", "pin", "pine", "fail", "AIM",
        "ZX Spectrum 48K", "snakes and ladders", "Spain", "said", "remains",
        "small", "html5", "AMLX", "the email address",
    ]
    bad = 0
    for n, s in enumerate(positives):
        if not find(s):
            bad += 1
            print("selftest: positive case %d not caught" % n, file=sys.stderr)
    for n, s in enumerate(negatives):
        if find(s):
            bad += 1
            print("selftest: negative case %d (%s) wrongly caught" % (n, s), file=sys.stderr)
    if not find(text_of(b"\x00\x01" + t[1].encode() + b"\x00\xff")):
        bad += 1
        print("selftest: binary case not caught", file=sys.stderr)
    guard_cases = [
        ("git commit --no-verify -m x", True), ("git commit -nm x", True),
        ("git -c core.hooksPath=/dev/null commit -m x", True),
        ("rm -rf .githooks", True), ("cp x /usr/local/lib/zxgate/wordgate.py", True),
        ("git commit -m 'add board'", False), ("git push origin main", False),
        ("python3 tools/wordgate.py tree", False),
    ]
    for cmd, expect in guard_cases:
        got = any(p.search(cmd.lower()) for p, _ in _GUARDS)
        if got != expect:
            bad += 1
            print("selftest: guard case %r gave %s" % (cmd, got), file=sys.stderr)
    total = len(positives) + len(negatives) + 1 + len(guard_cases)
    if verbose:
        print("selftest: %d/%d passed" % (total - bad, total))
    return 1 if bad else 0


def mode_install():
    if selftest() != 0:
        print("install: selftest failed; nothing installed", file=sys.stderr)
        return 1
    os.makedirs(FROZEN_DIR, exist_ok=True)
    tmp = os.path.join(FROZEN_DIR, "wordgate.py.new")
    shutil.copyfile(os.path.abspath(__file__), tmp)
    os.chmod(tmp, 0o755)
    if subprocess.run([sys.executable, tmp, "selftest"], capture_output=True).returncode != 0:
        os.remove(tmp)
        print("install: new copy failed its selftest; nothing installed", file=sys.stderr)
        return 1
    os.replace(tmp, os.path.join(FROZEN_DIR, "wordgate.py"))
    print("install: frozen copy updated")
    return 0


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "hook":
        return mode_hook()
    if mode == "files":
        return mode_files(rest)
    if mode == "tree":
        return mode_tree()
    if mode == "staged":
        return mode_staged()
    if mode == "msg":
        return mode_msg(rest[0])
    if mode == "prepush":
        return mode_prepush(*rest[:2])
    if mode == "history":
        return mode_history()
    if mode == "stdin":
        return mode_stdin(*rest[:1])
    if mode == "selftest":
        return selftest()
    if mode == "install":
        return mode_install()
    print("unknown mode: %s" % mode, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
