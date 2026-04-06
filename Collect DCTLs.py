"""
Collect LUTs & DCTLs from DaVinci Resolve Timeline
====================================================
Run from Resolve's Console or Script menu.
Iterates all clips across video tracks, reads:
  - Node-level LUTs (corrector nodes via GetLUT)
  - Clip Input LUTs (via MediaPoolItem clip properties)
Presents a checklist for the user to confirm which files to collect,
then copies them to a user-selected output folder, preserving subfolder
structure relative to the system LUT root.

Resolve 18+ required (GetLUT / GetNumNodes API).
"""

import os
import shutil
import platform
import tkinter as tk
from tkinter import filedialog, messagebox


# System LUT roots per OS - adjust if you have custom locations
LUT_ROOTS = {
    "Windows": [
        os.path.join(os.environ.get("PROGRAMDATA", ""), "Blackmagic Design", "DaVinci Resolve", "Support", "LUT"),
        os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "Blackmagic Design", "DaVinci Resolve", "Support", "LUT"),
    ],
    "Darwin": [
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT",
        os.path.expanduser("~/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT"),
    ],
    "Linux": [
        "/opt/resolve/LUT",
        os.path.expanduser("~/.local/share/DaVinci Resolve/LUT"),
    ],
}


def get_lut_roots():
    return LUT_ROOTS.get(platform.system(), [])


def ask_output_folder():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    folder = filedialog.askdirectory(title="Select destination folder for collected LUTs")
    root.destroy()
    return folder if folder else None


def relative_to_root(filepath, roots):
    fp = os.path.normpath(filepath)
    for root in roots:
        root_norm = os.path.normpath(root)
        if fp.lower().startswith(root_norm.lower() + os.sep):
            return os.path.relpath(fp, root_norm), root_norm
    return None, None


def register_lut(abs_path, roots, collected, source_label):
    if not abs_path or not abs_path.strip():
        return False
    abs_path = os.path.normpath(abs_path.strip())
    if abs_path in collected:
        return False
    rel, _ = relative_to_root(abs_path, roots)
    collected[abs_path] = rel if rel else os.path.basename(abs_path)
    print(f"  {source_label}: {abs_path}")
    return True


def collect_luts_from_items(items, roots):
    collected = {}
    for item in items:
        name = item.GetName()

        # Clip Input LUT
        try:
            mp_item = item.GetMediaPoolItem()
            if mp_item:
                clip_props = mp_item.GetClipProperty()
                if clip_props and isinstance(clip_props, dict):
                    input_lut = clip_props.get("Input LUT", "") or clip_props.get("input_lut", "")
                    register_lut(input_lut, roots, collected, f"[{name}] Input LUT")
        except Exception:
            pass

        # Node-level LUTs
        try:
            num_nodes = item.GetNumNodes()
        except Exception:
            continue
        for node_idx in range(1, num_nodes + 1):
            try:
                lut_path = item.GetLUT(node_idx)
            except Exception:
                continue
            register_lut(lut_path, roots, collected, f"[{name}] node {node_idx}")

    return collected


def show_checklist(collected):
    """
    Show a scrollable checklist of found LUT/DCTL files.
    All items checked by default. Returns list of selected absolute paths,
    or None if cancelled.
    """
    result = {"selected": None}

    win = tk.Tk()
    win.title("Collect LUTs – Select files to copy")
    win.attributes("-topmost", True)
    win.minsize(500, 300)

    # --- Header ---
    header = tk.Frame(win)
    header.pack(fill="x", padx=10, pady=(10, 0))
    tk.Label(header, text=f"{len(collected)} LUT/DCTL file(s) found.",
             font=("", 11, "bold")).pack(side="left")

    # --- Select All / None buttons ---
    btn_bar = tk.Frame(win)
    btn_bar.pack(fill="x", padx=10, pady=(4, 0))

    # Build checkbox vars first so buttons can reference them
    check_vars = {}  # abs_path -> BooleanVar

    def select_all():
        for var in check_vars.values():
            var.set(True)

    def select_none():
        for var in check_vars.values():
            var.set(False)

    tk.Button(btn_bar, text="Select All", command=select_all).pack(side="left", padx=(0, 4))
    tk.Button(btn_bar, text="Select None", command=select_none).pack(side="left")

    # --- Scrollable checklist ---
    container = tk.Frame(win)
    container.pack(fill="both", expand=True, padx=10, pady=6)

    canvas = tk.Canvas(container, highlightthickness=0)
    scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
    checklist_frame = tk.Frame(canvas)

    checklist_frame.bind(
        "<Configure>",
        lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
    )
    canvas.create_window((0, 0), window=checklist_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    # Mouse-wheel scrolling
    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_mousewheel_linux(event):
        if event.num == 4:
            canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            canvas.yview_scroll(1, "units")

    canvas.bind_all("<MouseWheel>", _on_mousewheel)        # Windows / macOS
    canvas.bind_all("<Button-4>", _on_mousewheel_linux)     # Linux scroll up
    canvas.bind_all("<Button-5>", _on_mousewheel_linux)     # Linux scroll down

    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    # Populate checkboxes
    for abs_path, rel_path in sorted(collected.items(), key=lambda x: x[1].lower()):
        var = tk.BooleanVar(value=True)
        check_vars[abs_path] = var
        exists = os.path.isfile(abs_path)
        label = rel_path if exists else f"{rel_path}  [NOT FOUND]"
        cb = tk.Checkbutton(checklist_frame, text=label, variable=var, anchor="w",
                            fg="black" if exists else "red")
        cb.pack(fill="x", anchor="w", padx=4, pady=1)

    # --- OK / Cancel ---
    footer = tk.Frame(win)
    footer.pack(fill="x", padx=10, pady=(0, 10))

    def on_ok():
        result["selected"] = [p for p, var in check_vars.items() if var.get()]
        win.destroy()

    def on_cancel():
        result["selected"] = None
        win.destroy()

    tk.Button(footer, text="Collect Selected", command=on_ok, width=16).pack(side="right", padx=(4, 0))
    tk.Button(footer, text="Cancel", command=on_cancel, width=10).pack(side="right")

    win.protocol("WM_DELETE_WINDOW", on_cancel)
    win.mainloop()

    return result["selected"]


def main():
    # --- Connect to Resolve ---
    resolve = None
    try:
        import DaVinciResolveScript as dvr
        resolve = dvr.scriptapp("Resolve")
    except ImportError:
        try:
            resolve = bmd.scriptapp("Resolve")  # noqa: F821
        except Exception:
            pass

    if not resolve:
        print("ERROR: Could not connect to DaVinci Resolve.")
        return

    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    timeline = project.GetCurrentTimeline()
    if not timeline:
        print("ERROR: No timeline is currently open.")
        return

    roots = get_lut_roots()
    print(f"Timeline : {timeline.GetName()}")
    print(f"LUT roots: {roots}\n")

    # --- Gather clips ---
    all_items = []
    track_count = timeline.GetTrackCount("video")
    for t in range(1, track_count + 1):
        items = timeline.GetItemListInTrack("video", t)
        if items:
            all_items.extend(items)

    if not all_items:
        print("No clips found in timeline.")
        return

    print(f"Scanning {len(all_items)} clip(s)...\n")
    collected = collect_luts_from_items(all_items, roots)

    if not collected:
        msg = "No LUTs or DCTLs found on any clip."
        print(f"\n{msg}")
        root = tk.Tk(); root.withdraw()
        messagebox.showinfo("Collect LUTs", msg)
        root.destroy()
        return

    # --- Checklist ---
    selected = show_checklist(collected)
    if selected is None:
        print("Cancelled.")
        return
    if not selected:
        print("No files selected.")
        return

    # --- Folder picker (after selection confirmed) ---
    output = ask_output_folder()
    if not output:
        print("Cancelled.")
        return
    print(f"Output   : {output}\n")

    # --- Copy selected files ---
    ok_count = 0
    miss_count = 0
    for abs_path in selected:
        rel_path = collected[abs_path]
        dest = os.path.join(output, rel_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.isfile(abs_path):
            shutil.copy2(abs_path, dest)
            print(f"  OK   {rel_path}")
            ok_count += 1
        else:
            print(f"  MISS {abs_path}")
            miss_count += 1

    summary = f"{ok_count} file(s) copied to:\n{output}"
    if miss_count:
        summary += f"\n{miss_count} file(s) not found on disk."
    print(f"\n{summary}")
    print("Copy this folder's contents into the matching LUT root on the target machine.")

    root = tk.Tk(); root.withdraw()
    messagebox.showinfo("Collect LUTs – Done", summary)
    root.destroy()


if __name__ == "__main__":
    main()
