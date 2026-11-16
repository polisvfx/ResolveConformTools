"""
Part I test: DCTL Report plugin reporting.

Covers the three dataclass-free functions behind the plugin section of the
report: finding OFX plugin identifiers in a decompressed project body,
parsing Resolve's OFX plugin cache, and deciding found / missing / built-in.

The script calls main() at import (and needs PySide6), so the real functions
are extracted from source via ast and exec'd into one namespace — the test
exercises the shipped source rather than a copy.

The body fixtures reproduce the framing seen in a real export from Resolve
21.2 (2026-11-16): a length-prefixed identifier, a newline, about ten framing
bytes, then "OfxImageEffectContext...".

No Resolve needed.
"""

import __future__
import ast
import html
import os
import re
import sys


def _repo_root():
    """Repo root = the parent of this tests/ directory (see test_part_e)."""
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
SCRIPT = os.path.join(REPO, "DCTL Report.py")
WANTED = ["scan_plugin_ids", "parse_ofx_plugin_cache", "classify_plugin"]

_SELF_CONTAINED_NODES = (
    ast.Constant, ast.BinOp, ast.UnaryOp, ast.Tuple, ast.List, ast.Set, ast.Dict,
    ast.operator, ast.unaryop, ast.expr_context,
)


def _is_self_contained(node):
    return all(isinstance(sub, _SELF_CONTAINED_NODES) for sub in ast.walk(node))


def extract(path, names):
    """Pull the named top-level functions plus every self-contained constant."""
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    consts = [n for n in tree.body
              if isinstance(n, ast.Assign) and len(n.targets) == 1
              and isinstance(n.targets[0], ast.Name)
              and n.targets[0].id.isupper()
              and _is_self_contained(n.value)]
    nodes = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    missing = set(names) - {n.name for n in nodes}
    if missing:
        raise SystemExit(f"not found in source: {sorted(missing)}")
    ns = {"print": print, "os": os, "re": re, "html": html}
    module = ast.Module(body=consts + nodes, type_ignores=[])
    exec(compile(module, path, "exec",
                 flags=__future__.annotations.compiler_flag), ns)
    for n in nodes:
        print(f"  extracted {n.name}()  lines {n.lineno}-{n.end_lineno}")
    return ns


ns = extract(SCRIPT, WANTED)
scan_plugin_ids = ns["scan_plugin_ids"]
parse_ofx_plugin_cache = ns["parse_ofx_plugin_cache"]
classify_plugin = ns["classify_plugin"]

fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"         got:  {got!r}")
        print(f"         want: {want!r}")
        fails.append(label)


# ---------------------------------------------------------------------------
# scan_plugin_ids
# ---------------------------------------------------------------------------

print("\nscan_plugin_ids")

UUID = b"901256a9-3e42-4067-9c96-55808625fcc8"


def effect(ident, context=b"Filter", n=1):
    """One effect record, framed as Resolve 21.2 writes it."""
    ident = ident.encode("ascii")
    ctx = b"OfxImageEffectContext" + context + b"_" + UUID + b"_%d" % n
    return (b"\n" + bytes([len(ident) + 9]) + b"\x08\xc9\x80\x80\x80\x0c\x12"
            + bytes([len(ident) + 2]) + b"*" + bytes([len(ident)]) + ident
            + b"\n" + bytes([len(ctx) + 10]) + b"\x08\xde\x80\x80\x80\x0c\x12"
            + bytes([len(ctx) + 2]) + b"*" + bytes([len(ctx)]) + ctx
            + b"\n\n\x08\xe3\x80\x80\x80\x0c\x12\x02 \x01")


NOISE = b"\x00\x00\x80?\x12\x05\x03\x04\x05\x06\x12R\xc6\x04\n\n\x08\x81"

check("a third-party id is read as stored",
      scan_plugin_ids(NOISE + effect("net.sf.openfx.kromatikaplugin") + NOISE),
      ["net.sf.openfx.kromatikaplugin"])

check("a General-context effect is found too",
      scan_plugin_ids(effect("co.videovillage.filmbox3ofx", b"General")),
      ["co.videovillage.filmbox3ofx"])

# Lengths 48-57 are the bytes "0"-"9": the length prefix looks like part of
# the id. 52 characters -> prefixed with "4"; 50 -> "2".
ID52 = "com.blackmagicdesign.resolvefx.colorspacetransformv2"
ID50 = "com.blackmagicdesign.resolvefx.chromaticadaptation"
check("fixture really has a 52-character id", len(ID52), 52)
check("a length byte that is a digit is not taken into the id",
      scan_plugin_ids(effect(ID52)), [ID52])
check("...nor for a 50-character id", scan_plugin_ids(effect(ID50)), [ID50])

# 65-90 / 97-122 are letters: every such length must still come out clean.
for length in (45, 46, 65, 77, 90, 95, 97, 110, 122):
    ident = "com.vendor." + "x" * (length - 11)
    check(f"id of {length} characters (length byte {bytes([length])!r})",
          scan_plugin_ids(effect(ident)), [ident])

check("several effects: first-seen order, duplicates dropped",
      scan_plugin_ids(effect("com.genarts.sapphire.Adjust.S_Gamma", n=1)
                      + NOISE + effect(ID52, n=2)
                      + effect("com.genarts.sapphire.Adjust.S_Gamma", n=3)
                      + effect("COM.GENARTS.SAPPHIRE.ADJUST.S_GAMMA", n=4)),
      ["com.genarts.sapphire.Adjust.S_Gamma", ID52])

check("a body with no effects yields nothing",
      scan_plugin_ids(NOISE * 20 + b"LUTs/Film Looks/Rec709 Kodak.cube"), [])

check("a context string with no id in front of it is ignored",
      scan_plugin_ids(b"\x00\x01\x02OfxImageEffectContextFilter_x_1"), [])

check("...also when text without a dot sits in front",
      scan_plugin_ids(b"\x00\x00somelabel\n\x08\x08OfxImageEffectContextFilter"),
      [])

check("an id whose length byte does not line up is kept if it has a dot",
      scan_plugin_ids(b"\x00\x00com.vendor.thing\n\x08OfxImageEffectContextFilter"),
      ["com.vendor.thing"])

check("empty input", scan_plugin_ids(b""), [])


# ---------------------------------------------------------------------------
# parse_ofx_plugin_cache
# ---------------------------------------------------------------------------

print("\nparse_ofx_plugin_cache")

CACHE = r'''<cache version="ResolveHostV1">
<bundle>
  <binary bundle_path="C:\OFX\Sapphire.ofx.bundle" path="C:\OFX\Sapphire.ofx.bundle\Contents\win64\Sapphire.ofx" mtime="1763400548" size="62818304" status="1" />
  <plugin name="com.genarts.sapphire.Adjust.S_Gamma" index="0" api="OfxImageEffectPluginAPI" api_version="1" major_version="1" minor_version="19000" >
    <apiproperties>
      <property name="OfxImageEffectPluginPropGrouping" >
        <value value=" Sapphire Adjust" />
      </property>
      <property name="OfxPropLabel" >
        <value value="S_Gamma" />
      </property>
      <property name="OfxPropRawLabel" >
        <value value="" />
      </property>
    </apiproperties>
  </plugin>
  <plugin name="com.genarts.sapphire.Adjust.S_Tint" index="1" api="OfxImageEffectPluginAPI" api_version="1" major_version="1" minor_version="19000" >
    <apiproperties>
      <property name="OfxPropLabel" >
        <value value="S_Tint &amp; Co" />
      </property>
    </apiproperties>
  </plugin>
</bundle>
<bundle>
  <binary bundle_path="C:\OFX\Old\NeatVideo5.ofx.bundle" path="x" mtime="1" size="1" status="1" />
  <plugin name="com.absoft.NeatVideo5" index="0" api="OfxImageEffectPluginAPI" api_version="1" major_version="5" minor_version="2" >
    <apiproperties>
      <property name="OfxPropLabel" >
        <value value="" />
      </property>
    </apiproperties>
  </plugin>
</bundle>
<bundle>
  <binary bundle_path="C:\OFX\NeatVideo5.ofx.bundle" path="x" mtime="1" size="1" status="0" />
  <plugin name="com.absoft.NeatVideo5" index="0" api="OfxImageEffectPluginAPI" api_version="1" major_version="5" minor_version="10" >
  </plugin>
</bundle>
<bundle>
  <binary bundle_path="C:\OFX\OpenDRT.ofx.bundle" path="x" mtime="0" size="0" status="2" />
</bundle>
</cache>
'''

cache = parse_ofx_plugin_cache(CACHE)
check("ids are keyed lower-cased",
      sorted(cache),
      ["com.absoft.neatvideo5", "com.genarts.sapphire.adjust.s_gamma",
       "com.genarts.sapphire.adjust.s_tint"])
check("an entry carries name, label, group, version and bundle",
      cache["com.genarts.sapphire.adjust.s_gamma"],
      [{"name": "com.genarts.sapphire.Adjust.S_Gamma", "label": "S_Gamma",
        "group": "Sapphire Adjust", "version": "1.19000",
        "bundle": "C:\\OFX\\Sapphire.ofx.bundle"}])
check("a property of one plugin does not leak into the next",
      cache["com.genarts.sapphire.adjust.s_tint"][0]["group"], "")
check("XML entities are unescaped",
      cache["com.genarts.sapphire.adjust.s_tint"][0]["label"], "S_Tint & Co")
check("the same id in two bundles gives two entries",
      [e["bundle"] for e in cache["com.absoft.neatvideo5"]],
      ["C:\\OFX\\Old\\NeatVideo5.ofx.bundle", "C:\\OFX\\NeatVideo5.ofx.bundle"])
check("a bundle with no plugins adds nothing", len(cache), 3)
check("an empty or foreign file parses to nothing",
      [parse_ofx_plugin_cache(""), parse_ofx_plugin_cache("<html></html>")],
      [{}, {}])


# ---------------------------------------------------------------------------
# classify_plugin
# ---------------------------------------------------------------------------

print("\nclassify_plugin")

everything = lambda p: True
nothing = lambda p: False
only_current = lambda p: "Old" not in p

got = classify_plugin("com.genarts.sapphire.adjust.s_gamma", cache, everything)
check("project ids are lower-cased; they still match the installed plugin",
      (got["status"], got["bundle"], got["version"]),
      ("found", "C:\\OFX\\Sapphire.ofx.bundle", "1.19000"))
check("an installed plugin gets its group and label", got["label"],
      "Sapphire Adjust / S_Gamma")

got = classify_plugin("net.sf.openfx.kromatikaplugin", cache, everything)
check("a plugin the cache does not list is missing, with no label",
      (got["status"], got["label"], got["bundle"]), ("missing", "", ""))

got = classify_plugin("com.blackmagicdesign.resolvefx.dctl", cache, nothing)
check("ResolveFX is built in, whatever the cache says", got["status"], "builtin")
check("...also with no cache at all",
      classify_plugin("com.blackmagicdesign.resolvefx.dctl", None)["status"],
      "builtin")

check("no cache: a third-party plugin is unknown, not missing",
      classify_plugin("com.absoft.neatvideo5", None, everything)["status"],
      "unknown")

got = classify_plugin("com.absoft.neatvideo5", cache, nothing)
check("listed in the cache but the bundle is gone: missing",
      got["status"], "missing")
check("...and the note says where it used to be",
      "C:\\OFX\\NeatVideo5.ofx.bundle" in got["note"], True)
check("...while the label is still known from the cache",
      got["label"], "com.absoft.NeatVideo5")

got = classify_plugin("com.absoft.neatvideo5", cache, only_current)
check("of two installs, only the one on disk counts",
      (got["status"], got["bundle"]),
      ("found", "C:\\OFX\\NeatVideo5.ofx.bundle"))

got = classify_plugin("com.absoft.neatvideo5", cache, everything)
check("with both on disk the newer version wins (5.10 over 5.2)",
      (got["bundle"], got["version"]),
      ("C:\\OFX\\NeatVideo5.ofx.bundle", "5.10"))

check("an empty cache makes everything third-party missing",
      classify_plugin("com.absoft.neatvideo5", {}, everything)["status"],
      "missing")


# ---------------------------------------------------------------------------

print("")
if fails:
    print(f"RESULT: FAIL ({len(fails)} failed)")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("RESULT: PASS")
sys.exit(0)
