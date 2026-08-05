"""
Part B test: Find Clip in Timelines get_target_clip() tier logic.

The script calls main() at import (and would open a tkinter popup), so the real
functions are extracted from source via ast and exec'd into one namespace.

Two halves:
  1. Stub tests proving tier precedence and every degradation path, with no
     dependence on what happens to be selected in Resolve.
  2. A live smoke test of the helpers against the open project.
"""

import ast
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
SCRIPT = os.path.join(REPO, "Find Clip in Timelines.py")
WANTED = ["_as_list", "get_media_pool_selection", "get_timeline_selection",
          "_earliest_media_pool_item", "get_target_clip"]


def extract(path, names):
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    nodes = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    found = {n.name for n in nodes}
    missing = set(names) - found
    if missing:
        raise SystemExit(f"not found in source: {sorted(missing)}")
    ns = {"print": print}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, "exec"), ns)
    for n in nodes:
        print(f"  extracted {n.name}()  lines {n.lineno}-{n.end_lineno}")
    return ns


ns = extract(SCRIPT, WANTED)
get_target_clip = ns["get_target_clip"]

# ---------------------------------------------------------------------------
# Part 1 - stubs
# ---------------------------------------------------------------------------


class MPI:
    def __init__(self, name):
        self._n = name

    def GetName(self):
        return self._n

    def __repr__(self):
        return f"MPI({self._n!r})"


class Item:
    """Stub TimelineItem."""

    def __init__(self, start, mpi):
        self._s, self._m = start, mpi

    def GetStart(self):
        return self._s

    def GetMediaPoolItem(self):
        return self._m


class MediaPool:
    def __init__(self, sel=None, absent=False):
        self._sel = sel or []
        if absent:
            # Mimic the bridge: unknown attribute resolves to None, not a raise.
            self.GetSelectedClips = None

    def GetSelectedClips(self):  # noqa: F811 - overwritten above when absent
        return self._sel


class Timeline:
    def __init__(self, name, sel=None, playhead=None, absent=False,
                 raises=False):
        self._name, self._sel, self._ph = name, sel, playhead
        self._raises = raises
        if absent:
            self.GetSelectedClips = None

    def GetName(self):
        return self._name

    def GetSelectedClips(self):  # noqa: F811
        if self._raises:
            raise RuntimeError("boom")
        return self._sel

    def GetCurrentVideoItem(self):
        return self._ph


class Project:
    def __init__(self, mp, tl):
        self._mp, self._tl = mp, tl

    def GetMediaPool(self):
        return self._mp

    def GetCurrentTimeline(self):
        return self._tl


a, b, c = MPI("bin_clip"), MPI("tl_clip"), MPI("playhead_clip")
fails = []


def case(label, project, want_mpi, want_label_contains):
    got, lbl = get_target_clip(project)
    ok_mpi = (got is want_mpi)
    ok_lbl = want_label_contains.lower() in (lbl or "").lower()
    status = "ok  " if (ok_mpi and ok_lbl) else "FAIL"
    if not (ok_mpi and ok_lbl):
        fails.append(label)
    print(f"  [{status}] {label}")
    print(f"           -> {got!r}  label={lbl!r}")
    if not ok_mpi:
        print(f"           expected mpi {want_mpi!r}")
    if not ok_lbl:
        print(f"           expected label to contain {want_label_contains!r}")


print("\n--- tier precedence ---")
case("bin selection beats timeline selection and playhead",
     Project(MediaPool([a]), Timeline("T", [Item(5, b)], Item(9, c))),
     a, "Media Pool selection")

case("timeline selection beats playhead when bin is empty",
     Project(MediaPool([]), Timeline("T", [Item(5, b)], Item(9, c))),
     b, "Timeline selection")

case("playhead used when nothing is selected",
     Project(MediaPool([]), Timeline("T", [], Item(9, c))),
     c, "under playhead")

print("\n--- multi-select labelling ---")
case("multi bin selection notes the count",
     Project(MediaPool([a, b]), Timeline("T", [], None)),
     a, "2 clips selected, using the first")

print("\n--- ordering: arbitrary API order must be sorted ---")
later, earlier = MPI("later"), MPI("earlier")
case("earliest GetStart wins regardless of list order",
     Project(MediaPool([]),
             Timeline("T", [Item(900, later), Item(100, earlier)], None)),
     earlier, "using the earliest")

print("\n--- degradation paths ---")
case("timeline API absent (pre-21.0.4) falls through to playhead",
     Project(MediaPool([]), Timeline("T", None, Item(9, c), absent=True)),
     c, "under playhead")

case("timeline API raising falls through to playhead",
     Project(MediaPool([]), Timeline("T", None, Item(9, c), raises=True)),
     c, "under playhead")

case("bin API absent falls through to timeline selection",
     Project(MediaPool(absent=True), Timeline("T", [Item(5, b)], Item(9, c))),
     b, "Timeline selection")

case("selection of generators (no MPI) falls through to playhead",
     Project(MediaPool([]), Timeline("T", [Item(5, None)], Item(9, c))),
     c, "under playhead")

got, lbl = get_target_clip(Project(MediaPool([]), Timeline("T", [], None)))
ok = got is None and "nothing selected" in lbl.lower()
print(f"  [{'ok  ' if ok else 'FAIL'}] nothing anywhere -> (None, reason)")
print(f"           -> {got!r}  label={lbl!r}")
if not ok:
    fails.append("nothing anywhere")

got, lbl = get_target_clip(Project(MediaPool([]), None))
ok = got is None and "nothing selected" in lbl.lower()
print(f"  [{'ok  ' if ok else 'FAIL'}] no timeline at all -> (None, reason)")
if not ok:
    fails.append("no timeline")

# ---------------------------------------------------------------------------
# Part 2 - live smoke test
# ---------------------------------------------------------------------------
print("\n--- live API smoke test ---")
api = os.environ.get(
    "RESOLVE_SCRIPT_API",
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting",
)
sys.path.append(os.path.join(api, "Modules"))
os.environ.setdefault(
    "RESOLVE_SCRIPT_LIB",
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
)
try:
    import DaVinciResolveScript as dvr
    resolve = dvr.scriptapp("Resolve")
    project = resolve.GetProjectManager().GetCurrentProject()
except Exception as exc:
    print(f"  (skipped: {exc})")
    project = None

if project:
    tl = project.GetCurrentTimeline()
    mp_sel = ns["get_media_pool_selection"](project)
    tl_sel = ns["get_timeline_selection"](tl)
    print(f"  media pool selection: {len(mp_sel)} item(s)")
    print(f"  timeline selection:   {len(tl_sel)} item(s)")
    target, label = get_target_clip(project)
    print(f"  get_target_clip -> {target.GetName() if target else None!r}")
    print(f"  source label    -> {label!r}")
    if target is not None and not label:
        fails.append("live: empty label")
    # a real MediaPoolItem must expose GetUniqueId, which main() relies on
    if target is not None:
        try:
            print(f"  target GetUniqueId() -> {target.GetUniqueId()!r}")
        except Exception as exc:
            fails.append(f"live: GetUniqueId failed: {exc}")

print("\n" + ("RESULT: PASS" if not fails
              else f"RESULT: FAIL ({len(fails)}): {fails}"))
sys.exit(0 if not fails else 1)
