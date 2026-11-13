#!/usr/bin/env bash
# Build the installable zip of the add-on this script sits in. The one place
# the build is defined: scripts/build-plugin.sh runs it on a desktop, and
# .github/workflows/release.yml on GitHub.
#
#     .github/build.sh <output directory>
#
# Prints the zip's path, and writes <zip>.sha256 next to it; everything else
# goes to stderr. The zip is made from the add-on's parent directory, so its
# single top-level folder is the add-on's directory -- which must be named
# after the add-on id, as "Install from zip file" expects (on GitHub: the
# checkout path).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARENT="$(dirname "$SRC")"
ADDON="$(basename "$SRC")"
DIST="$(mkdir -p "${1:?usage: .github/build.sh <output directory>}" && cd "$1" && pwd)"

read -r ID VERSION < <(python3 - "$SRC/addon.xml" <<'PY'
import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
print(root.get("id"), root.get("version"))
PY
)
if [ "$ADDON" != "$ID" ]; then
    echo "the add-on's directory is $ADDON, but the zip's folder must be its id, $ID" >&2
    exit 1
fi

# The add-on ships a yt-dlp copy to run until its updater has downloaded one;
# it is not in git (resources/vendor/ is ignored), so a fresh checkout fetches
# the current stable release -- through the add-on's own ytdlp_loader.update(),
# with the checks a device applies: the checksum from the release's
# SHA2-256SUMS, and compatibility with Python 3.11, the interpreter Kodi 21
# ships. ../scripts/fetch-ytdlp.sh refreshes a desktop's copy on demand.
if [ ! -f "$SRC/resources/vendor/yt-dlp.zip" ]; then
    python3 - "$SRC" <<'FETCH' >&2
import os, shutil, sys, tempfile
src = sys.argv[1]
sys.path.insert(0, src)
from resources.lib import ytdlp_loader as L
store = tempfile.mkdtemp()
try:
    result = L.update(store, "stable", python=(3, 11))
    if result.status != L.UPDATED:
        sys.exit("yt-dlp stable not bundled: {}".format(result))
    path, version = L.current(store)
    target = os.path.join(src, "resources", "vendor", "yt-dlp.zip")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(path, target)
    print("bundled yt-dlp {} (stable)".format(version))
finally:
    shutil.rmtree(store, ignore_errors=True)
FETCH
fi

# The bundled copy must have its \N{} escapes rewritten like every download,
# or Kodi 21's Python crashes on the second playback (ytdlp_loader docstring).
# Downloads come rewritten; a copy fetched by an older script is patched here.
python3 - "$SRC" <<'PATCH'
import os, sys, tempfile
src = sys.argv[1]
sys.path.insert(0, src)
from resources.lib import ytdlp_loader as L
bundled = os.path.join(src, "resources", "vendor", "yt-dlp.zip")
if not L.is_patched(bundled):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(bundled), prefix=".patch-")
    os.close(fd)
    count = L.patch_archive(bundled, tmp)
    os.replace(tmp, bundled)
    print("patched the bundled yt-dlp: {} escape(s) rewritten".format(count), file=sys.stderr)
PATCH

# Kodi refuses an add-on with malformed XML and says very little about why,
# so fail here instead of on the box.
while IFS= read -r -d '' xml; do
    python3 -c "import sys,xml.etree.ElementTree as ET; ET.parse(sys.argv[1])" "$xml"
done < <(find "$SRC" -name '*.xml' -not -path "$SRC/.git/*" -print0)

# Syntax errors must not reach the box. compileall runs this machine's Python;
# the grammar of CoreELEC Omega's 3.11 is checked by the guard below.
python3 -m compileall -q -x '/\.git/' "$SRC" > /dev/null

# compileall does not catch a module used but never imported -- that only blows
# up at runtime, and a broad "except Exception" can swallow it into a silent
# fallback. Check our own modules explicitly -- and that every file parses with
# Python 3.11's grammar, which a newer compileall above would let through. Best
# effort: it rejects 3.12 statements (type, generic syntax), not every f-string
# that only 3.12's PEP 701 accepts.
python3 - "$SRC" <<'GUARD'
import ast, builtins, os, sys
src = sys.argv[1]
MODULE_NAMES = {"__file__", "__name__", "__doc__", "__spec__", "__package__"}


def undefined_names(tree):
    """Names read somewhere in the file but bound nowhere in it -- a missing import
    above all. Scope-blind, so it misses a name bound in the wrong function, but
    never flags a correct one."""
    bound = set(dir(builtins)) | MODULE_NAMES
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    return sorted({(node.lineno, node.id) for node in ast.walk(tree)
                   if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in bound})


ours = {f[:-3] for f in os.listdir(os.path.join(src, "resources", "lib")) if f.endswith(".py")}
ours.discard("__init__")
problems = []
exports = {}
for root, dirs, files in os.walk(src):
    dirs[:] = [d for d in dirs if not d.startswith(".git")]  # not the add-on: .git, .github
    for name in files:
        if not name.endswith(".py"):
            continue
        path = os.path.join(root, name)
        source = open(path, encoding="utf-8").read()
        try:
            ast.parse(source, filename=path, feature_version=(3, 11))
        except SyntaxError as error:
            problems.append("{}:{}: not valid on Python 3.11: {}".format(
                os.path.relpath(path, src), error.lineno, error.msg))
            continue
        tree = ast.parse(source)
        for line, name in undefined_names(tree):
            problems.append("{}:{}: {} is used but never defined or imported".format(
                os.path.relpath(path, src), line, name))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update((a.asname or a.name).split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.update(a.asname or a.name for a in node.names)
        used = {n.value.id for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        for missing in sorted((used & ours) - imported):
            problems.append("{}: uses {} but never imports it".format(
                os.path.relpath(path, src), missing))
        # Also catch calling something our module does not define -- a symbol
        # deleted during a refactor only blows up at runtime otherwise.
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in ours and node.value.id in imported):
                target = os.path.join(src, "resources", "lib", node.value.id + ".py")
                if target not in exports:
                    mod = ast.parse(open(target, encoding="utf-8").read())
                    exports[target] = {n.name for n in mod.body
                                       if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
                    # Plain, annotated and tuple-unpacking assignments alike:
                    # "A, B = 1, 2" defines A and B just as much as "A = 1".
                    for n in mod.body:
                        targets = (n.targets if isinstance(n, ast.Assign)
                                   else [n.target] if isinstance(n, ast.AnnAssign) else [])
                        for target_node in targets:
                            exports[target].update(
                                x.id for x in ast.walk(target_node) if isinstance(x, ast.Name))
                if node.attr not in exports[target]:
                    problems.append("{}: {}.{} is not defined in {}.py".format(
                        os.path.relpath(path, src), node.value.id, node.attr, node.value.id))
if problems:
    print("\n".join(problems), file=sys.stderr)
    sys.exit(1)
GUARD
find "$SRC" -name '__pycache__' -type d -prune -exec rm -rf {} +

ZIP="$DIST/$ID-$VERSION.zip"
rm -f "$ZIP" "$ZIP.sha256"

# Zip from the parent directory so the archive holds a single top-level folder
# named after the add-on id -- what "Install from zip file" expects. The add-on
# directory is also a git checkout and gets opened in an IDE; neither .git nor
# .idea belongs in an installable zip. zip's '*' spans directory separators, so
# ".git*" covers the directory, its contents, .gitignore and .gitattributes --
# and .github, this script and the release workflow, alike.
( cd "$PARENT" && zip -qr "$ZIP" "$ADDON" \
    -x "$ADDON/.git*" "$ADDON/.idea*" '*.pyc' '*__pycache__*' )
( cd "$DIST" && sha256sum "$(basename "$ZIP")" > "$(basename "$ZIP").sha256" )

echo "$ZIP"
