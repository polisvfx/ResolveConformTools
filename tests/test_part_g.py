"""
Part G test: tools/make_release_zip.py.

The release builder gates every tag: it decides what ships, refuses a script
with no Version header, and writes the version table that heads the GitHub
release body. None of that had coverage, and a mistake in it is only visible
after a tag is already pushed.

Runs the real script as a subprocess against a throwaway repo tree, so the
argument parsing and the exit codes are exercised as CI actually calls them.

No Resolve needed.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile


def _repo_root():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        cwd = os.path.abspath(os.getcwd())
        here = cwd if os.path.basename(cwd) == "tests" else os.path.join(cwd, "tests")
    return os.path.dirname(here)


REPO = _repo_root()
BUILDER = os.path.join(REPO, "tools", "make_release_zip.py")

fails = []


def check(label, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"       got  {got!r}")
        print(f"       want {want!r}")
        fails.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def make_tree(scripts, extras=("README.md", "LICENSE")):
    """A throwaway repo: tools/make_release_zip.py plus the given root files."""
    root = tempfile.mkdtemp(prefix="rct_release_test_")
    os.makedirs(os.path.join(root, "tools"))
    shutil.copy(BUILDER, os.path.join(root, "tools", "make_release_zip.py"))
    for name, body in scripts.items():
        with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
            fh.write(body)
    for name in extras:
        with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
            fh.write("# readme\n")
    return root


def run(root, *args):
    result = subprocess.run(
        [sys.executable, os.path.join(root, "tools", "make_release_zip.py")] + list(args),
        capture_output=True, text=True, cwd=root,
    )
    return result.returncode, result.stdout + result.stderr


PY = '"""Thing\n\nVersion: 2.5\n"""\n'
LUA = "--[[\nThing\nVersion: 1.4\n]]\n"
NO_VERSION = '"""Thing with no version header."""\n'


# ---------------------------------------------------------------------------
print("\n== a normal build ==")

root = make_tree({"Alpha.py": PY, "Beta.lua": LUA})
try:
    code, out = run(root, "1.2.3", "--out", "dist")
    check("it succeeds", code, 0)

    zip_path = os.path.join(root, "dist", "ResolveConformTools-1.2.3.zip")
    check_true("it writes the zip named for the version", os.path.isfile(zip_path))

    with zipfile.ZipFile(zip_path) as archive:
        names = sorted(archive.namelist())
        manifest = archive.read("ResolveConformTools/VERSIONS.txt").decode("utf-8")

    check("everything lands under one folder",
          [n for n in names if not n.startswith("ResolveConformTools/")], [])
    check("it ships the scripts, the README, the LICENSE and the manifest",
          names,
          ["ResolveConformTools/Alpha.py", "ResolveConformTools/Beta.lua",
           "ResolveConformTools/LICENSE", "ResolveConformTools/README.md",
           "ResolveConformTools/VERSIONS.txt"])

    check_true("the manifest names the release", "ResolveConformTools 1.2.3" in manifest)
    check_true("and records each script's own version", "Alpha.py" in manifest
               and "2.5" in manifest)
    check_true("including the Lua one", "Beta.lua" in manifest and "1.4" in manifest)
finally:
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
print("\n== a script with no Version header ==")

root = make_tree({"Alpha.py": PY, "Nameless.py": NO_VERSION})
try:
    code, out = run(root, "1.2.3", "--out", "dist")
    check("the build fails rather than shipping it", code, 1)
    check_true("and says which script is at fault", "Nameless.py" in out)
    check_true("no zip is written",
               not os.path.isfile(os.path.join(root, "dist",
                                               "ResolveConformTools-1.2.3.zip")))
finally:
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
print("\n== a missing README or LICENSE ==")

root = make_tree({"Alpha.py": PY}, extras=())
try:
    code, out = run(root, "1.2.3", "--out", "dist")
    check("the build fails", code, 1)
    check_true("and names what is missing", "README.md" in out)
finally:
    shutil.rmtree(root, ignore_errors=True)

# The GPL requires every copy to carry the licence, and the zip is a copy, so a
# missing LICENSE has to stop the release rather than quietly ship without it.
root = make_tree({"Alpha.py": PY}, extras=("README.md",))
try:
    code, out = run(root, "1.2.3", "--out", "dist")
    check("a build with no LICENSE fails too", code, 1)
    check_true("and says which file is missing", "LICENSE" in out)
    check_true("and writes no zip",
               not os.path.isfile(os.path.join(root, "dist",
                                               "ResolveConformTools-1.2.3.zip")))
finally:
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
print("\n== version argument handling ==")

root = make_tree({"Alpha.py": PY})
try:
    for bad in ("1.2", "v", "banana", "1.2.3.4"):
        code, _out = run(root, bad, "--out", "dist")
        check(f"{bad!r} is refused", code, 2)   # argparse.error exits 2

    # A leading v is tolerated, because the tag carries one.
    code, _out = run(root, "v1.2.3", "--out", "dist")
    check("a leading 'v' is stripped, not rejected", code, 0)
    check_true("and the zip is named without it",
               os.path.isfile(os.path.join(root, "dist",
                                           "ResolveConformTools-1.2.3.zip")))
finally:
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
print("\n== the release notes header ==")

root = make_tree({"Alpha.py": PY, "Beta.lua": LUA})
try:
    code, _out = run(root, "1.2.3", "--out", "dist", "--notes-out", "dist/notes.md")
    check("it succeeds", code, 0)
    notes_path = os.path.join(root, "dist", "notes.md")
    check_true("the notes header is written", os.path.isfile(notes_path))
    with open(notes_path, encoding="utf-8") as fh:
        notes = fh.read()
    check_true("it names the zip to download",
               "ResolveConformTools-1.2.3.zip" in notes)
    check_true("it lists every script and version",
               "`Alpha.py` | 2.5" in notes and "`Beta.lua` | 1.4" in notes)
    check_true("and it tells the user where to unzip",
               "Scripts\\Utility" in notes or "Scripts/Utility" in notes)
    # The literal %PROGRAMDATA% must survive: it sits in a line that is NOT
    # %-formatted, and turning it into one would raise at build time.
    check_true("the Windows path keeps its %PROGRAMDATA% token",
               "%PROGRAMDATA%" in notes)
finally:
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
print("\n== rebuilding over an existing zip ==")

root = make_tree({"Alpha.py": PY})
try:
    run(root, "1.2.3", "--out", "dist")
    code, _out = run(root, "1.2.3", "--out", "dist")
    check("a second build of the same version succeeds", code, 0)
    with zipfile.ZipFile(os.path.join(root, "dist",
                                      "ResolveConformTools-1.2.3.zip")) as archive:
        check("and does not duplicate entries",
              len(archive.namelist()), len(set(archive.namelist())))
finally:
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
print("")
if fails:
    print(f"RESULT: FAIL ({len(fails)} failed)")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
