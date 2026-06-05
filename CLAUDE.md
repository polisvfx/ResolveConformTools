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

## Working in branches

Non-trivial changes should be made on a new branch off `main`, not directly
on `main`.
