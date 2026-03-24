#!/usr/bin/env python
"""
Batch Rename
A comprehensive batch renaming utility for DaVinci Resolve.

Features:
  - Search & Replace (plain text or regex)
  - Add Prefix / Suffix (with date/time tokens)
  - Remove N characters from start, end, or specific position
  - Pipeline of composable, reorderable operations
  - Live preview with collision detection
  - Undo history
  - Saveable presets
  - Type filter checkboxes (Video, Audio, Still, Timeline, etc.)

Renames media pool items filtered by type. Uses SetName() API.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QComboBox, QSpinBox, QCheckBox, QPushButton, QTreeWidget,
    QTreeWidgetItem, QListWidget, QListWidgetItem, QWidget, QGroupBox,
    QSplitter, QSizePolicy, QAbstractItemView, QFrame,
)
from PySide6.QtCore import Qt, Signal, QMimeData, QSize
from PySide6.QtGui import QColor, QFont, QDrag


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PREFS_KEY = "ResolveConformTools.BatchRename"
MAX_UNDO = 20

DATETIME_TOKENS = {
    "{date}": lambda dt: dt.strftime("%Y-%m-%d"),
    "{time}": lambda dt: dt.strftime("%H-%M-%S"),
    "{year}": lambda dt: dt.strftime("%Y"),
    "{month}": lambda dt: dt.strftime("%m"),
    "{day}": lambda dt: dt.strftime("%d"),
    "{hour}": lambda dt: dt.strftime("%H"),
    "{minute}": lambda dt: dt.strftime("%M"),
    "{second}": lambda dt: dt.strftime("%S"),
}

# Media pool clip types returned by GetClipProperty("Type").
# Each entry: checkbox ID -> (label, set of type strings to match).
# Resolve may return slightly different strings across versions;
# the sets allow for known variants.
CLIP_TYPE_FILTERS = [
    ("FilterTimeline",  "Timeline",       {"Timeline"}),
    ("FilterVideo",     "Video",          {"Video", "Video + Audio"}),
    ("FilterAudio",     "Audio",          {"Audio"}),
    ("FilterStill",     "Still / Image",  {"Still"}),
    ("FilterCompound",  "Compound Clip",  {"Compound Clip"}),
    ("FilterFusion",    "Fusion Comp",    {"Fusion Composition", "Fusion Comp"}),
    ("FilterGenerator", "Generator",      {"Generator"}),
]

# All filter checkbox IDs including the "Other" catch-all
ALL_FILTER_IDS = [cb_id for cb_id, _, _ in CLIP_TYPE_FILTERS] + ["FilterOther"]

# Flat set of every type string we explicitly handle — anything not in here
# is considered "Other".
_KNOWN_TYPES = set()
for _, _, type_set in CLIP_TYPE_FILTERS:
    _KNOWN_TYPES |= type_set


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OpType(Enum):
    SEARCH_REPLACE = "Search & Replace"
    PREFIX = "Add Prefix"
    SUFFIX = "Add Suffix"
    REMOVE_START = "Remove from Start"
    REMOVE_END = "Remove from End"
    REMOVE_POSITION = "Remove at Position"


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class RenameOp:
    """A single rename operation in the pipeline."""
    op_type: OpType
    search_text: str = ""
    replace_text: str = ""
    case_sensitive: bool = True
    use_regex: bool = False
    affix_text: str = ""
    remove_count: int = 0
    remove_position: int = 0
    enabled: bool = True

    def describe(self) -> str:
        """Human-readable one-line summary for the operations list."""
        if self.op_type == OpType.SEARCH_REPLACE:
            flags = ""
            if not self.case_sensitive:
                flags += " [i]"
            if self.use_regex:
                flags += " [re]"
            return f'Replace "{self.search_text}" -> "{self.replace_text}"{flags}'
        elif self.op_type == OpType.PREFIX:
            return f'Prefix: "{self.affix_text}"'
        elif self.op_type == OpType.SUFFIX:
            return f'Suffix: "{self.affix_text}"'
        elif self.op_type == OpType.REMOVE_START:
            return f"Remove {self.remove_count} chars from start"
        elif self.op_type == OpType.REMOVE_END:
            return f"Remove {self.remove_count} chars from end"
        elif self.op_type == OpType.REMOVE_POSITION:
            return f"Remove {self.remove_count} chars at pos {self.remove_position}"
        return self.op_type.value


@dataclass
class RenamePreset:
    """A saved collection of operations and filter states."""
    name: str
    operations: list = field(default_factory=list)
    filters: dict = field(default_factory=dict)  # {checkbox_id: bool}
    include_subfolders: bool = False


@dataclass
class UndoRecord:
    """Stores one batch rename for undo."""
    description: str
    mappings: list = field(default_factory=list)  # [(media_id, old_name, new_name)]


# ---------------------------------------------------------------------------
# Token Expansion
# ---------------------------------------------------------------------------

def expand_tokens(text: str, now: Optional[datetime] = None) -> str:
    """Replace date/time tokens in text with current values."""
    if now is None:
        now = datetime.now()
    for token, formatter in DATETIME_TOKENS.items():
        if token in text:
            text = text.replace(token, formatter(now))
    return text


# ---------------------------------------------------------------------------
# Pipeline Engine
# ---------------------------------------------------------------------------

def apply_operation(name: str, op: RenameOp, now: Optional[datetime] = None) -> str:
    """Apply a single RenameOp to a clip name string."""
    if now is None:
        now = datetime.now()

    if op.op_type == OpType.SEARCH_REPLACE:
        search = expand_tokens(op.search_text, now)
        replace = expand_tokens(op.replace_text, now)
        if op.use_regex:
            try:
                flags = 0 if op.case_sensitive else re.IGNORECASE
                name = re.sub(search, replace, name, flags=flags)
            except re.error:
                pass
        else:
            if op.case_sensitive:
                name = name.replace(search, replace)
            else:
                try:
                    pattern = re.escape(search)
                    name = re.sub(pattern, replace, name, flags=re.IGNORECASE)
                except re.error:
                    pass

    elif op.op_type == OpType.PREFIX:
        name = expand_tokens(op.affix_text, now) + name

    elif op.op_type == OpType.SUFFIX:
        name = name + expand_tokens(op.affix_text, now)

    elif op.op_type == OpType.REMOVE_START:
        name = name[op.remove_count:]

    elif op.op_type == OpType.REMOVE_END:
        if op.remove_count > 0 and len(name) > op.remove_count:
            name = name[:-op.remove_count]

    elif op.op_type == OpType.REMOVE_POSITION:
        pos = op.remove_position
        count = op.remove_count
        if 0 <= pos < len(name):
            name = name[:pos] + name[pos + count:]

    return name


def apply_pipeline(name: str, operations: list, now: Optional[datetime] = None) -> str:
    """Apply the full operation pipeline sequentially to a name."""
    if now is None:
        now = datetime.now()
    for op in operations:
        if not op.enabled:
            continue
        name = apply_operation(name, op, now)
    return name


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def serialize_op(op: RenameOp) -> dict:
    """Convert a RenameOp to a JSON-serializable dict."""
    return {
        "op_type": op.op_type.value,
        "search_text": op.search_text,
        "replace_text": op.replace_text,
        "case_sensitive": op.case_sensitive,
        "use_regex": op.use_regex,
        "affix_text": op.affix_text,
        "remove_count": op.remove_count,
        "remove_position": op.remove_position,
        "enabled": op.enabled,
    }


def deserialize_op(d: dict) -> RenameOp:
    """Reconstruct a RenameOp from a dict."""
    return RenameOp(
        op_type=OpType(d["op_type"]),
        search_text=d.get("search_text", ""),
        replace_text=d.get("replace_text", ""),
        case_sensitive=d.get("case_sensitive", True),
        use_regex=d.get("use_regex", False),
        affix_text=d.get("affix_text", ""),
        remove_count=d.get("remove_count", 0),
        remove_position=d.get("remove_position", 0),
        enabled=d.get("enabled", True),
    )


# ---------------------------------------------------------------------------
# Resolve API Interaction
# ---------------------------------------------------------------------------

def _get_clip_type(clip) -> str:
    """Return the Type string for a media pool item."""
    try:
        props = clip.GetClipProperty("Type")
        if props:
            return str(props)
    except Exception:
        pass
    return "Unknown"


def _classify_type(type_str: str) -> str:
    """Map a Resolve type string to the user-facing label."""
    for _, label, type_set in CLIP_TYPE_FILTERS:
        if type_str in type_set:
            return label
    return "Other"


def get_media_pool_items(project, recursive: bool = False) -> list:
    """Get all items from the current media pool folder with type info."""
    media_pool = project.GetMediaPool()
    if not media_pool:
        return []
    root_folder = media_pool.GetCurrentFolder()
    if not root_folder:
        return []
    items = []

    def collect_from_folder(folder):
        clips = folder.GetClipList()
        if clips:
            for clip in clips:
                clip_name = clip.GetName() or ""
                media_id = ""
                try:
                    media_id = clip.GetMediaId()
                except Exception:
                    pass
                clip_type = _get_clip_type(clip)
                items.append({
                    "item": clip,
                    "name": clip_name,
                    "media_id": media_id,
                    "type": clip_type,
                    "type_label": _classify_type(clip_type),
                })
        if recursive:
            subfolders = folder.GetSubFolderList()
            if subfolders:
                for subfolder in subfolders:
                    collect_from_folder(subfolder)

    collect_from_folder(root_folder)
    return items


def rename_item(item, new_name: str) -> bool:
    """Rename any media pool item using SetName()."""
    try:
        result = item.SetName(new_name)
        if result:
            return True
    except Exception as e:
        print(f"  SetName() failed: {e}")
    return False


def execute_rename(items: list, operations: list) -> Optional[UndoRecord]:
    """Execute the rename pipeline on all items. Returns an UndoRecord."""
    now = datetime.now()
    mappings = []

    print(f"\n--- Executing Batch Rename ({len(items)} items, "
          f"{len(operations)} operations) ---")

    for item_info in items:
        old_name = item_info["name"]
        new_name = apply_pipeline(old_name, operations, now)

        if new_name != old_name:
            if rename_item(item_info["item"], new_name):
                ident = item_info.get("media_id") or old_name
                mappings.append((ident, old_name, new_name))
                print(f"  Renamed: '{old_name}' -> '{new_name}'")
            else:
                print(f"  FAILED:  '{old_name}' -> '{new_name}'")

    if not mappings:
        return None

    return UndoRecord(
        description=f"Renamed {len(mappings)} items",
        mappings=mappings,
    )


def execute_undo(undo_record: UndoRecord, project) -> int:
    """Reverse a rename operation. Returns number of items reverted."""
    reverted = 0
    # Search recursively to find renamed items wherever they are
    items = get_media_pool_items(project, recursive=True)
    for ident, old_name, new_name in undo_record.mappings:
        for item_info in items:
            if item_info.get("media_id") == ident or item_info["name"] == new_name:
                if rename_item(item_info["item"], old_name):
                    reverted += 1
                break
    return reverted


# ---------------------------------------------------------------------------
# Preset Persistence
# ---------------------------------------------------------------------------

def save_presets(presets: list) -> None:
    """Save all presets to Fusion settings."""
    try:
        fusion = resolve.Fusion()  # noqa: F821
        data = []
        for preset in presets:
            data.append({
                "name": preset.name,
                "operations": preset.operations,
                "filters": preset.filters,
                "include_subfolders": preset.include_subfolders,
            })
        fusion.SetData(f"{PREFS_KEY}.presets", json.dumps(data))
    except Exception as e:
        print(f"Error saving presets: {e}")


def load_presets() -> list:
    """Load presets from Fusion settings."""
    try:
        fusion = resolve.Fusion()  # noqa: F821
        raw = fusion.GetData(f"{PREFS_KEY}.presets")
        if not raw:
            return []
        data = json.loads(raw)
        return [
            RenamePreset(
                name=d["name"],
                operations=d["operations"],
                filters=d.get("filters", {}),
                include_subfolders=d.get("include_subfolders", False),
            )
            for d in data
        ]
    except Exception:
        return []


def save_default_preset_name(name: Optional[str]) -> None:
    """Save (or clear) the default preset name."""
    try:
        fusion = resolve.Fusion()  # noqa: F821
        fusion.SetData(f"{PREFS_KEY}.default_preset", name or "")
    except Exception:
        pass


def load_default_preset_name() -> str:
    """Load the default preset name, or empty string if none."""
    try:
        fusion = resolve.Fusion()  # noqa: F821
        return fusion.GetData(f"{PREFS_KEY}.default_preset") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# PySide6 UI
# ---------------------------------------------------------------------------

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
QLineEdit, QComboBox {
    background-color: #3a3a3a;
    border: 1px solid #555555;
    border-radius: 3px;
    padding: 3px 6px;
    color: #cccccc;
}
QSpinBox {
    background-color: #3a3a3a;
    border: 1px solid #555555;
    border-radius: 3px;
    padding: 3px 6px;
    color: #cccccc;
    min-width: 50px;
}
QSpinBox::up-button {
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 16px;
    border-left: 1px solid #555555;
    background-color: #3a3a3a;
}
QSpinBox::up-button:hover { background-color: #4a4a4a; }
QSpinBox::up-button:pressed { background-color: #2a2a2a; }
QSpinBox::up-arrow { image: none; border: none; width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-bottom: 5px solid #cccccc; }
QSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 16px;
    border-left: 1px solid #555555;
    border-top: 1px solid #555555;
    background-color: #3a3a3a;
}
QSpinBox::down-button:hover { background-color: #4a4a4a; }
QSpinBox::down-button:pressed { background-color: #2a2a2a; }
QSpinBox::down-arrow { image: none; border: none; width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid #cccccc; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border-color: #2d89ef;
}
QComboBox::drop-down {
    border: none;
    padding-right: 6px;
}
QComboBox QAbstractItemView {
    background-color: #3a3a3a;
    color: #cccccc;
    selection-background-color: #2d89ef;
}
QCheckBox { color: #cccccc; spacing: 4px; }
QCheckBox::indicator {
    width: 14px; height: 14px;
    border: 1px solid #555555; border-radius: 2px;
    background-color: #3a3a3a;
}
QCheckBox::indicator:checked {
    background-color: #2d89ef; border-color: #2d89ef;
}
QPushButton {
    padding: 4px 12px;
    background-color: #3a3a3a; color: #cccccc;
    border: 1px solid #555555; border-radius: 3px;
}
QPushButton:hover { background-color: #4a4a4a; border-color: #777777; }
QPushButton:pressed { background-color: #2a2a2a; }
QPushButton:disabled { background-color: #2d2d2d; color: #666666; border-color: #3a3a3a; }
QTreeWidget {
    background-color: #1e1e1e;
    alternate-background-color: #252525;
    border: 1px solid #555555;
    border-radius: 3px;
    color: #cccccc;
}
QTreeWidget::item:selected { background-color: #2d89ef; }
QTreeWidget QHeaderView::section {
    background-color: #333333;
    color: #cccccc;
    border: none;
    border-right: 1px solid #555555;
    padding: 3px 6px;
    font-weight: bold;
}
QListWidget {
    background-color: #1e1e1e;
    border: 1px solid #555555;
    border-radius: 3px;
    color: #cccccc;
}
QListWidget::item { padding: 0px; }
QListWidget::item:selected { background-color: transparent; }
QSplitter::handle {
    background-color: #555555;
    width: 3px;
}
"""

OP_ROW_STYLE = """
QWidget#OpRow { background: transparent; }
QWidget#OpRow:hover QLabel#OpIndex { color: #ffffff; }
QLabel#OpDesc { color: #cccccc; }
QLabel#OpIndex { color: #888888; font-weight: bold; min-width: 18px; }
QLabel#DragHandle { color: #666666; font-size: 14px; min-width: 16px; }
QPushButton#OpDelete {
    background: transparent; border: none; color: #666666;
    font-size: 16px; font-weight: bold; padding: 0px;
    min-width: 20px; max-width: 20px;
}
QPushButton#OpDelete:hover { color: #ff6b6b; }
"""


class OperationRowWidget(QWidget):
    """Custom widget for a single operation row in the list."""
    delete_clicked = Signal(int)
    toggled = Signal(int, bool)

    def __init__(self, index: int, op: RenameOp, parent=None):
        super().__init__(parent)
        self.setObjectName("OpRow")
        self.index = index
        self.op = op

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        # Enable checkbox
        self.enabled_cb = QCheckBox()
        self.enabled_cb.setChecked(op.enabled)
        self.enabled_cb.setToolTip("Enable / disable this operation")
        self.enabled_cb.setFixedWidth(18)
        self.enabled_cb.toggled.connect(self._on_toggled)
        layout.addWidget(self.enabled_cb)

        # Drag handle
        self.handle = QLabel("\u2261")
        self.handle.setObjectName("DragHandle")
        self.handle.setAlignment(Qt.AlignCenter)
        self.handle.setFixedWidth(16)
        layout.addWidget(self.handle)

        # Index number
        self.idx_label = QLabel(str(index + 1))
        self.idx_label.setObjectName("OpIndex")
        self.idx_label.setAlignment(Qt.AlignCenter)
        self.idx_label.setFixedWidth(20)
        layout.addWidget(self.idx_label)

        # Description
        self.desc_label = QLabel(op.describe())
        self.desc_label.setObjectName("OpDesc")
        layout.addWidget(self.desc_label, stretch=1)

        # Delete button
        delete_btn = QPushButton("\u00d7")
        delete_btn.setObjectName("OpDelete")
        delete_btn.setFixedSize(20, 20)
        delete_btn.setCursor(Qt.PointingHandCursor)
        delete_btn.setToolTip("Remove operation")
        delete_btn.clicked.connect(lambda: self.delete_clicked.emit(self.index))
        layout.addWidget(delete_btn)

        self.setStyleSheet(OP_ROW_STYLE)
        self._apply_enabled_style()

    def _on_toggled(self, checked: bool):
        self.op.enabled = checked
        self._apply_enabled_style()
        self.toggled.emit(self.index, checked)

    def _apply_enabled_style(self):
        opacity = "1.0" if self.op.enabled else "0.4"
        for w in (self.handle, self.idx_label, self.desc_label):
            w.setStyleSheet(f"opacity: {opacity};" if not self.op.enabled else "")
        if not self.op.enabled:
            self.desc_label.setStyleSheet("color: #666666; text-decoration: line-through;")
        else:
            self.desc_label.setStyleSheet("")


class DragDropOpsList(QListWidget):
    """QListWidget with drag-drop reordering and custom row widgets."""
    order_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSpacing(1)

    def dropEvent(self, event):
        super().dropEvent(event)
        self.order_changed.emit()


class BatchRenameDialog(QDialog):
    """Main Batch Rename dialog using PySide6."""

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project = project
        self.operations: list[RenameOp] = []
        self.undo_stack: list[UndoRecord] = []
        self.presets: list[RenamePreset] = load_presets()
        self.cached_items: list = []
        self.editing_index: Optional[int] = None
        self.default_preset: str = load_default_preset_name()

        self.setWindowTitle("Batch Rename")
        self.resize(1400, 700)
        self.setMinimumSize(800, 500)
        self.setStyleSheet(DARK_STYLE)
        self._build_ui()
        self._connect_signals()
        self._initial_state()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter)

        # ---- LEFT PANEL ----
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        # Type filters
        filter_group = QGroupBox("Type Filters")
        filter_layout = QVBoxLayout(filter_group)
        filter_row = QHBoxLayout()
        self.filter_checks = {}
        for _, label, _ in CLIP_TYPE_FILTERS:
            cb = QCheckBox(label)
            cb.setChecked(True)
            self.filter_checks[label] = cb
            filter_row.addWidget(cb)
        cb_other = QCheckBox("Other")
        cb_other.setChecked(True)
        self.filter_checks["Other"] = cb_other
        filter_row.addWidget(cb_other)
        filter_row.addStretch()
        filter_layout.addLayout(filter_row)

        subfolder_row = QHBoxLayout()
        self.include_subfolders = QCheckBox("Include Subfolders")
        subfolder_row.addWidget(self.include_subfolders)
        subfolder_row.addStretch()
        filter_layout.addLayout(subfolder_row)
        left_layout.addWidget(filter_group)

        # Operation builder
        builder_group = QGroupBox("Add Operation")
        builder_layout = QVBoxLayout(builder_group)

        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Type:"))
        self.op_type_combo = QComboBox()
        for ot in OpType:
            self.op_type_combo.addItem(ot.value)
        type_row.addWidget(self.op_type_combo, stretch=1)
        builder_layout.addLayout(type_row)

        # Search & Replace fields (container widget for show/hide)
        self.search_container = QWidget()
        search_layout = QVBoxLayout(self.search_container)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(4)

        find_row = QHBoxLayout()
        find_row.addWidget(QLabel("Find:"))
        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("Text to find")
        find_row.addWidget(self.search_field, stretch=1)
        search_layout.addLayout(find_row)

        replace_row = QHBoxLayout()
        replace_row.addWidget(QLabel("Replace:"))
        self.replace_field = QLineEdit()
        self.replace_field.setPlaceholderText("Replace with (tokens: {date} {year}...)")
        replace_row.addWidget(self.replace_field, stretch=1)
        search_layout.addLayout(replace_row)

        flags_row = QHBoxLayout()
        self.case_sensitive = QCheckBox("Case Sensitive")
        self.case_sensitive.setChecked(True)
        self.use_regex = QCheckBox("Use Regex")
        flags_row.addWidget(self.case_sensitive)
        flags_row.addWidget(self.use_regex)
        flags_row.addStretch()
        search_layout.addLayout(flags_row)
        builder_layout.addWidget(self.search_container)

        # Prefix / Suffix fields (container widget for show/hide)
        self.affix_container = QWidget()
        affix_layout = QHBoxLayout(self.affix_container)
        affix_layout.setContentsMargins(0, 0, 0, 0)
        affix_layout.addWidget(QLabel("Text:"))
        self.affix_field = QLineEdit()
        self.affix_field.setPlaceholderText("Tokens: {date} {time} {year} {month} {day}...")
        affix_layout.addWidget(self.affix_field, stretch=1)
        builder_layout.addWidget(self.affix_container)

        # Remove fields (container widget for show/hide)
        self.remove_container = QWidget()
        remove_layout = QHBoxLayout(self.remove_container)
        remove_layout.setContentsMargins(0, 0, 0, 0)
        self.remove_count_label = QLabel("Count:")
        remove_layout.addWidget(self.remove_count_label)
        self.remove_count = QSpinBox()
        self.remove_count.setRange(1, 999)
        self.remove_count.setValue(1)
        remove_layout.addWidget(self.remove_count)
        self.position_label = QLabel("Position:")
        remove_layout.addWidget(self.position_label)
        self.remove_position = QSpinBox()
        self.remove_position.setRange(0, 999)
        remove_layout.addWidget(self.remove_position)
        remove_layout.addStretch()
        builder_layout.addWidget(self.remove_container)

        btn_row = QHBoxLayout()
        self.add_op_btn = QPushButton("Add Operation")
        self.cancel_edit_btn = QPushButton("Cancel Edit")
        self.cancel_edit_btn.setEnabled(False)
        btn_row.addWidget(self.add_op_btn)
        btn_row.addWidget(self.cancel_edit_btn)
        btn_row.addStretch()
        builder_layout.addLayout(btn_row)
        left_layout.addWidget(builder_group)

        # Operations pipeline
        ops_group = QGroupBox("Operation Pipeline")
        ops_layout = QVBoxLayout(ops_group)
        self.ops_list = DragDropOpsList()
        ops_layout.addWidget(self.ops_list)

        ops_btn_row = QHBoxLayout()
        self.move_up_btn = QPushButton("Move Up")
        self.move_down_btn = QPushButton("Move Down")
        self.edit_op_btn = QPushButton("Edit")
        self.clear_ops_btn = QPushButton("Clear All")
        ops_btn_row.addWidget(self.move_up_btn)
        ops_btn_row.addWidget(self.move_down_btn)
        ops_btn_row.addWidget(self.edit_op_btn)
        ops_btn_row.addWidget(self.clear_ops_btn)
        ops_btn_row.addStretch()
        ops_layout.addLayout(ops_btn_row)
        left_layout.addWidget(ops_group, stretch=1)

        # Presets
        preset_group = QGroupBox("Presets")
        preset_layout = QVBoxLayout(preset_group)

        preset_top = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        preset_top.addWidget(self.preset_combo, stretch=1)
        self.preset_name = QLineEdit()
        self.preset_name.setPlaceholderText("Preset name")
        preset_top.addWidget(self.preset_name, stretch=1)
        preset_layout.addLayout(preset_top)

        preset_btn_row = QHBoxLayout()
        self.save_preset_btn = QPushButton("Save")
        self.load_preset_btn = QPushButton("Load")
        self.delete_preset_btn = QPushButton("Delete")
        self.default_preset_btn = QPushButton("Set as Default")
        preset_btn_row.addWidget(self.save_preset_btn)
        preset_btn_row.addWidget(self.load_preset_btn)
        preset_btn_row.addWidget(self.delete_preset_btn)
        preset_btn_row.addWidget(self.default_preset_btn)
        preset_btn_row.addStretch()
        preset_layout.addLayout(preset_btn_row)
        left_layout.addWidget(preset_group)

        # Action buttons — extra top margin to separate from presets
        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 10, 6, 0)
        self.rename_btn = QPushButton("  Rename All  ")
        self.rename_btn.setStyleSheet(
            "QPushButton {"
            "  font-weight: bold; font-size: 14px;"
            "  padding: 8px 20px;"
            "  background-color: #2d89ef; color: #ffffff;"
            "  border: none; border-radius: 3px;"
            "}"
            "QPushButton:hover { background-color: #1b6ec2; }"
            "QPushButton:pressed { background-color: #155a9e; }"
        )
        self.rename_btn.setMinimumHeight(44)
        self.undo_btn = QPushButton("Undo Last (0)")
        self.undo_btn.setMinimumHeight(44)
        self.undo_btn.setMinimumWidth(120)
        action_row.addWidget(self.rename_btn, stretch=1)
        action_row.addWidget(self.undo_btn)
        left_layout.addLayout(action_row)

        splitter.addWidget(left)

        # ---- RIGHT PANEL ----
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 0, 0, 0)
        right_layout.setSpacing(6)

        preview_header = QHBoxLayout()
        preview_header.setContentsMargins(0, 2, 0, 4)
        preview_lbl = QLabel("Preview")
        preview_lbl.setStyleSheet("font-weight: bold; font-size: 13px;")
        preview_lbl.setMinimumHeight(28)
        preview_header.addWidget(preview_lbl)
        self.refresh_btn = QPushButton("Refresh from Resolve")
        self.refresh_btn.setMinimumHeight(28)
        preview_header.addWidget(self.refresh_btn)
        self.collision_label = QLabel("")
        preview_header.addWidget(self.collision_label, stretch=1)
        right_layout.addLayout(preview_header)

        self.preview_tree = QTreeWidget()
        self.preview_tree.setHeaderLabels(["Type", "Original Name", "New Name"])
        self.preview_tree.setAlternatingRowColors(True)
        self.preview_tree.setRootIsDecorated(False)
        self.preview_tree.setColumnWidth(0, 90)
        self.preview_tree.setColumnWidth(1, 240)
        self.preview_tree.setColumnWidth(2, 240)
        right_layout.addWidget(self.preview_tree)

        splitter.addWidget(right)
        # Left panel stays fixed when resizing the window; only preview grows.
        # The splitter handle still allows manual resize of the left panel.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        # Set explicit initial sizes so there's no gap on startup
        splitter.setSizes([400, 1000])

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self):
        self.op_type_combo.currentIndexChanged.connect(self._on_op_type_changed)
        self.add_op_btn.clicked.connect(self._on_add_operation)
        self.cancel_edit_btn.clicked.connect(self._on_cancel_edit)
        self.move_up_btn.clicked.connect(self._on_move_up)
        self.move_down_btn.clicked.connect(self._on_move_down)
        self.edit_op_btn.clicked.connect(self._on_edit_operation)
        self.clear_ops_btn.clicked.connect(self._on_clear_operations)
        self.ops_list.itemDoubleClicked.connect(self._on_ops_double_click)
        self.ops_list.order_changed.connect(self._on_ops_reordered)
        self.rename_btn.clicked.connect(self._on_rename)
        self.undo_btn.clicked.connect(self._on_undo)
        self.save_preset_btn.clicked.connect(self._on_save_preset)
        self.load_preset_btn.clicked.connect(self._on_load_preset)
        self.delete_preset_btn.clicked.connect(self._on_delete_preset)
        self.default_preset_btn.clicked.connect(self._on_default_preset)
        self.preset_combo.currentIndexChanged.connect(self._update_default_button)
        self.refresh_btn.clicked.connect(self._on_refresh_preview)
        self.include_subfolders.stateChanged.connect(self._on_refresh_preview)
        for cb in self.filter_checks.values():
            cb.stateChanged.connect(self._on_filter_changed)

    # ------------------------------------------------------------------
    # Initial state
    # ------------------------------------------------------------------

    def _initial_state(self):
        self._refresh_preset_combo()
        self._on_op_type_changed()

        # Load default preset
        if self.default_preset:
            for i, preset in enumerate(self.presets):
                if preset.name == self.default_preset:
                    self.preset_combo.setCurrentIndex(i)
                    self._apply_preset(preset)
                    print(f"Loaded default preset: {preset.name}")
                    break

        self._update_default_button()
        if not self.cached_items:
            self.cached_items = self._fetch_items()
            self._update_preview()

    # ------------------------------------------------------------------
    # Filter helpers
    # ------------------------------------------------------------------

    def _get_filter_state(self) -> dict:
        filters = {}
        for cb_id, label, _ in CLIP_TYPE_FILTERS:
            filters[cb_id] = self.filter_checks[label].isChecked()
        filters["FilterOther"] = self.filter_checks["Other"].isChecked()
        return filters

    def _apply_filter_state(self, filters: dict, include_subfolders: bool = False):
        for cb_id, label, _ in CLIP_TYPE_FILTERS:
            if cb_id in filters:
                self.filter_checks[label].setChecked(filters[cb_id])
        if "FilterOther" in filters:
            self.filter_checks["Other"].setChecked(filters["FilterOther"])
        self.include_subfolders.setChecked(include_subfolders)

    def _get_active_type_labels(self) -> set:
        active = set()
        for _, label, _ in CLIP_TYPE_FILTERS:
            if self.filter_checks[label].isChecked():
                active.add(label)
        if self.filter_checks["Other"].isChecked():
            active.add("Other")
        return active

    def _filter_items(self, items: list) -> list:
        active = self._get_active_type_labels()
        return [i for i in items if i["type_label"] in active]

    # ------------------------------------------------------------------
    # Fetch items from Resolve
    # ------------------------------------------------------------------

    def _fetch_items(self) -> list:
        return get_media_pool_items(self.project, self.include_subfolders.isChecked())

    # ------------------------------------------------------------------
    # Operations list (QListWidget with drag-drop)
    # ------------------------------------------------------------------

    def _refresh_ops_list(self):
        self.ops_list.clear()
        for i, op in enumerate(self.operations):
            item = QListWidgetItem(self.ops_list)
            widget = OperationRowWidget(i, op)
            widget.delete_clicked.connect(self._on_delete_op_row)
            widget.toggled.connect(self._on_op_toggled)
            item.setSizeHint(widget.sizeHint())
            self.ops_list.setItemWidget(item, widget)

    def _on_delete_op_row(self, index: int):
        if 0 <= index < len(self.operations):
            self.operations.pop(index)
            self._refresh_ops_list()
            self._update_preview()

    def _on_op_toggled(self, index: int, enabled: bool):
        if 0 <= index < len(self.operations):
            self.operations[index].enabled = enabled
            self._update_preview()

    def _on_ops_reordered(self):
        """Sync self.operations to match the new visual order after drag-drop."""
        new_ops = []
        for i in range(self.ops_list.count()):
            item = self.ops_list.item(i)
            widget = self.ops_list.itemWidget(item)
            if widget and hasattr(widget, "op"):
                new_ops.append(widget.op)
        if len(new_ops) == len(self.operations):
            self.operations = new_ops
        self._refresh_ops_list()
        self._update_preview()

    def _get_selected_op_index(self) -> Optional[int]:
        row = self.ops_list.currentRow()
        if row < 0 or row >= len(self.operations):
            return None
        return row

    # ------------------------------------------------------------------
    # Preset helpers
    # ------------------------------------------------------------------

    def _refresh_preset_combo(self):
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for preset in self.presets:
            self.preset_combo.addItem(preset.name)
        self.preset_combo.blockSignals(False)

    def _apply_preset(self, preset: RenamePreset):
        self.operations = [deserialize_op(d) for d in preset.operations]
        if preset.filters:
            self._apply_filter_state(preset.filters, preset.include_subfolders)
        self._refresh_ops_list()
        self.cached_items = self._fetch_items()
        self._update_preview()
        self._update_default_button()

    def _update_default_button(self):
        idx = self.preset_combo.currentIndex()
        if 0 <= idx < len(self.presets):
            if self.presets[idx].name == self.default_preset:
                self.default_preset_btn.setText("Remove Default")
                return
        self.default_preset_btn.setText("Set as Default")

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    def _update_preview(self):
        self.preview_tree.clear()
        self.collision_label.setText("")
        self.collision_label.setStyleSheet("")

        if not self.cached_items:
            self.cached_items = self._fetch_items()

        filtered = self._filter_items(self.cached_items)
        if not filtered:
            return

        if not self.operations:
            for info in filtered:
                QTreeWidgetItem(self.preview_tree,
                                [info["type_label"], info["name"], info["name"]])
            return

        now = datetime.now()
        new_names = []
        for info in filtered:
            old = info["name"]
            new = apply_pipeline(old, self.operations, now)
            new_names.append((info["type_label"], old, new))

        name_counts = Counter(n for _, _, n in new_names)
        collisions = {n for n, c in name_counts.items() if c > 1}
        collision_count = sum(1 for _, _, n in new_names if n in collisions)

        if collisions:
            self.collision_label.setText(
                f"WARNING: {len(collisions)} name collision(s) "
                f"affecting {collision_count} items"
            )
            self.collision_label.setStyleSheet("color: #FF6B6B; font-weight: bold;")

        for type_label, old, new in new_names:
            display_new = new + "  [COLLISION]" if new in collisions else new
            item = QTreeWidgetItem(self.preview_tree, [type_label, old, display_new])
            if new in collisions:
                item.setForeground(2, QColor("#FF6B6B"))

    # ------------------------------------------------------------------
    # Events: operation type changed
    # ------------------------------------------------------------------

    def _on_op_type_changed(self):
        ot = self.op_type_combo.currentText()
        is_search = ot == OpType.SEARCH_REPLACE.value
        is_affix = ot in (OpType.PREFIX.value, OpType.SUFFIX.value)
        is_remove = ot in (OpType.REMOVE_START.value, OpType.REMOVE_END.value,
                           OpType.REMOVE_POSITION.value)
        is_pos = ot == OpType.REMOVE_POSITION.value

        self.search_container.setVisible(is_search)
        self.affix_container.setVisible(is_affix)
        self.remove_container.setVisible(is_remove)
        self.position_label.setVisible(is_pos)
        self.remove_position.setVisible(is_pos)

    # ------------------------------------------------------------------
    # Events: filter changed
    # ------------------------------------------------------------------

    def _on_filter_changed(self):
        self._update_preview()

    def _on_refresh_preview(self):
        self.cached_items = self._fetch_items()
        self._update_preview()

    # ------------------------------------------------------------------
    # Events: add / edit operation
    # ------------------------------------------------------------------

    def _build_op_from_fields(self) -> Optional[RenameOp]:
        try:
            op_type = OpType(self.op_type_combo.currentText())
        except ValueError:
            return None

        op = RenameOp(op_type=op_type)
        if op_type == OpType.SEARCH_REPLACE:
            op.search_text = self.search_field.text()
            op.replace_text = self.replace_field.text()
            op.case_sensitive = self.case_sensitive.isChecked()
            op.use_regex = self.use_regex.isChecked()
            if not op.search_text:
                print("Search field is empty.")
                return None
        elif op_type in (OpType.PREFIX, OpType.SUFFIX):
            op.affix_text = self.affix_field.text()
            if not op.affix_text:
                print("Text field is empty.")
                return None
        elif op_type in (OpType.REMOVE_START, OpType.REMOVE_END):
            op.remove_count = self.remove_count.value()
        elif op_type == OpType.REMOVE_POSITION:
            op.remove_count = self.remove_count.value()
            op.remove_position = self.remove_position.value()
        return op

    def _on_add_operation(self):
        op = self._build_op_from_fields()
        if op is None:
            return
        if self.editing_index is not None:
            self.operations[self.editing_index] = op
            self._exit_edit_mode()
        else:
            self.operations.append(op)
        self._refresh_ops_list()
        self._update_preview()

    def _enter_edit_mode(self, idx: int):
        op = self.operations[idx]
        self.editing_index = idx
        for i, ot in enumerate(OpType):
            if ot == op.op_type:
                self.op_type_combo.setCurrentIndex(i)
                break
        self.search_field.setText(op.search_text)
        self.replace_field.setText(op.replace_text)
        self.case_sensitive.setChecked(op.case_sensitive)
        self.use_regex.setChecked(op.use_regex)
        self.affix_field.setText(op.affix_text)
        self.remove_count.setValue(op.remove_count)
        self.remove_position.setValue(op.remove_position)
        self.add_op_btn.setText("Apply Edit")
        self.cancel_edit_btn.setEnabled(True)
        self._on_op_type_changed()

    def _exit_edit_mode(self):
        self.editing_index = None
        self.add_op_btn.setText("Add Operation")
        self.cancel_edit_btn.setEnabled(False)

    def _on_cancel_edit(self):
        self._exit_edit_mode()

    def _on_edit_operation(self):
        idx = self._get_selected_op_index()
        if idx is not None:
            self._enter_edit_mode(idx)

    def _on_ops_double_click(self, item):
        idx = self.ops_list.row(item)
        if 0 <= idx < len(self.operations):
            self._enter_edit_mode(idx)

    # ------------------------------------------------------------------
    # Events: pipeline management
    # ------------------------------------------------------------------

    def _on_move_up(self):
        idx = self._get_selected_op_index()
        if idx is None or idx == 0:
            return
        self.operations[idx], self.operations[idx - 1] = (
            self.operations[idx - 1], self.operations[idx])
        self._refresh_ops_list()
        self.ops_list.setCurrentRow(idx - 1)
        self._update_preview()

    def _on_move_down(self):
        idx = self._get_selected_op_index()
        if idx is None or idx >= len(self.operations) - 1:
            return
        self.operations[idx], self.operations[idx + 1] = (
            self.operations[idx + 1], self.operations[idx])
        self._refresh_ops_list()
        self.ops_list.setCurrentRow(idx + 1)
        self._update_preview()

    def _on_clear_operations(self):
        self.operations.clear()
        self._refresh_ops_list()
        self._update_preview()

    # ------------------------------------------------------------------
    # Events: rename
    # ------------------------------------------------------------------

    def _on_rename(self):
        if not self.operations:
            print("No operations to apply.")
            return
        all_items = self._fetch_items()
        items = self._filter_items(all_items)
        if not items:
            print("No items found to rename.")
            return

        now = datetime.now()
        new_names = [apply_pipeline(i["name"], self.operations, now) for i in items]
        name_counts = Counter(new_names)
        collisions = {n for n, c in name_counts.items() if c > 1}
        if collisions:
            print(f"WARNING: {len(collisions)} name collision(s) detected. Proceeding anyway.")

        undo_record = execute_rename(items, self.operations)
        if undo_record:
            self.undo_stack.append(undo_record)
            if len(self.undo_stack) > MAX_UNDO:
                self.undo_stack.pop(0)
            print(f"Renamed {len(undo_record.mappings)} items.")
        else:
            print("No items were renamed (no changes detected).")

        self.undo_btn.setText(f"Undo Last ({len(self.undo_stack)})")
        self.cached_items = self._fetch_items()
        self._update_preview()

    # ------------------------------------------------------------------
    # Events: undo
    # ------------------------------------------------------------------

    def _on_undo(self):
        if not self.undo_stack:
            print("Nothing to undo.")
            return
        record = self.undo_stack.pop()
        reverted = execute_undo(record, self.project)
        print(f"Undid rename: reverted {reverted} items.")
        self.undo_btn.setText(f"Undo Last ({len(self.undo_stack)})")
        self.cached_items = self._fetch_items()
        self._update_preview()

    # ------------------------------------------------------------------
    # Events: presets
    # ------------------------------------------------------------------

    def _on_save_preset(self):
        name = self.preset_name.text().strip()
        if not name:
            print("Enter a preset name first.")
            return
        serialized_ops = [serialize_op(op) for op in self.operations]
        filters = self._get_filter_state()
        include_sub = self.include_subfolders.isChecked()

        existing = next((p for p in self.presets if p.name == name), None)
        if existing:
            existing.operations = serialized_ops
            existing.filters = filters
            existing.include_subfolders = include_sub
        else:
            self.presets.append(RenamePreset(
                name=name, operations=serialized_ops,
                filters=filters, include_subfolders=include_sub))
        save_presets(self.presets)
        self._refresh_preset_combo()
        self._update_default_button()
        print(f"Saved preset: {name}")

    def _on_load_preset(self):
        idx = self.preset_combo.currentIndex()
        if 0 <= idx < len(self.presets):
            self._apply_preset(self.presets[idx])
            print(f"Loaded preset: {self.presets[idx].name}")

    def _on_delete_preset(self):
        idx = self.preset_combo.currentIndex()
        if idx < 0 or idx >= len(self.presets):
            return
        name = self.presets[idx].name
        if name == self.default_preset:
            self.default_preset = ""
            save_default_preset_name("")
        self.presets.pop(idx)
        save_presets(self.presets)
        self._refresh_preset_combo()
        self._update_default_button()
        print(f"Deleted preset: {name}")

    def _on_default_preset(self):
        idx = self.preset_combo.currentIndex()
        if idx < 0 or idx >= len(self.presets):
            return
        name = self.presets[idx].name
        if name == self.default_preset:
            self.default_preset = ""
            save_default_preset_name("")
            print("Default preset cleared.")
        else:
            self.default_preset = name
            save_default_preset_name(name)
            print(f"Default preset set to: {name}")
        self._update_default_button()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    project = resolve.GetProjectManager().GetCurrentProject()  # noqa: F821
    if not project:
        print("Error: No project is open.")
        return

    print("=== Batch Rename loaded ===")

    app = QApplication.instance()
    standalone = app is None
    if standalone:
        app = QApplication(sys.argv)

    dlg = BatchRenameDialog(project)
    dlg.show()

    if standalone:
        app.exec()
    else:
        # Resolve already has a Qt event loop — run as modal dialog
        dlg.exec()

    print("=== Batch Rename closed ===")


main()
