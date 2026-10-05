# Snakes and Ladders for the ZX Spectrum 48K

A SANYALnet Labs project: Snakes and Ladders for the 48K ZX Spectrum, with a
cinematic loading screen, a turbo loader, continuous multi-channel background
music and NIRVANA+ multicolour visuals, built by GitHub workflows.

## Word gate

This project has a list of banned terms. They may not appear anywhere in the
repository: file names, file contents (text or binary), commit messages,
author and committer names, branch and tag names, or pull request titles and
descriptions. The list is stored hex-encoded in `tools/wordgate.py` so the
gate itself never spells the terms out.

Matching is case-insensitive and also catches identifier forms
(`camelCase`, `snake_case`, `kebab-case`), plurals, separated compounds,
full-width letters, accents and invisible characters.

The gate runs at three levels, and each one hard-fails:

| Level | When | What it checks |
| --- | --- | --- |
| `pre-commit` hook | every commit | every file in the index, names and contents |
| `commit-msg` hook | every commit | the message, author and committer |
| `pre-push` hook | every push | every commit, tag, path and blob being sent |
| `Word gate` workflow | every push and pull request | the whole tree, the full history, branch name, pull request title and body |

### Turning on the local hooks

After cloning, run once:

```sh
git config core.hooksPath .githooks
```

The hooks need Python 3 (`python3`, `python` or `py` on the PATH). If no
working Python is found they refuse to continue.

### Running it by hand

```sh
python3 tools/wordgate.py tree       # working tree
python3 tools/wordgate.py history    # every commit, tag, path and blob
python3 tools/wordgate.py selftest   # built-in tests
```

Findings are reported by file and line with the term masked, for example
`README.md:12: banned term #3 (c******)`.

### Upstream is read-only

The upstream project,
[snakes-and-ladders-arena](https://github.com/tuklusan/snakes-and-ladders-arena),
is a fetch-only remote. To set it up on a new clone:

```sh
git remote add upstream https://github.com/tuklusan/snakes-and-ladders-arena
git remote set-url --push upstream DISABLED-read-only
git config remote.upstream.tagOpt --no-tags
```

The `pre-push` hook refuses any push to it, whatever the remote is called.
Upstream's own history is not scanned. Anything taken from it into this
repository is scanned like everything else.

### Making it a merge requirement

The workflow can only report after a push lands. To stop anything reaching
`main`, protect the branch on GitHub and mark the **Word gate / scan** check
as required.
