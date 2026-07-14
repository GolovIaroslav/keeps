#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$ROOT/pyproject.toml" | head -n 1)}"
APPDIR="${APPDIR:-$ROOT/AppDir}"
OUTPUT="${2:-$ROOT/keeps-${VERSION}-x86_64.AppImage}"
APPIMAGETOOL_URL="${APPIMAGETOOL_URL:-https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage}"
APPIMAGETOOL_SHA256="${APPIMAGETOOL_SHA256:-b90f4a8b18967545fda78a445b27680a1642f1ef9488ced28b65398f2be7add2}"

if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' "error: uv is required to install the portable Python runtime" >&2
    exit 1
fi

uv python install 3.12
PYROOT="$(dirname "$(dirname "$(uv python find 3.12)")")"
PYTHON="$APPDIR/usr/python312/bin/python3.12"
BUILD_TMP="$(mktemp -d)"
trap 'rm -rf "$BUILD_TMP"' EXIT

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr"
cp -aL "$PYROOT" "$APPDIR/usr/python312"

"$PYTHON" -c "import sys; assert sys.prefix.startswith('$APPDIR'), f'sys.prefix={sys.prefix!r} escaped the AppDir'; print('interpreter self-contained, sys.prefix =', sys.prefix)"
(cd "$ROOT" && uv export \
    --frozen \
    --no-dev \
    --extra ai \
    --format requirements.txt \
    --no-emit-project \
    --output-file "$BUILD_TMP/requirements.txt" >/dev/null)
# The copied interpreter can carry PEP 668's EXTERNALLY-MANAGED marker from
# the CI runner. It is intentionally the private interpreter inside AppDir,
# so allow uv to install the bundled application into it.
uv pip sync --python "$PYTHON" --system --break-system-packages "$BUILD_TMP/requirements.txt"
uv pip install --python "$PYTHON" --system --break-system-packages --no-deps "$ROOT"
"$PYTHON" -c "import keeps, PySide6, onnxruntime, cv2, numpy, tokenizers; print('all dependencies import from the AppDir copy')"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/python312/bin/python3.12" -c "from keeps.app import main; main()" "$@"
EOF
chmod +x "$APPDIR/AppRun"

cp "$ROOT/packaging/keeps.desktop" "$APPDIR/keeps.desktop"
ICON="$(find /usr/share/icons/breeze -iname 'edit-paste.svg' 2>/dev/null | sort | tail -1)"
if [ -z "$ICON" ]; then
    printf '%s\n' "error: /usr/share/icons/breeze/edit-paste.svg was not found" >&2
    exit 1
fi
cp "$ICON" "$APPDIR/edit-paste.svg"

TOOL="${APPIMAGETOOL:-}"
if [ -z "$TOOL" ]; then
    TOOL="$BUILD_TMP/appimagetool"
    curl -fsSL -o "$TOOL" "$APPIMAGETOOL_URL"
    chmod +x "$TOOL"
fi
printf '%s  %s\n' "$APPIMAGETOOL_SHA256" "$TOOL" | sha256sum --check --status

# mksquashfs defaults to one thread per core; against a ~1GB AppDir that can
# starve a busy desktop machine (observed locally: silent build death under
# load). Set MKSQUASHFS_PROCESSORS=2 for local builds; CI runs unthrottled.
TOOL_ARGS=()
if [ -n "${MKSQUASHFS_PROCESSORS:-}" ]; then
    TOOL_ARGS+=(--mksquashfs-opt -processors --mksquashfs-opt "$MKSQUASHFS_PROCESSORS")
fi
ARCH=x86_64 "$TOOL" --appimage-extract-and-run "${TOOL_ARGS[@]}" "$APPDIR" "$OUTPUT"
printf 'created %s\n' "$OUTPUT"
