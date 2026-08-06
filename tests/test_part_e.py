"""
Part E test: Generate All Clips Timeline PRO update-mode logic.

The script calls main() at import (and would open a Fusion UIManager dialog), so
the real functions are extracted from source via ast and exec'd into one
namespace — the test exercises the shipped source rather than a copy.

Every function covered here is deliberately dataclass-free: it takes and returns
plain tuples, dicts, lists and scalars. The ast harness pulls bare FunctionDef
nodes, so anything referencing a module-level dataclass could not be extracted
without keeping a second copy of that dataclass in sync here.

No Resolve needed.
"""

import __future__
import ast
import json
import os
import sys


def _repo_root():
    """Repo root = the parent of this tests/ directory.

    Derived from this file's path so the suite runs from any checkout rather
    than one hardcoded Resolve install. __file__ is undefined in some Resolve
    embedded-Python contexts (see commit 5e768cb), so fall back to the current
    working directory, which covers being run from the repo root or tests/.
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        cwd = os.path.abspath(os.getcwd())
        if os.path.basename(cwd) == "tests":
            here = cwd
        else:
            here = os.path.join(cwd, "tests")
    return os.path.dirname(here)


REPO = _repo_root()
SCRIPT = os.path.join(REPO, "Generate All Clips Timeline PRO.py")
WANTED = ["clip_identity_key"]


def extract(path, names):
    """Pull the named top-level functions, plus every literal module constant.

    Two wrinkles beyond the Part A/B harness:

    - Constants come along because functions like merge_changelog() default an
      argument to a module-level constant, and defaults are evaluated at def
      time. Only assignments whose value is a literal are taken, which skips
      SORT_METHODS (its values are function references that are not extracted).
    - The module is compiled with the `annotations` future flag, matching the
      real file's `from __future__ import annotations`. Without it, an
      annotation like `-> Optional[str]` is evaluated at def time and raises
      NameError because typing was never imported into this namespace.
    """
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)

    consts = []
    for n in tree.body:
        if not isinstance(n, ast.Assign) or len(n.targets) != 1:
            continue
        target = n.targets[0]
        if not isinstance(target, ast.Name) or not target.id.isupper():
            continue
        try:
            ast.literal_eval(n.value)
        except (ValueError, TypeError, SyntaxError):
            continue  # not a literal (e.g. SORT_METHODS) — leave it behind
        consts.append(n)

    nodes = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    found = {n.name for n in nodes}
    missing = set(names) - found
    if missing:
        raise SystemExit(f"not found in source: {sorted(missing)}")

    ns = {"print": print, "json": json}
    module = ast.Module(body=consts + nodes, type_ignores=[])
    exec(compile(module, path, "exec",
                 flags=__future__.annotations.compiler_flag), ns)
    print(f"  extracted {len(consts)} module constant(s)")
    for n in nodes:
        print(f"  extracted {n.name}()  lines {n.lineno}-{n.end_lineno}")
    return ns


ns = extract(SCRIPT, WANTED)
clip_identity_key = ns["clip_identity_key"]

fails = []


def check(label, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"       got  {got!r}")
        print(f"       want {want!r}")
        fails.append(label)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class MPI:
    """Stub MediaPoolItem.

    raises_prop mimics a bridge call that blows up rather than returning None,
    which is the failure mode the real getters are wrapped against.
    """

    def __init__(self, media_id=None, file_path=None, raises_prop=False,
                 raises_id=False):
        self._id = media_id
        self._fp = file_path
        self._raises_prop = raises_prop
        self._raises_id = raises_id

    def GetMediaId(self):
        if self._raises_id:
            raise RuntimeError("boom")
        return self._id

    def GetClipProperty(self, key):
        if self._raises_prop:
            raise RuntimeError("boom")
        if key == "File Path":
            return self._fp
        return None


# ---------------------------------------------------------------------------
# clip_identity_key
# ---------------------------------------------------------------------------

print("\n== clip_identity_key ==")

check("file path wins when merging by source file",
      clip_identity_key(MPI("mid1", "/vol/a.mov"), True), "FP:/vol/a.mov")

check("media id used when merging by source file is off",
      clip_identity_key(MPI("mid1", "/vol/a.mov"), False), "mid1")

check("empty file path falls back to media id",
      clip_identity_key(MPI("mid1", ""), True), "mid1")

check("missing file path falls back to media id",
      clip_identity_key(MPI("mid1", None), True), "mid1")

check("no media id and no path is None",
      clip_identity_key(MPI(None, None), True), None)

check("no media id and no path is None (merge off)",
      clip_identity_key(MPI(None, "/vol/a.mov"), False), None)

check("path-only item still keys by path",
      clip_identity_key(MPI(None, "/vol/a.mov"), True), "FP:/vol/a.mov")

check("GetClipProperty raising falls back to media id",
      clip_identity_key(MPI("mid1", "/vol/a.mov", raises_prop=True), True), "mid1")

check("GetMediaId raising still yields the path key",
      clip_identity_key(MPI("mid1", "/vol/a.mov", raises_id=True), True),
      "FP:/vol/a.mov")

check("GetMediaId raising with no path is None",
      clip_identity_key(MPI("mid1", None, raises_id=True), True), None)

check("empty media id normalises to None",
      clip_identity_key(MPI("", None), True), None)

# Two Media Pool entries for one file share an identity only when merging is on.
# This is the whole point of the helper: the update scan must recognise a clip
# already on the timeline as the same shot the collector just produced.
dual_a = MPI("mid_a", "/vol/shot_010.exr")
dual_b = MPI("mid_b", "/vol/shot_010.exr")
check("two media pool entries, one file, merging on -> same identity",
      clip_identity_key(dual_a, True) == clip_identity_key(dual_b, True), True)
check("two media pool entries, one file, merging off -> different identity",
      clip_identity_key(dual_a, False) == clip_identity_key(dual_b, False), False)


# ---------------------------------------------------------------------------

print("")
if fails:
    print(f"RESULT: FAIL ({len(fails)} failed)")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
