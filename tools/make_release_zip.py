#!/usr/bin/env python3
"""Build the downloadable zip for a ResolveConformTools release.

Collects the scripts a user actually installs -- every ``*.py`` and ``*.lua`` at
the repo root, plus the README and the LICENSE -- and leaves the test suite, the
CI config and the repo conventions behind. The licence ships because the GPL
requires every copy to carry it, and the zip is a copy. Everything lands under a single ``ResolveConformTools/``
folder inside the zip, so extracting it into Resolve's ``Scripts/Utility``
directory produces exactly the layout a git checkout would.

Every root script must carry a ``Version: X.Y`` header (see CLAUDE.md); a script
without one aborts the build rather than shipping unlabelled.

``--notes-out`` additionally writes the install instructions and the script
version table that head the GitHub release body, built from the same data as the
zip so the two can never disagree.

Usage:
    python tools/make_release_zip.py 1.0.0
    python tools/make_release_zip.py 1.0.0 --out dist --notes-out dist/notes.md
"""

import argparse
import datetime
import re
import subprocess
import sys
import zipfile
from pathlib import Path

PACKAGE_NAME = "ResolveConformTools"
SCRIPT_SUFFIXES = (".py", ".lua")
EXTRA_FILES = ("README.md", "LICENSE")
MANIFEST_NAME = "VERSIONS.txt"

VERSION_HEADER = re.compile(r"Version:\s*(\d+\.\d+)")
RELEASE_VERSION = re.compile(r"^\d+\.\d+\.\d+$")

ROOT = Path(__file__).resolve().parent.parent


def script_version(path):
    """Return the ``Version: X.Y`` value from a script's header, or None."""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        head = handle.read(4000)
    match = VERSION_HEADER.search(head)
    return match.group(1) if match else None


def collect_scripts():
    """Every installable script at the repo root, sorted by name."""
    return sorted(
        (p for p in ROOT.iterdir() if p.is_file() and p.suffix in SCRIPT_SUFFIXES),
        key=lambda p: p.name.lower(),
    )


def git_commit():
    """Short commit hash of the checkout, or None outside a git tree."""
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def build_manifest(version, scripts, versions):
    """The VERSIONS.txt that ships inside the zip."""
    lines = [
        "%s %s" % (PACKAGE_NAME, version),
        "Built %s" % datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
    ]
    commit = git_commit()
    if commit:
        lines.append("Commit %s" % commit)
    lines.append("")
    lines.append("Script versions in this release:")
    lines.append("")
    width = max(len(p.name) for p in scripts)
    for path in scripts:
        lines.append("  %-*s  %s" % (width, path.name, versions[path.name]))
    lines.append("")
    return "\n".join(lines)


def build_notes(version, scripts, versions):
    """The header of the GitHub release body, above the generated changelog."""
    zip_name = "%s-%s.zip" % (PACKAGE_NAME, version)
    lines = [
        "## Install",
        "",
        "Download **`%s`** below and unzip it into Resolve's Utility scripts folder,"
        % zip_name,
        "so that the scripts end up in a `%s` subfolder:" % PACKAGE_NAME,
        "",
        "| | |",
        "|---|---|",
        "| Windows | `%PROGRAMDATA%\\Blackmagic Design\\DaVinci Resolve\\Fusion\\Scripts\\Utility` |",
        "| macOS | `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility` |",
        "| Linux | `/opt/resolve/Fusion/Scripts/Utility` |",
        "",
        "They then appear under **Workspace > Scripts > %s**." % PACKAGE_NAME,
        "Restart Resolve, or re-open the menu, to pick up new files.",
        "",
        "The `Source code` archives below are the whole repo including the test",
        "suite; the zip is what you want for installing.",
        "",
        "## Script versions",
        "",
        "| Script | Version |",
        "|---|---|",
    ]
    for path in scripts:
        lines.append("| `%s` | %s |" % (path.name, versions[path.name]))
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="release version, e.g. 1.0.0 (no leading v)")
    parser.add_argument(
        "--out",
        default="dist",
        help="output directory, relative to the repo root (default: dist)",
    )
    parser.add_argument(
        "--notes-out",
        default=None,
        help="also write the release-body header to this path",
    )
    args = parser.parse_args()

    version = args.version.lstrip("v")
    if not RELEASE_VERSION.match(version):
        parser.error("version must look like X.Y.Z, got %r" % args.version)

    scripts = collect_scripts()
    if not scripts:
        print("No scripts found at %s" % ROOT, file=sys.stderr)
        return 1

    versions = {}
    unversioned = []
    for path in scripts:
        found = script_version(path)
        if found is None:
            unversioned.append(path.name)
        versions[path.name] = found
    if unversioned:
        print(
            "These scripts have no 'Version: X.Y' header and cannot be released:",
            file=sys.stderr,
        )
        for name in unversioned:
            print("  %s" % name, file=sys.stderr)
        return 1

    missing_extras = [name for name in EXTRA_FILES if not (ROOT / name).is_file()]
    if missing_extras:
        print("Missing from the repo root: %s" % ", ".join(missing_extras), file=sys.stderr)
        return 1

    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / ("%s-%s.zip" % (PACKAGE_NAME, version))
    if zip_path.exists():
        zip_path.unlink()

    manifest = build_manifest(version, scripts, versions)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in scripts:
            archive.write(path, "%s/%s" % (PACKAGE_NAME, path.name))
        for name in EXTRA_FILES:
            archive.write(ROOT / name, "%s/%s" % (PACKAGE_NAME, name))
        archive.writestr("%s/%s" % (PACKAGE_NAME, MANIFEST_NAME), manifest)

    print(manifest)
    print(
        "Wrote %s (%d files, %.1f KB)"
        % (zip_path, len(scripts) + len(EXTRA_FILES) + 1, zip_path.stat().st_size / 1024)
    )

    if args.notes_out:
        notes_path = Path(args.notes_out)
        if not notes_path.is_absolute():
            notes_path = ROOT / notes_path
        notes_path.parent.mkdir(parents=True, exist_ok=True)
        notes_path.write_text(build_notes(version, scripts, versions), encoding="utf-8")
        print("Wrote %s" % notes_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
