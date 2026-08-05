"""
Part A test: Copy Clip to Nuke pick_export_item().

Real functions + the ItemPick dataclass are extracted from source via ast (the
script calls main() at import). Stub tests cover the tie-break and every
degradation path; a live section runs against the open timeline.
"""

import ast
import os
import sys
from dataclasses import dataclass
from typing import Optional

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
SCRIPT = os.path.join(REPO, "Copy Clip to Nuke.py")
WANTED = ["ItemPick", "_as_list", "get_selected_timeline_items",
          "_video_track_index", "pick_export_item"]


def extract(path, names):
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    nodes = [n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.ClassDef))
             and n.name in names]
    found = {n.name for n in nodes}
    if set(names) - found:
        raise SystemExit(f"not found: {sorted(set(names) - found)}")
    ns = {"dataclass": dataclass, "Optional": Optional, "print": print}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, "exec"), ns)
    for n in nodes:
        print(f"  extracted {n.name}  lines {n.lineno}-{n.end_lineno}")
    return ns


ns = extract(SCRIPT, WANTED)
pick_export_item = ns["pick_export_item"]

fails = []


class Item:
    """Stub TimelineItem. track=None mimics a missing GetTrackTypeAndIndex."""

    def __init__(self, name, track_type, track_index, start, has_tti=True):
        self.name = name
        self._tt, self._ti, self._s = track_type, track_index, start
        if not has_tti:
            self.GetTrackTypeAndIndex = None

    def GetTrackTypeAndIndex(self):  # noqa: F811
        return [self._tt, self._ti]

    def GetStart(self):
        return self._s

    def __repr__(self):
        return f"Item({self.name!r} {self._tt}{self._ti}@{self._s})"


class Timeline:
    def __init__(self, sel, playhead=None, absent=False, raises=False):
        self._sel, self._ph, self._raises = sel, playhead, raises
        if absent:
            self.GetSelectedClips = None

    def GetSelectedClips(self):  # noqa: F811
        if self._raises:
            raise RuntimeError("boom")
        return self._sel

    def GetCurrentVideoItem(self):
        return self._ph


def case(label, timeline, want_item, want_count=None, want_label_has=None):
    pick = pick_export_item(timeline)
    got_item = pick.item if pick else None
    ok = got_item is want_item
    if want_count is not None and pick and pick.selected_count != want_count:
        ok = False
    if want_label_has and pick and want_label_has.lower() not in pick.label.lower():
        ok = False
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}")
    if pick:
        print(f"           -> {pick.item!r} count={pick.selected_count} "
              f"label={pick.label!r}")
    else:
        print("           -> None")
    if not ok:
        fails.append(label)
        print(f"           expected {want_item!r}"
              + (f" count={want_count}" if want_count is not None else ""))


print("\n--- the real-world case that drove the tie-break correction ---")
# From the live project: E005C103_260506AE_58.mov stacked on V3@1303 and V4@1308.
# Earliest-start would pick the buried V3; the Viewer and GetCurrentVideoItem
# both show V4. Topmost must win.
v3 = Item("stacked_v3", "video", 3, 1303)
v4 = Item("stacked_v4", "video", 4, 1308)
case("stacked versions at offset starts -> topmost V4 wins, not earliest V3",
     Timeline([v3, v4]), v4, want_count=2, want_label_has="V4")

print("\n--- tie-break details ---")
a1 = Item("a", "video", 4, 1401)
a2 = Item("b", "video", 4, 1421)
case("same track -> earliest start wins", Timeline([a2, a1]), a1, 2)

hi = Item("hi", "video", 5, 9000)
lo = Item("lo", "video", 1, 10)
case("higher track wins even with a much later start",
     Timeline([lo, hi]), hi, 2, "V5")

solo = Item("solo", "video", 2, 500)
case("single selection is used as-is", Timeline([solo]), solo, 1)

print("\n--- audio / subtitle filtering ---")
vid = Item("vid", "video", 1, 100)
aud = Item("aud", "audio", 1, 50)
case("audio item dropped, video kept", Timeline([aud, vid]), vid, 1)

sub = Item("sub", "subtitle", 1, 10)
ph = Item("playhead", "video", 1, 777)
case("selection of only audio+subtitle falls back to playhead",
     Timeline([aud, sub], playhead=ph), ph, 0)

print("\n--- degradation paths ---")
case("API absent (pre-21.0.4) -> playhead",
     Timeline(None, playhead=ph, absent=True), ph, 0)
case("API raising -> playhead", Timeline(None, playhead=ph, raises=True), ph, 0)
case("empty selection -> playhead", Timeline([], playhead=ph), ph, 0)

no_tti = Item("legacy", "video", 1, 5, has_tti=False)
case("item without GetTrackTypeAndIndex is skipped -> playhead",
     Timeline([no_tti], playhead=ph), ph, 0)

pick = pick_export_item(Timeline([], playhead=None))
ok = pick is None
print(f"  [{'ok  ' if ok else 'FAIL'}] nothing selected and no playhead -> None")
if not ok:
    fails.append("no clip at all")

# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------
print("\n--- live ---")
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
    tl = project.GetCurrentTimeline() if project else None
except Exception as exc:
    print(f"  (skipped: {exc})")
    tl = None

if tl:
    live_sel = ns["get_selected_timeline_items"](tl)
    print(f"  live selection: {len(live_sel)} item(s)")
    for it in live_sel:
        print(f"    _video_track_index -> {ns['_video_track_index'](it)}  "
              f"{it.GetName()!r}")
    pick = pick_export_item(tl)
    if pick:
        print(f"  pick: {pick.item.GetName()!r}  count={pick.selected_count}")
        print(f"  label: {pick.label!r}")
        # the picked item must be usable by the extractor
        mpi = pick.item.GetMediaPoolItem()
        print(f"  GetMediaPoolItem -> {mpi.GetName() if mpi else None!r}")
        for meth in ("GetStart", "GetEnd", "GetLeftOffset"):
            try:
                print(f"  {meth}() -> {getattr(pick.item, meth)()}")
            except Exception as exc:
                fails.append(f"live {meth}: {exc}")
    else:
        print("  pick: None (nothing selected, no clip under playhead)")

print("\n" + ("RESULT: PASS" if not fails
              else f"RESULT: FAIL ({len(fails)}): {fails}"))
sys.exit(0 if not fails else 1)
