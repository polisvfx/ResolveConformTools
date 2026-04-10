#!/usr/bin/env python
"""
DCTL Report  (DRP-only variant)
================================

Scans the currently-open DaVinci Resolve project for every LUT and DCTL
reference it uses, resolves each reference against the standard Resolve
LUT roots *and* any extra directories configured in Preferences > General
> LUT Locations, and produces a consolidated report of what is present
and what is missing - so you don't have to dismiss N modal warnings one
by one when opening a project authored on another machine.

Data source: the project file itself.
  - ProjectManager.ExportProject() writes the current project to a .drp
    (a ZIP of inner XML files).
  - Each inner XML contains <Body>...</Body> blobs which are hex-encoded,
    prefixed with a framing byte, and zstd-compressed.
  - We decompress every Body and regex-scrape for .dctl/.cube/.3dl/
    .ilut/.olut/.davlut filenames. This is the only known way to recover
    DCTL filenames - every other Resolve export format strips OFX/
    ResolveFX parameters entirely.

Modes:
  - Selected Timelines (default): results are restricted to references
                                    that come from Body blobs that also
                                    mention at least one of the selected
                                    timelines' names.
  - All Timelines:                  every reference the DRP contains.

Actions after scanning:
  - Copy Existing Files...   copies every resolved LUT/DCTL into a
                              destination folder, preserving parent
                              directory structure.
  - Show Missing List...     opens a window with the full *expected*
                              paths of every missing file, one per line,
                              selectable and copyable.
  - Export Full Report...    writes a .txt file listing every LUT and
                              DCTL reference found, categorised by
                              status (missing / found).

Requirements:
  - Resolve 18+
  - PySide6 (bundled with Resolve 19+)
  - zstandard, installed into the Python interpreter Resolve is using:
        <resolve-python> -m pip install zstandard
    Without zstandard the DRP cannot be decoded and the script refuses
    to scan - the user is warned on startup.
"""

from __future__ import annotations

import os
import re
import sys
import shutil
import zipfile
import platform
import tempfile
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QRadioButton, QButtonGroup, QGroupBox,
    QAbstractItemView, QFileDialog, QMessageBox, QPlainTextEdit, QFrame,
    QProgressBar,
)
from PySide6.QtCore import Qt


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RESOLVE_ROOTS = {
    "Windows": [
        os.path.join(os.environ.get("PROGRAMDATA", ""),
                     "Blackmagic Design", "DaVinci Resolve", "Support", "LUT"),
        os.path.join(os.path.expanduser("~"),
                     "AppData", "Roaming", "Blackmagic Design",
                     "DaVinci Resolve", "Support", "LUT"),
    ],
    "Darwin": [
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT",
        os.path.expanduser(
            "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT"),
    ],
    "Linux": [
        "/opt/resolve/LUT",
        os.path.expanduser("~/.local/share/DaVinci Resolve/LUT"),
    ],
}

LUT_EXTS = (".cube", ".3dl", ".ilut", ".olut", ".davlut")

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

DCTL_RE = re.compile(rb"[\w\-./\\: ]{1,200}\.dctl", re.IGNORECASE)
LUT_RE = re.compile(rb"[\w\-./\\: ]{1,200}\.(?:cube|3dl|ilut|olut|davlut)",
                    re.IGNORECASE)


DARK_STYLE = """
QDialog, QWidget {
    background-color: #2b2b2b;
    color: #cccccc;
    font-size: 12px;
}
QGroupBox {
    border: 1px solid #555555;
    border-radius: 3px;
    margin-top: 8px;
    padding-top: 14px;
    font-weight: bold;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 4px;
    color: #cccccc;
}
QLabel { color: #cccccc; }
QRadioButton { color: #cccccc; spacing: 6px; }
QRadioButton::indicator {
    width: 13px; height: 13px;
    border: 1px solid #555555; border-radius: 7px;
    background-color: #3a3a3a;
}
QRadioButton::indicator:checked {
    background-color: #2d89ef; border-color: #2d89ef;
}
QPushButton {
    padding: 5px 14px;
    background-color: #3a3a3a; color: #cccccc;
    border: 1px solid #555555; border-radius: 3px;
}
QPushButton:hover { background-color: #4a4a4a; border-color: #777777; }
QPushButton:pressed { background-color: #2a2a2a; }
QPushButton:disabled { background-color: #2d2d2d; color: #666666; border-color: #3a3a3a; }
QPushButton#Primary {
    background-color: #2d89ef; color: #ffffff; border-color: #2d89ef;
    font-weight: bold;
}
QPushButton#Primary:hover { background-color: #4a9ff5; }
QPushButton#Primary:pressed { background-color: #1e6bc5; }
QListWidget, QPlainTextEdit {
    background-color: #1e1e1e;
    alternate-background-color: #252525;
    border: 1px solid #555555;
    border-radius: 3px;
    color: #cccccc;
    selection-background-color: #2d89ef;
}
QPlainTextEdit { font-family: Consolas, "Courier New", monospace; }
QProgressBar {
    border: 1px solid #555555;
    border-radius: 3px;
    background-color: #1e1e1e;
    color: #cccccc;
    text-align: center;
}
QProgressBar::chunk { background-color: #2d89ef; }
QFrame#Separator { background-color: #555555; max-height: 1px; }
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Reference:
    kind: str          # "LUT" or "DCTL"
    path: str          # stored path as it appears in the project
    source: str        # "DRP/<inner xml>"
    timelines: list = field(default_factory=list)  # timeline names matched in body
    resolved: Optional[str] = None

    @property
    def missing(self) -> bool:
        return self.resolved is None

    @property
    def basename(self) -> str:
        return os.path.basename(self.path.replace("\\", "/"))

    @property
    def norm_path(self) -> str:
        return self.path.replace("\\", "/").lower()


@dataclass
class ScanResult:
    refs: list = field(default_factory=list)
    timelines_scanned: list = field(default_factory=list)
    drp_path: str = ""
    errors: list = field(default_factory=list)

    @property
    def missing(self) -> list:
        return [r for r in self.refs if r.missing]

    @property
    def found(self) -> list:
        return [r for r in self.refs if not r.missing]


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def _dbg(msg: str) -> None:
    """No-op diagnostic sink (diagnostics removed)."""
    pass


def _resolve_config_paths() -> list:
    """Return plausible paths to Resolve's Preferences/config.dat per OS."""
    system = platform.system()
    out = []
    if system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            out.append(os.path.join(
                appdata, "Blackmagic Design", "DaVinci Resolve",
                "Preferences", "config.dat"))
    elif system == "Darwin":
        out.append(os.path.expanduser(
            "~/Library/Application Support/Blackmagic Design/"
            "DaVinci Resolve/config.dat"))
        out.append(os.path.expanduser(
            "~/Library/Preferences/Blackmagic Design/"
            "DaVinci Resolve/config.dat"))
    else:  # Linux
        out.append(os.path.expanduser(
            "~/.local/share/DaVinci Resolve/config.dat"))
        out.append(os.path.expanduser(
            "~/.config/Blackmagic Design/DaVinci Resolve/config.dat"))
    return [p for p in out if p]


def _get_custom_lut_paths_from_config() -> list:
    """Parse Resolve's Preferences/config.dat for the user-configured
    LUT Locations (Preferences > General > LUT Locations).

    Format is a plain key=value text file containing lines like:
        Custom.LUT.Path.Count = 3
        Custom.LUT.Path.1 = M:\\Geteilte Ablagen\\Pipeline\\LUT
        Custom.LUT.Path.2 = S:\\...
    """
    found = []
    for cfg in _resolve_config_paths():
        if not os.path.isfile(cfg):
            _dbg(f"config.dat not at {cfg}")
            continue
        try:
            with open(cfg, "rb") as f:
                raw = f.read()
        except Exception as e:
            _dbg(f"failed to read {cfg}: {e}")
            continue
        # Decode permissively; this is a text-ish file but may have
        # stray bytes.
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = raw.decode("latin-1", errors="replace")
        _dbg(f"parsing {cfg}")
        pat = re.compile(
            r"^\s*Custom\.LUT\.Path\.(\d+)\s*=\s*(.+?)\s*$",
            re.MULTILINE,
        )
        entries = {}
        for m in pat.finditer(text):
            idx = int(m.group(1))
            val = m.group(2).strip().strip('"')
            if val:
                entries[idx] = val
        for _, v in sorted(entries.items()):
            _dbg(f"  Custom.LUT.Path -> {v}")
            found.append(v)
        if entries:
            break  # first config.dat that yields results wins
    return found


def _get_preferences_lut_roots() -> list:
    """Return the LUT directories configured in Preferences > General > LUT
    Locations, sourced live from the Fusion path-map ("Global.Paths.Map.LUTs").

    Resolve searches these directories for both .cube/.3dl/... LUTs and .dctl
    files, so they must be included alongside the standard Support/LUT roots.
    Any failure to read prefs is swallowed and returns an empty list.
    """
    try:
        resolve_app = _get_resolve()
        if resolve_app is None:
            _dbg("no Resolve handle")
            return []
        fusion = resolve_app.Fusion()
        if fusion is None:
            _dbg("no Fusion handle")
            return []

        pm_obj = None
        try:
            pm_obj = fusion.GetPrefs("Global.Paths.Map")
        except Exception as e:
            _dbg(f"GetPrefs('Global.Paths.Map') failed: {e}")
        _dbg(f"raw Global.Paths.Map type={type(pm_obj).__name__} value={pm_obj!r}")

        # Normalize pm_obj into a plain dict if possible
        pm_dict = {}
        if isinstance(pm_obj, dict):
            pm_dict = pm_obj
        elif pm_obj is not None:
            # Fusion sometimes returns a table-like object; try common accessors
            for attr in ("items", "GetItems", "AsTable"):
                try:
                    fn = getattr(pm_obj, attr, None)
                    if callable(fn):
                        data = fn()
                        if isinstance(data, dict):
                            pm_dict = data
                            break
                        if hasattr(data, "items"):
                            pm_dict = dict(data.items())
                            break
                except Exception:
                    pass
            if not pm_dict:
                try:
                    pm_dict = dict(pm_obj)  # may iterate keys
                except Exception:
                    pm_dict = {}
        _dbg(f"normalized pm_dict keys={list(pm_dict.keys())}")

        # Try several key spellings Fusion is known to use
        entry = ""
        for key in ("LUTs", "LUTs:", "LUT", "LUT:"):
            v = pm_dict.get(key)
            if v:
                entry = v
                _dbg(f"found key {key!r} -> {v!r}")
                break

        if not entry:
            for direct in ("Global.Paths.Map.LUTs",
                           "Global.Paths.Map.LUTs:",
                           "Paths.Map.LUTs"):
                try:
                    v = fusion.GetPrefs(direct)
                except Exception as e:
                    _dbg(f"GetPrefs({direct!r}) failed: {e}")
                    v = None
                _dbg(f"GetPrefs({direct!r}) -> {v!r}")
                if v:
                    entry = v
                    break

        if not entry:
            _dbg("no LUT entry found in Fusion prefs")
            return []

        # entry may itself be a table/dict (list of paths) or a ';'-separated string
        parts = []
        if isinstance(entry, dict):
            parts = [str(v) for v in entry.values() if v]
        elif isinstance(entry, (list, tuple)):
            parts = [str(v) for v in entry if v]
        else:
            parts = [p for p in str(entry).split(";")]

        out = []
        for part in parts:
            p = part.strip().strip('"')
            if not p:
                continue
            p = os.path.expandvars(os.path.expanduser(p))
            p = os.path.normpath(p)
            exists = os.path.isdir(p)
            _dbg(f"  parsed entry: {p}  (isdir={exists})")
            out.append(p)
        return out
    except Exception as exc:
        print(f"[DCTL Report] Could not read Preferences "
              f"LUT locations: {exc}", file=sys.stderr)
        return []


def get_roots() -> list:
    roots = [r for r in RESOLVE_ROOTS.get(platform.system(), []) if r]
    seen = {os.path.normcase(os.path.normpath(r)) for r in roots}

    def _add(extra: str) -> None:
        if not extra:
            return
        # Skip Fusion logical-path tokens like "UserPaths:LUTs" that are
        # not real filesystem paths.
        if re.match(r"^[A-Za-z][A-Za-z0-9]*:[^/\\]", extra):
            _dbg(f"skipping Fusion logical path token: {extra}")
            return
        p = os.path.normpath(os.path.expandvars(os.path.expanduser(extra)))
        key = os.path.normcase(p)
        if key in seen:
            return
        seen.add(key)
        roots.append(p)

    for extra in _get_custom_lut_paths_from_config():
        _add(extra)
    for extra in _get_preferences_lut_roots():
        _add(extra)

    _dbg("final roots:")
    for r in roots:
        _dbg(f"  {'[ok] ' if os.path.isdir(r) else '[MISS]'} {r}")
    return roots


def resolve_reference(stored_path: str, roots: list) -> Optional[str]:
    if not stored_path:
        return None
    sp = stored_path.strip().strip('"').replace("\\", "/")
    if not sp:
        return None
    if os.path.isabs(sp) and os.path.isfile(sp):
        return sp
    base = os.path.basename(sp)
    _dbg(f"resolve_reference: stored={stored_path!r} base={base!r}")
    for root in roots:
        if not os.path.isdir(root):
            _dbg(f"  skip (not a dir): {root}")
            continue
        if not os.path.isabs(sp):
            candidate = os.path.normpath(os.path.join(root, sp))
            if os.path.isfile(candidate):
                _dbg(f"  HIT relative: {candidate}")
                return candidate
        _dbg(f"  walking: {root}")
        for dirpath, _dirnames, filenames in os.walk(root):
            if base in filenames:
                hit = os.path.join(dirpath, base)
                _dbg(f"  HIT walk: {hit}")
                return hit
    _dbg(f"  MISS: {base}")
    return None


def _decompress_body(hex_text: str, zstd_module) -> Optional[bytes]:
    cleaned = "".join(hex_text.split())
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError:
        return None
    off = raw.find(ZSTD_MAGIC)
    if off < 0 or off > 4:
        return None
    payload = raw[off:]
    dctx = zstd_module.ZstdDecompressor()
    try:
        return dctx.stream_reader(payload).read()
    except Exception:
        try:
            return dctx.decompress(payload, max_output_size=128 * 1024 * 1024)
        except Exception:
            return None


def _scan_decompressed(data: bytes, source: str) -> list:
    """Extract LUT/DCTL refs from decompressed body bytes."""
    out = []
    local_seen = set()

    def _clean(raw_match: bytes) -> str:
        s = raw_match.decode("ascii", "replace").strip()
        s = re.split(r"[\x00-\x1f]", s)[-1].strip()
        return s

    for m in DCTL_RE.finditer(data):
        s = _clean(m.group())
        if s.lower().startswith("com.blackmagicdesign"):
            continue
        if not s.lower().endswith(".dctl") or len(s) < 6:
            continue
        key = ("DCTL", s.replace("\\", "/").lower())
        if key in local_seen:
            continue
        local_seen.add(key)
        out.append(Reference("DCTL", s, source, []))

    for m in LUT_RE.finditer(data):
        s = _clean(m.group())
        low = s.lower()
        if not any(low.endswith(ext) for ext in LUT_EXTS) or len(s) < 6:
            continue
        key = ("LUT", s.replace("\\", "/").lower())
        if key in local_seen:
            continue
        local_seen.add(key)
        out.append(Reference("LUT", s, source, []))

    return out


def export_project_drp(project_manager, project) -> Optional[str]:
    """Export current project to a temp .drp and return the path."""
    try:
        name = project.GetName()
    except Exception:
        name = "project"
    work_dir = tempfile.mkdtemp(prefix="resolve_missing_drp_")
    # ExportProject refuses to overwrite, so use a fresh path.
    safe = re.sub(r"[^\w\-. ]+", "_", name).strip() or "project"
    drp_path = os.path.join(work_dir, f"{safe}.drp")
    try:
        ok = project_manager.ExportProject(name, drp_path, False)
    except Exception as e:
        print(f"ExportProject raised: {e}")
        return None
    if not ok or not os.path.isfile(drp_path):
        print(f"ExportProject returned {ok!r}, file exists: "
              f"{os.path.isfile(drp_path)}")
        return None
    return drp_path


UUID_RE_BYTES = re.compile(
    rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def build_drp_uuid_map(zf: zipfile.ZipFile, all_timeline_names: list,
                       zstd_module) -> dict:
    """Scan project.xml bodies to build {drp_uuid_lower: timeline_name}.

    Inside each decompressed body we find every UUID string and every
    timeline name occurrence (as ASCII bytes). For each name occurrence,
    we pair it with the UUID whose byte position is closest to it in
    the same body — that's usually the protobuf field that owns the
    name.
    """
    mapping = {}
    try:
        names = zf.namelist()
    except Exception:
        return mapping

    # Known SeqContainer UUIDs straight from filenames — these are the
    # exact UUIDs we need to resolve to timeline names.
    seq_uuids = []
    for n in names:
        norm = n.replace("\\", "/")
        if norm.lower().startswith("seqcontainer/") and norm.lower().endswith(".xml"):
            stem = norm.rsplit("/", 1)[-1][:-4].lower()
            seq_uuids.append(stem)
    if not seq_uuids:
        return mapping
    seq_uuid_bytes = [(u, u.encode("ascii")) for u in seq_uuids]

    # Timeline name probes in UTF-8 and UTF-16LE.
    name_bytes = []
    for n in all_timeline_names:
        if not n:
            continue
        probes = []
        try:
            probes.append(n.encode("utf-8", "ignore"))
        except Exception:
            pass
        try:
            probes.append(n.encode("utf-16-le", "ignore"))
        except Exception:
            pass
        probes = [p for p in probes if p]
        if probes:
            name_bytes.append((n, probes))

    # Candidate: {seq_uuid_lower: {name: best_distance}}
    candidates = {}

    index_files = [n for n in names
                   if n.lower().endswith(".xml")
                   and "seqcontainer/" not in n.lower().replace("\\", "/")]

    def _scan_stream(data: bytes):
        # Find every seq-uuid position.
        uuid_hits = []
        for u, ub in seq_uuid_bytes:
            start = 0
            while True:
                idx = data.find(ub, start)
                if idx < 0:
                    break
                uuid_hits.append((u, idx))
                start = idx + len(ub)
        if not uuid_hits:
            return
        # Find every name position.
        name_hits = []
        for name, probes in name_bytes:
            for nb in probes:
                start = 0
                while True:
                    idx = data.find(nb, start)
                    if idx < 0:
                        break
                    name_hits.append((name, idx))
                    start = idx + len(nb)
        if not name_hits:
            return
        # For each uuid hit, record the nearest name.
        for u, upos in uuid_hits:
            best_name, best_dist = None, None
            for name, npos in name_hits:
                d = abs(upos - npos)
                if best_dist is None or d < best_dist:
                    best_name, best_dist = name, d
            if best_name is None:
                continue
            prev = candidates.setdefault(u, {})
            if best_name not in prev or prev[best_name] > best_dist:
                prev[best_name] = best_dist

    for pf in index_files:
        try:
            raw = zf.read(pf)
        except Exception:
            continue
        _scan_stream(raw)
        try:
            text = raw.decode("utf-8", "replace")
        except Exception:
            text = ""
        for body_text in re.findall(r"<Body>([^<]*)</Body>", text):
            data = _decompress_body(body_text, zstd_module)
            if data is None:
                continue
            _scan_stream(data)

    for u, name_dists in candidates.items():
        # Pick the name with smallest recorded distance to this uuid.
        best_name = min(name_dists.items(), key=lambda kv: kv[1])[0]
        mapping[u] = best_name
    return mapping


def export_timeline_drt(resolve_app, timeline, work_dir: str) -> Optional[str]:
    """Export one timeline to a temp .drt file."""
    try:
        name = timeline.GetName()
    except Exception:
        name = "timeline"
    safe = re.sub(r"[^\w\-. ]+", "_", name).strip() or "timeline"
    # Unique filename in case names collide.
    i = 0
    while True:
        suffix = f"_{i}" if i else ""
        drt_path = os.path.join(work_dir, f"{safe}{suffix}.drt")
        if not os.path.exists(drt_path):
            break
        i += 1
    export_type = getattr(resolve_app, "EXPORT_DRT", None)
    try:
        if export_type is not None:
            ok = timeline.Export(drt_path, export_type)
        else:
            # Fallback: some Resolve versions accept the string constant.
            ok = timeline.Export(drt_path, "drt")
    except Exception as e:
        print(f"Export DRT raised for '{name}': {e}")
        return None
    if not ok or not os.path.isfile(drt_path):
        print(f"Export DRT returned {ok!r} for '{name}', exists: "
              f"{os.path.isfile(drt_path)}")
        return None
    return drt_path


def scan_drt(drt_path: str, timeline_name: str, zstd_module) -> list:
    """Scan a .drt file for LUT/DCTL refs, tagged with timeline_name.

    DRT may be plain XML or a zip of XMLs (Resolve versions differ).
    Bodies in either case may be hex+zstd like DRP bodies. We scan the
    raw bytes of each stream for filename regexes, plus decompress any
    <Body> blobs.
    """
    refs = []
    source = f"DRT/{os.path.basename(drt_path)}"

    def _scan_stream_bytes(data: bytes):
        out = _scan_decompressed(data, source)
        # Decompress any body blobs and scan those too.
        try:
            text = data.decode("utf-8", "replace")
        except Exception:
            text = ""
        for body_text in re.findall(r"<Body>([^<]*)</Body>", text):
            d = _decompress_body(body_text, zstd_module)
            if d is not None:
                out.extend(_scan_decompressed(d, source))
        return out

    try:
        with open(drt_path, "rb") as f:
            head = f.read(4)
    except Exception:
        return refs

    if head.startswith(b"PK"):
        # Zip-packed DRT.
        try:
            zf = zipfile.ZipFile(drt_path)
        except Exception as e:
            print(f"DRT zip open failed: {e}")
            return refs
        try:
            for inner in zf.namelist():
                try:
                    data = zf.read(inner)
                except Exception:
                    continue
                refs.extend(_scan_stream_bytes(data))
        finally:
            zf.close()
    else:
        try:
            with open(drt_path, "rb") as f:
                data = f.read()
        except Exception:
            return refs
        refs.extend(_scan_stream_bytes(data))

    # Dedupe within this DRT and tag with timeline.
    seen = set()
    unique = []
    for r in refs:
        key = (r.kind, r.norm_path)
        if key in seen:
            continue
        seen.add(key)
        r.timelines = [timeline_name]
        unique.append(r)
    return unique


def scan_drp(drp_path: str, all_timeline_names: list,
             uuid_to_name: dict, zstd_module) -> list:
    """Walk every inner xml in the DRP and extract references.

    Timeline association is per-inner-file: for each inner XML, we
    collect every timeline name that appears anywhere in the raw XML
    text OR in any of its decompressed Body blobs, then tag every ref
    extracted from that file with that set. This is reliable because a
    DRP groups a given timeline's data (header, bodies, node metadata)
    into the same inner XML stream.
    """
    refs = []
    # Pre-encode names for case-insensitive byte search. Try both UTF-8
    # (covers plain ASCII strings in XML text and protobuf-ish bodies)
    # and UTF-16LE (covers Qt/wide-string storage that Resolve uses for
    # some metadata fields).
    name_probes = []
    for n in all_timeline_names:
        if not n:
            continue
        low = n.lower()
        probes = []
        try:
            probes.append(low.encode("utf-8", "ignore"))
        except Exception:
            pass
        try:
            probes.append(low.encode("utf-16-le", "ignore"))
        except Exception:
            pass
        name_probes.append((n, [p for p in probes if p]))
    try:
        zf = zipfile.ZipFile(drp_path)
    except zipfile.BadZipFile as e:
        print(f"DRP not a valid zip: {e}")
        return refs
    try:
        # Build DRP-internal UUID -> timeline-name map from project.xml.
        drp_uuid_map = build_drp_uuid_map(zf, all_timeline_names, zstd_module)
        # Merge into passed-in uuid_to_name (GetUniqueId values), the DRP
        # ones take priority since those actually match filenames.
        merged_uuid_map = dict(uuid_to_name)
        merged_uuid_map.update(drp_uuid_map)
        uuid_to_name = merged_uuid_map

        for inner_name in zf.namelist():
            if not inner_name.lower().endswith(".xml"):
                continue
            try:
                raw_bytes = zf.read(inner_name)
            except Exception:
                continue
            text = raw_bytes.decode("utf-8", "replace")

            # Decompress all bodies first, so we can both scan them for
            # refs AND use them (plus the XML text) for timeline matching.
            decompressed_bodies = []
            for body_text in re.findall(r"<Body>([^<]*)</Body>", text):
                d = _decompress_body(body_text, zstd_module)
                if d is not None:
                    decompressed_bodies.append(d)

            # Primary association: if this is a SeqContainer/<uuid>.xml
            # file, look up the timeline by its Resolve unique id.
            matched_timelines = []
            low_inner = inner_name.lower().replace("\\", "/")
            if "seqcontainer/" in low_inner:
                stem = os.path.splitext(os.path.basename(low_inner))[0]
                tl_name = uuid_to_name.get(stem)
                if tl_name:
                    matched_timelines.append(tl_name)
                    # Skip the name-in-body fallback; we have an
                    # authoritative mapping.
                    file_refs = []
                    for d in decompressed_bodies:
                        file_refs.extend(
                            _scan_decompressed(d, f"DRP/{inner_name}"))
                    for r in file_refs:
                        r.timelines = list(matched_timelines)
                    refs.extend(file_refs)
                    continue

            # Primary path: scan the raw XML + decompressed bodies for
            # any timeline name (UTF-8 or UTF-16LE). The SeqContainer
            # UUIDs in filenames do NOT match GetUniqueId(), so name
            # matching is the reliable route.
            haystacks = [raw_bytes.lower()]
            haystacks.extend(b.lower() for b in decompressed_bodies)
            for name, probes in name_probes:
                if not probes:
                    continue
                for p in probes:
                    if any(p in h for h in haystacks):
                        if name not in matched_timelines:
                            matched_timelines.append(name)
                        break

            # Extract refs from every body in this file and tag them.
            file_refs = []
            for d in decompressed_bodies:
                file_refs.extend(_scan_decompressed(d, f"DRP/{inner_name}"))
            for r in file_refs:
                r.timelines = list(matched_timelines)
            refs.extend(file_refs)
    finally:
        zf.close()
    return refs


# ---------------------------------------------------------------------------
# Timeline discovery
# ---------------------------------------------------------------------------

def all_project_timelines(project) -> list:
    out = []
    try:
        count = project.GetTimelineCount() or 0
    except Exception:
        count = 0
    for i in range(1, count + 1):
        try:
            tl = project.GetTimelineByIndex(i)
        except Exception:
            tl = None
        if tl:
            try:
                out.append((tl.GetName(), tl))
            except Exception:
                pass
    return out


def selected_timelines(project) -> list:
    mp = project.GetMediaPool()
    if not mp:
        return []
    try:
        selected = mp.GetSelectedClips() or []
    except Exception:
        return []
    wanted_names = set()
    for clip in selected:
        try:
            if clip.GetClipProperty("Type") == "Timeline":
                wanted_names.add(clip.GetName())
        except Exception:
            pass
    if not wanted_names:
        return []
    out = []
    for name, tl in all_project_timelines(project):
        if name in wanted_names:
            out.append((name, tl))
    return out


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class MissingListDialog(QDialog):
    def __init__(self, missing_lines: str, count: int, parent=None,
                 title: str = "Missing Files",
                 header: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle(f"{title} - {count} item(s)")
        self.setStyleSheet(DARK_STYLE)
        self.resize(780, 480)

        lay = QVBoxLayout(self)
        if header is None:
            header = (f"{count} missing file(s). Full expected paths, "
                      f"one per line (selectable):")
        lay.addWidget(QLabel(header))

        self.text = QPlainTextEdit()
        self.text.setPlainText(missing_lines)
        self.text.setReadOnly(True)
        lay.addWidget(self.text, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        copy_btn = QPushButton("Copy All")
        copy_btn.clicked.connect(self._copy_all)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(copy_btn)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

    def _copy_all(self):
        QApplication.clipboard().setText(self.text.toPlainText())


class ReportDialog(QDialog):
    def __init__(self, resolve_app, project, zstd_module, parent=None):
        super().__init__(parent)
        self.resolve_app = resolve_app
        self.project_manager = resolve_app.GetProjectManager()
        self.project = project
        self.zstd_module = zstd_module
        self.result: Optional[ScanResult] = None

        self.setWindowTitle("DCTL Report")
        self.setStyleSheet(DARK_STYLE)
        self.resize(720, 640)

        self._build_ui()
        self._refresh_timelines()

        if zstd_module is None:
            QMessageBox.warning(
                self,
                "zstandard not available",
                "The Python package `zstandard` is not installed in the "
                "interpreter Resolve is using. This script requires "
                "zstandard to decode the DRP project file and cannot "
                "scan without it.\n\n"
                "Install it into Resolve's Python interpreter:\n\n"
                "    <python> -m pip install zstandard\n\n"
                "The interpreter path is in\n"
                "Resolve > Preferences > System > General > Python."
            )

    # --- UI construction -------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        mode_box = QGroupBox("Scan scope")
        mode_lay = QHBoxLayout(mode_box)
        self.mode_selected = QRadioButton("Selected Timelines")
        self.mode_all = QRadioButton("All Timelines")
        self.mode_selected.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.mode_selected)
        self.mode_group.addButton(self.mode_all)
        self.mode_selected.toggled.connect(self._refresh_timelines)
        self.mode_all.toggled.connect(self._refresh_timelines)
        mode_lay.addWidget(self.mode_selected)
        mode_lay.addWidget(self.mode_all)
        mode_lay.addStretch()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_timelines)
        mode_lay.addWidget(refresh_btn)
        root.addWidget(mode_box)

        tl_box = QGroupBox("Timelines in scope")
        tl_lay = QVBoxLayout(tl_box)
        self.tl_list = QListWidget()
        self.tl_list.setSelectionMode(QAbstractItemView.NoSelection)
        tl_lay.addWidget(self.tl_list)
        root.addWidget(tl_box, 1)

        scan_row = QHBoxLayout()
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.setObjectName("Primary")
        self.scan_btn.clicked.connect(self._do_scan)
        scan_row.addWidget(self.scan_btn)
        self.progress = QProgressBar()
        self.progress.setMinimum(0)
        self.progress.setMaximum(100)
        self.progress.setValue(0)
        scan_row.addWidget(self.progress, 1)
        root.addLayout(scan_row)

        sep = QFrame()
        sep.setObjectName("Separator")
        sep.setFrameShape(QFrame.HLine)
        root.addWidget(sep)

        self.summary = QLabel("Not scanned yet.")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        act_box = QGroupBox("Actions")
        act_lay = QVBoxLayout(act_box)
        self.copy_btn = QPushButton("Copy Existing Files...")
        # Layout rows are assembled below.
        self.copy_btn.setToolTip(
            "Copy every resolved LUT/DCTL to a destination folder, "
            "preserving parent directory structure.")
        self.copy_btn.clicked.connect(self._do_copy)
        self.missing_btn = QPushButton("Show Missing List...")
        self.missing_btn.setToolTip(
            "Show full expected paths of every missing file, "
            "selectable and copyable.")
        self.missing_btn.clicked.connect(self._do_missing)
        self.complete_btn = QPushButton("Show Complete List...")
        self.complete_btn.setToolTip(
            "Show every referenced LUT/DCTL with the path it is being "
            "read from. Missing entries are marked '(missing)'.")
        self.complete_btn.clicked.connect(self._do_complete)
        self.report_btn = QPushButton("Export Full Report...")
        self.report_btn.setToolTip(
            "Write a .txt file listing every LUT and DCTL reference found, "
            "with status and source.")
        self.report_btn.clicked.connect(self._do_report)
        for b in (self.copy_btn, self.missing_btn, self.complete_btn,
                  self.report_btn):
            b.setEnabled(False)
        list_row = QHBoxLayout()
        list_row.addWidget(self.missing_btn)
        list_row.addWidget(self.complete_btn)
        act_lay.addLayout(list_row)
        file_row = QHBoxLayout()
        file_row.addWidget(self.copy_btn)
        file_row.addWidget(self.report_btn)
        act_lay.addLayout(file_row)
        root.addWidget(act_box)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)

    # --- Timeline list ---------------------------------------------------

    def _current_timelines(self) -> list:
        if self.mode_all.isChecked():
            return all_project_timelines(self.project)
        return selected_timelines(self.project)

    def _refresh_timelines(self):
        self.tl_list.clear()
        tls = self._current_timelines()
        if not tls:
            item = QListWidgetItem(
                "(no timelines selected)" if self.mode_selected.isChecked()
                else "(no timelines in project)")
            item.setForeground(Qt.gray)
            self.tl_list.addItem(item)
            self.scan_btn.setEnabled(False)
            return
        for name, _tl in tls:
            self.tl_list.addItem(QListWidgetItem(name))
        self.scan_btn.setEnabled(True)

    # --- Scan ------------------------------------------------------------

    def _do_scan(self):
        if self.zstd_module is None:
            QMessageBox.critical(
                self, "zstandard required",
                "Cannot scan without zstandard installed.")
            return
        in_scope = self._current_timelines()
        if not in_scope:
            return
        scope_names = [n for n, _ in in_scope]
        all_names = [n for n, _ in all_project_timelines(self.project)]

        self.scan_btn.setEnabled(False)
        self.progress.setValue(5)
        QApplication.processEvents()

        drp_path = ""
        raw_refs = []

        if self.mode_all.isChecked():
            # All mode: one DRP export covers every timeline.
            self.summary.setText("Saving project...")
            QApplication.processEvents()
            try:
                self.project.SaveProject()
            except Exception as e:
                print(f"SaveProject warning: {e}")

            self.summary.setText("Exporting project (.drp)...")
            self.progress.setValue(20)
            QApplication.processEvents()

            drp_path = export_project_drp(self.project_manager, self.project)
            if not drp_path:
                QMessageBox.critical(
                    self, "Export failed",
                    "Could not export the current project to a .drp file. "
                    "See the Resolve console for details.")
                self.scan_btn.setEnabled(True)
                self.progress.setValue(0)
                self.summary.setText("Export failed.")
                return

            self.summary.setText(
                f"Scanning DRP ({os.path.basename(drp_path)})...")
            self.progress.setValue(55)
            QApplication.processEvents()
            # No timeline filtering — tag every ref with an empty list;
            # the All-mode filter below accepts everything.
            raw_refs = scan_drp(drp_path, all_names, {}, self.zstd_module)
        else:
            # Selected mode: export each selected timeline to DRT and
            # scan. DRT preserves ResolveFX parameters (including DCTL
            # filenames), and the per-timeline scope is authoritative.
            work_dir = tempfile.mkdtemp(prefix="resolve_missing_drt_")
            total = len(in_scope)
            for i, (name, tl) in enumerate(in_scope):
                self.summary.setText(
                    f"Exporting timeline {i + 1}/{total}: {name}")
                pct = 10 + int(75 * (i / max(total, 1)))
                self.progress.setValue(pct)
                QApplication.processEvents()
                drt_path = export_timeline_drt(self.resolve_app, tl, work_dir)
                if not drt_path:
                    continue
                raw_refs.extend(
                    scan_drt(drt_path, name, self.zstd_module))
            drp_path = work_dir

        self.progress.setValue(85)
        QApplication.processEvents()

        # In selected mode, refs are already tagged per-timeline from DRT.
        # In all mode, no filtering needed.
        filtered = raw_refs

        # Dedupe by (kind, norm_path), merging timeline lists.
        seen = {}
        for ref in filtered:
            key = (ref.kind, ref.norm_path)
            if key in seen:
                existing = seen[key]
                for t in ref.timelines:
                    if t not in existing.timelines:
                        existing.timelines.append(t)
            else:
                seen[key] = ref

        result = ScanResult()
        result.refs = list(seen.values())
        result.timelines_scanned = scope_names
        result.drp_path = drp_path

        roots = get_roots()
        for ref in result.refs:
            ref.resolved = resolve_reference(ref.path, roots)
        result.refs.sort(key=lambda r: (0 if r.kind == "DCTL" else 1,
                                        r.path.lower()))
        self.result = result

        missing = result.missing
        found = result.found
        dctl_m = sum(1 for r in missing if r.kind == "DCTL")
        lut_m = sum(1 for r in missing if r.kind == "LUT")
        scope_desc = ("all timelines" if self.mode_all.isChecked()
                      else f"{len(scope_names)} selected timeline(s)")
        self.summary.setText(
            f"Scanned {scope_desc} via DRP. "
            f"{len(result.refs)} unique reference(s): "
            f"{len(found)} found, {len(missing)} missing "
            f"({dctl_m} DCTL, {lut_m} LUT)."
        )
        self.progress.setValue(100)
        self.copy_btn.setEnabled(bool(found))
        self.missing_btn.setEnabled(bool(missing))
        self.complete_btn.setEnabled(bool(result.refs))
        self.report_btn.setEnabled(bool(result.refs))
        self.scan_btn.setEnabled(True)

    # --- Actions ---------------------------------------------------------

    def _do_copy(self):
        if not self.result:
            return
        found = self.result.found
        if not found:
            QMessageBox.information(self, "Nothing to copy",
                                    "No existing files to copy.")
            return
        dest = QFileDialog.getExistingDirectory(
            self, "Select destination folder for collected files")
        if not dest:
            return

        copied = 0
        skipped = 0
        errors = []
        for ref in found:
            sp = ref.path.strip().strip('"').replace("\\", "/")
            rel = sp if not os.path.isabs(sp) else os.path.basename(sp)
            out_path = os.path.normpath(os.path.join(dest, rel))
            try:
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                if os.path.isfile(out_path):
                    skipped += 1
                    continue
                shutil.copy2(ref.resolved, out_path)
                copied += 1
            except Exception as e:
                errors.append(f"{ref.path}: {e}")

        msg = f"Copied {copied} file(s) to:\n{dest}"
        if skipped:
            msg += f"\n\nSkipped {skipped} file(s) that already existed."
        if errors:
            msg += "\n\nErrors:\n" + "\n".join(errors[:10])
            if len(errors) > 10:
                msg += f"\n... and {len(errors) - 10} more."
        QMessageBox.information(self, "Copy complete", msg)

    def _do_missing(self):
        if not self.result:
            return
        missing = self.result.missing
        if not missing:
            QMessageBox.information(self, "Nothing missing",
                                    "No missing files were found.")
            return
        roots = get_roots()
        primary_root = roots[0] if roots else ""
        lines = []
        for ref in missing:
            sp = ref.path.strip().strip('"').replace("\\", "/")
            if os.path.isabs(sp):
                full = os.path.normpath(sp)
            else:
                full = os.path.normpath(os.path.join(primary_root, sp))
            lines.append(full)
        text = "\n".join(lines)
        dlg = MissingListDialog(text, len(missing), self,
                                title="Missing Files")
        dlg.exec()

    def _do_complete(self):
        if not self.result:
            return
        refs = list(self.result.refs)
        if not refs:
            QMessageBox.information(self, "Nothing to show",
                                    "No LUT/DCTL references were found.")
            return
        roots = get_roots()
        primary_root = roots[0] if roots else ""
        # Sort: DCTLs first, then LUTs; within each kind, missing first, then
        # by basename (case-insensitive) for stable reading.
        refs.sort(key=lambda r: (r.kind != "DCTL",
                                 not r.missing,
                                 r.basename.lower()))
        lines = []
        for ref in refs:
            if ref.resolved:
                path = os.path.normpath(ref.resolved)
                lines.append(f"{path}")
            else:
                sp = ref.path.strip().strip('"').replace("\\", "/")
                if os.path.isabs(sp):
                    path = os.path.normpath(sp)
                else:
                    path = os.path.normpath(
                        os.path.join(primary_root, sp)) if primary_root \
                        else os.path.normpath(sp)
                lines.append(f"{path} (missing)")
        text = "\n".join(lines)
        header = (f"{len(refs)} reference(s). Each line shows the path the "
                  f"file is being read from; missing entries are marked "
                  f"'(missing)'.")
        dlg = MissingListDialog(text, len(refs), self,
                                title="Complete List", header=header)
        dlg.exec()

    def _do_report(self):
        if not self.result:
            return
        default_name = "missing_luts_dctls_report.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save full report", default_name,
            "Text file (*.txt);;All files (*.*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._format_full_report())
            QMessageBox.information(self, "Report saved",
                                    f"Wrote report to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))

    def _format_full_report(self) -> str:
        r = self.result
        if r is None:
            return ""
        lines = []
        lines.append("DCTL Report  (DRP scan)")
        lines.append("=" * 60)
        lines.append(f"DRP: {r.drp_path}")
        lines.append(f"Timelines in scope ({len(r.timelines_scanned)}):")
        for n in r.timelines_scanned:
            lines.append(f"  - {n}")
        lines.append("")
        lines.append("Search roots:")
        for root in get_roots():
            lines.append(f"  - {root}")
        lines.append("")
        lines.append(f"Total unique references: {len(r.refs)}")
        lines.append(f"  Found:   {len(r.found)}")
        lines.append(f"  Missing: {len(r.missing)}")
        lines.append("")

        for status_name, items in (("MISSING", r.missing), ("FOUND", r.found)):
            for kind in ("DCTL", "LUT"):
                sub = [x for x in items if x.kind == kind]
                if not sub:
                    continue
                lines.append(f"=== {status_name} {kind}s ({len(sub)}) ===")
                for ref in sub:
                    lines.append(f"  {ref.basename}")
                    lines.append(f"    stored:   {ref.path}")
                    if ref.resolved:
                        lines.append(f"    resolved: {ref.resolved}")
                    if ref.timelines:
                        lines.append(
                            f"    timelines: {', '.join(ref.timelines)}")
                    lines.append(f"    source:   {ref.source}")
                lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _get_resolve():
    try:
        import DaVinciResolveScript as dvr  # type: ignore
        return dvr.scriptapp("Resolve")
    except Exception:
        try:
            return resolve  # noqa: F821
        except Exception:
            try:
                return bmd.scriptapp("Resolve")  # noqa: F821
            except Exception:
                return None


def _try_import_zstandard():
    try:
        import zstandard  # noqa: F401
        return zstandard
    except ImportError:
        return None


def main():
    resolve_app = _get_resolve()
    if resolve_app is None:
        print("Error: Could not connect to Resolve scripting API.")
        return

    pm = resolve_app.GetProjectManager()
    project = pm.GetCurrentProject() if pm else None
    if not project:
        print("Error: No project is open.")
        return

    zstd_module = _try_import_zstandard()

    print("=== DCTL Report loaded ===")

    app = QApplication.instance()
    standalone = app is None
    if standalone:
        app = QApplication(sys.argv)

    dlg = ReportDialog(resolve_app, project, zstd_module)
    dlg.show()

    if standalone:
        app.exec()
    else:
        dlg.exec()

    print("=== DCTL Report closed ===")


main()
