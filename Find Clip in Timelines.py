"""
Find Clip in Timelines.py
─────────────────────────
Searches all timelines in the current project for the selected clip and
shows a popup listing the timelines that contain it.

Double-click a timeline in the popup to switch to it; the playhead will
jump (best-effort) to the in-point of the clip's first occurrence on
that timeline.

Source detection priority:
  1. Selected item in the active Timeline (video tracks only — Resolve's
     GetCurrentVideoItem())
  2. Selected clip in the Media Pool

Run from:  Workspace ▸ Scripts  – or –  Workspace ▸ Console (exec/run)
"""

import tkinter as tk
from tkinter import messagebox


# ─── Resolve bootstrapping ────────────────────────────────────────────────────

def get_resolve():
    try:
        import DaVinciResolveScript as dvr
        return dvr.scriptapp("Resolve")
    except ImportError:
        pass
    # Console context: bmd is already in globals
    if "bmd" in dir(__builtins__) or "bmd" in globals():
        return bmd.scriptapp("Resolve")          # noqa: F821
    # Last resort: try the global injected by Resolve's script runner
    import builtins
    if hasattr(builtins, "bmd"):
        return builtins.bmd.scriptapp("Resolve")
    raise RuntimeError("Could not obtain Resolve handle. "
                       "Run this script from inside DaVinci Resolve.")


# ─── Clip detection ───────────────────────────────────────────────────────────

def get_target_clip(project):
    """
    Returns (media_pool_item, source_label) or (None, reason_string).

    Priority:
      1. Current video item selected on the active timeline
      2. Selected clip(s) in the Media Pool
    """
    tl = project.GetCurrentTimeline()

    # 1 – Try timeline selection first
    if tl:
        current_item = tl.GetCurrentVideoItem()
        if current_item:
            mpi = current_item.GetMediaPoolItem()
            if mpi:
                return mpi, f"Timeline selection  [{tl.GetName()}]"

    # 2 – Fall back to Media Pool selection
    mp = project.GetMediaPool()
    selected = mp.GetSelectedMediaPoolItems()
    if selected:
        return selected[0], "Media Pool selection"

    return None, "No clip selected in Timeline or Media Pool"


# ─── Search ───────────────────────────────────────────────────────────────────

def find_timelines_for_clip(project, target_id):
    """
    Returns a list of dicts:
      {
        "name":     str,
        "timeline": Timeline,
        "hits": [
            {
              "track_type":  "video" | "audio",
              "track":       int,
              "index":       int,   # 1-based item index on the track
              "start_frame": int,   # absolute timeline frame
            }, ...
        ]
      }
    """
    results = []
    tl_count = project.GetTimelineCount()

    for i in range(1, tl_count + 1):
        tl = project.GetTimelineByIndex(i)
        hits = []

        for track_type in ("video", "audio"):
            track_count = tl.GetTrackCount(track_type)
            for t in range(1, track_count + 1):
                items = tl.GetItemListInTrack(track_type, t) or []
                for idx, item in enumerate(items):
                    mpi = item.GetMediaPoolItem()
                    if mpi and mpi.GetUniqueId() == target_id:
                        hits.append({
                            "track_type":  track_type,
                            "track":       t,
                            "index":       idx + 1,
                            "start_frame": int(item.GetStart()),
                        })

        if hits:
            results.append({"name": tl.GetName(), "timeline": tl, "hits": hits})

    return results


# ─── Timecode helper ──────────────────────────────────────────────────────────

def frames_to_timecode(frame, fps, drop_frame=False):
    """
    Convert an absolute timeline frame to "HH:MM:SS:FF" (or ";FF" when DF).
    Returns None if inputs can't be parsed — playhead jump is best-effort.
    """
    try:
        frame = int(frame)
        nominal = int(round(float(fps)))
        if nominal <= 0:
            return None

        if drop_frame and nominal in (30, 60):
            drop = 2 if nominal == 30 else 4
            fpm  = nominal * 60 - drop          # frames per minute
            fp10 = fpm * 10 + drop              # frames per 10 minutes

            d, m = divmod(frame, fp10)
            if m > drop:
                m += drop * ((m - drop) // fpm)
            adj = d * (nominal * 600) + m

            f  = adj % nominal
            s  = (adj // nominal) % 60
            mn = ((adj // nominal) // 60) % 60
            h  = ((adj // nominal) // 3600) % 24
            return f"{h:02d}:{mn:02d}:{s:02d};{f:02d}"

        f  = frame % nominal
        s  = (frame // nominal) % 60
        mn = (frame // (nominal * 60)) % 60
        h  = (frame // (nominal * 3600)) % 24
        return f"{h:02d}:{mn:02d}:{s:02d}:{f:02d}"
    except Exception:
        return None


# ─── UI ───────────────────────────────────────────────────────────────────────

# Resolve-style dark palette
BG_MAIN  = "#2b2b2b"   # window background
BG_PANEL = "#3a3a3a"   # listbox / button background
BG_ALT   = "#323232"   # alternating row tint
FG_TEXT  = "#d8d8d8"   # primary text
FG_MUTED = "#8a8a8a"   # subtitle / secondary text
FG_ERR   = "#ff8080"   # status errors
SEL_BG   = "#5a5a5a"   # selected row background
SEL_FG   = "#ffffff"   # selected row text
BORDER   = "#1a1a1a"   # subtle dividers


def show_popup(project, clip_name, source_label, results):
    root = tk.Tk()
    root.withdraw()                        # hide the blank root window
    root.configure(bg=BG_MAIN)

    win = tk.Toplevel(root)
    win.title("Clip Usage — Find in Timelines")
    win.configure(bg=BG_MAIN)
    win.resizable(False, False)
    win.attributes("-topmost", True)       # float above Resolve

    # ── Header ──
    header = tk.Label(
        win,
        text=f'"{clip_name}"',
        font=("Helvetica", 13, "bold"),
        bg=BG_MAIN, fg=FG_TEXT,
        padx=18, pady=10,
        anchor="w",
        justify="left"
    )
    header.pack(fill="x")

    sub = tk.Label(
        win,
        text=f"Source: {source_label}",
        font=("Helvetica", 10),
        bg=BG_MAIN, fg=FG_MUTED,
        padx=18,
        anchor="w",
        justify="left"
    )
    sub.pack(fill="x")

    separator = tk.Frame(win, height=1, bg=BORDER)
    separator.pack(fill="x", padx=12, pady=(6, 0))

    if not results:
        empty = tk.Label(
            win,
            text="Not found in any timeline.",
            font=("Helvetica", 11),
            bg=BG_MAIN, fg=FG_TEXT,
            padx=18, pady=20,
            anchor="w",
            justify="left"
        )
        empty.pack(fill="x")
    else:
        # ── Timeline list ──
        count_label = tk.Label(
            win,
            text=f"Used in {len(results)} timeline(s)  —  double-click to open:",
            font=("Helvetica", 10, "bold"),
            bg=BG_MAIN, fg=FG_TEXT,
            padx=18,
            anchor="w"
        )
        count_label.pack(fill="x", pady=(10, 4))

        # Scrollable frame for long lists
        frame = tk.Frame(win, bg=BG_MAIN, padx=18, pady=2)
        frame.pack(fill="both", expand=True)

        scrollbar = tk.Scrollbar(
            frame,
            orient="vertical",
            bg=BG_PANEL,
            troughcolor=BG_MAIN,
            activebackground=SEL_BG,
            highlightthickness=0,
            bd=0,
        )
        listbox = tk.Listbox(
            frame,
            yscrollcommand=scrollbar.set,
            font=("Helvetica", 11),
            bg=BG_PANEL, fg=FG_TEXT,
            selectbackground=SEL_BG,
            selectforeground=SEL_FG,
            selectmode="browse",
            activestyle="none",
            relief="flat",
            bd=0,
            highlightthickness=0,
            width=48,
            height=min(len(results), 16)       # cap at 16 rows, then scroll
        )
        scrollbar.config(command=listbox.yview)

        for r in results:
            hit_summary = f"  ({len(r['hits'])}×)" if len(r['hits']) > 1 else ""
            listbox.insert("end", f"  {r['name']}{hit_summary}")

        # Alternate row tint for readability
        for i in range(0, listbox.size(), 2):
            listbox.itemconfig(i, bg=BG_ALT)

        listbox.pack(side="left", fill="both", expand=True)
        if len(results) > 16:
            scrollbar.pack(side="right", fill="y")

        # ── Status line (used for open-attempt errors) ──
        status_var = tk.StringVar(value="")
        status_label = tk.Label(
            win,
            textvariable=status_var,
            font=("Helvetica", 9),
            bg=BG_MAIN, fg=FG_ERR,
            padx=18,
            anchor="w",
            justify="left"
        )
        status_label.pack(fill="x")

        # ── Open handler ──
        def open_selected(event=None):
            selection = listbox.curselection()
            if not selection:
                return
            r  = results[selection[0]]
            tl = r["timeline"]

            if not project.SetCurrentTimeline(tl):
                status_var.set(f'Could not switch to "{r["name"]}".')
                return
            status_var.set("")

            # Best-effort playhead jump to the first occurrence
            try:
                fps   = tl.GetSetting("timelineFrameRate")
                df_in = str(tl.GetSetting("timelineDropFrameTimecode"))
                df    = df_in in ("1", "True", "true")
                tc    = frames_to_timecode(r["hits"][0]["start_frame"], fps, df)
                if tc:
                    tl.SetCurrentTimecode(tc)
            except Exception:
                pass  # non-critical — timeline switch already succeeded

        listbox.bind("<Double-Button-1>", open_selected)
        listbox.bind("<Return>",          open_selected)

    # ── Close button ──
    btn_frame = tk.Frame(win, bg=BG_MAIN, pady=10)
    btn_frame.pack()
    tk.Button(
        btn_frame,
        text="   Close   ",
        command=win.destroy,
        bg=BG_PANEL, fg=FG_TEXT,
        activebackground=SEL_BG,
        activeforeground=SEL_FG,
        relief="flat",
        bd=0,
        padx=12, pady=4,
        highlightthickness=0,
        font=("Helvetica", 10)
    ).pack()

    # Centre window on screen
    win.update_idletasks()
    w  = win.winfo_width()
    h  = win.winfo_height()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.grab_set()
    root.wait_window(win)
    root.destroy()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    try:
        resolve = get_resolve()
    except RuntimeError as e:
        print(f"[ERROR] {e}")
        return

    pm      = resolve.GetProjectManager()
    project = pm.GetCurrentProject()

    if not project:
        print("[ERROR] No project open.")
        return

    # ── Detect clip ──
    target, source_label = get_target_clip(project)

    if target is None:
        print(f"[INFO] {source_label}")
        messagebox.showwarning("Clip Usage", source_label)
        return

    clip_name = target.GetName()
    target_id = target.GetUniqueId()

    print(f"\n{'─'*60}")
    print(f"  Clip    : {clip_name}")
    print(f"  ID      : {target_id}")
    print(f"  Source  : {source_label}")
    print(f"{'─'*60}")

    # ── Search ──
    tl_count = project.GetTimelineCount()
    print(f"  Scanning {tl_count} timeline(s)…")
    results = find_timelines_for_clip(project, target_id)

    # ── Console output ──
    if not results:
        print("\n  Not found in any timeline.\n")
    else:
        print(f"\n  Found in {len(results)} timeline(s):\n")
        for r in results:
            print(f"    ▸  {r['name']}")
            for h in r["hits"]:
                print(f"         {h['track_type']} track {h['track']}, "
                      f"item {h['index']}  (frame {h['start_frame']})")
        print()

    # ── Popup ──
    show_popup(project, clip_name, source_label, results)


main()
