# Repo conventions

## Internal script versioning

Every script in this repo carries an internal version number in its header
comment / docstring, in the form:

```
Version: X.Y
```

- Lua: `-- Version: X.Y` (or inside the `--[[ ... ]]` block)
- Python: `Version: X.Y` line inside the top-of-file docstring

### When to bump

Whenever you modify a script's behaviour, UI, or fix a bug, bump its version
in the same commit as the change. Use the existing `X.Y` format:

- Bump **Y** (minor) for bug fixes, small tweaks, and most edits.
- Bump **X** (major) and reset Y to 0 only for a significant rewrite or a
  user-facing redesign.

Pure refactors with no observable effect, comment-only changes, README edits,
and edits to non-script files do **not** require a version bump.

### Scope

Applies to every `*.lua` and `*.py` script at the repo root. If a new script
is added, give it `Version: 1.0` in its header.

Files under `tests/` are **not** covered — they carry no version header and
changing them needs no bump.

## Tests

`tests/` holds a suite that runs outside Resolve's UI. See
[tests/README.md](tests/README.md) for prerequisites and how to run it.

Run the relevant tests before committing a change to any script:

```bash
fuscript.exe -l lua tests/luacheck.lua          # parse-check all Lua scripts
fuscript.exe -l lua tests/test_shot_numbering.lua
python tests/test_part_a.py                      # and the other test_part_*.py
```

Most tests need no running Resolve — the Lua ones drive the real scripts against
stubbed API objects. `test_part_d.py` and `final_live_check.py` do need Resolve
open with a project, and exit `2` to signal "skipped" when it is unavailable;
treat a skip as untested, not as a pass.

If you change a script's behaviour, extend the matching test in the same commit.

## Working in branches

Non-trivial changes should be made on a new branch off `main`, not directly
on `main`.

## Releases

Releases are cut by pushing a `vX.Y.Z` tag on `main`. That tag is the **repo's**
version and is independent of the per-script `Version: X.Y` headers above — a
release rolls up whatever versions the scripts happen to carry at that commit.

Pushing the tag runs [.github/workflows/release.yml](.github/workflows/release.yml),
which runs the offline Python tests, builds the zip, and publishes a GitHub
release with GitHub's generated changelog under an install/versions header.

```bash
git tag -a v1.0.1 -m "Release v1.0.1" && git push origin v1.0.1
```

Choose the tag by what changed for users: **patch** for fixes, **minor** for a
new script or feature, **major** for a break in how a script is used.

[tools/make_release_zip.py](tools/make_release_zip.py) builds the payload — every
`*.py` and `*.lua` at the repo root plus `README.md`, under a `ResolveConformTools/`
folder, with a generated `VERSIONS.txt`. It **fails the release** if any root
script is missing its `Version:` header. Run it locally to check what a tag would
ship, before tagging:

```bash
python tools/make_release_zip.py 1.0.1 --out dist
```

The Lua tests and the live Python tests cannot run on a GitHub runner (no
Resolve, no `fuscript`), so run those locally before tagging.
