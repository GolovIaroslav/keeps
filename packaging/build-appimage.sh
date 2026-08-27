#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$ROOT/pyproject.toml" | head -n 1)}"
APPDIR="${APPDIR:-$ROOT/AppDir}"
OUTPUT="${2:-$ROOT/keeps-${VERSION}-x86_64.AppImage}"
OUTPUT="$(realpath -m "$OUTPUT")"
APPIMAGETOOL_URL="${APPIMAGETOOL_URL:-https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage}"
APPIMAGETOOL_SHA256="${APPIMAGETOOL_SHA256:-b90f4a8b18967545fda78a445b27680a1642f1ef9488ced28b65398f2be7add2}"
APPIMAGE_RUNTIME_URL="${APPIMAGE_RUNTIME_URL:-https://github.com/AppImage/AppImageKit/releases/download/continuous/runtime-x86_64}"
APPIMAGE_RUNTIME_SHA256="${APPIMAGE_RUNTIME_SHA256:-66f5b22f035022b8bdebb54c066aa6edc7b5db282fe6cdb372e7965f80772557}"

if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' "error: uv is required to install the portable Python runtime" >&2
    exit 1
fi

# Do not let `uv python find` discover the project's `.venv`: a virtual
# environment contains only a launcher and site-packages, while its standard
# library remains in the build machine's uv cache.  Copying it produces an
# AppImage that works only where that cache happens to exist.
uv python install --managed-python 3.12
PYTHON_SOURCE="$(uv python find --no-project --managed-python --resolve-links 3.12)"
PYROOT="$(dirname "$(dirname "$PYTHON_SOURCE")")"
PYTHON="$APPDIR/usr/python312/bin/python3.12"
BUILD_TMP="$(mktemp -d)"
trap 'rm -rf "$BUILD_TMP"' EXIT

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr"
cp -aL "$PYROOT" "$APPDIR/usr/python312"

"$PYTHON" -I -c "import encodings, sys; assert sys.prefix.startswith('$APPDIR'), f'sys.prefix={sys.prefix!r} escaped the AppDir'; assert encodings.__file__.startswith('$APPDIR'), encodings.__file__; print('interpreter self-contained, sys.prefix =', sys.prefix)"
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
"$PYTHON" -I -c "import keeps, PySide6, onnxruntime, cv2, numpy, tokenizers; print('all dependencies import from the AppDir copy')"

# Legacy clipboard bytes without a declared charset are genuinely ambiguous.
# Keeps uses libuchardet rather than a home-grown language heuristic. Bundle
# the small native library so the AppImage behaves the same on clean systems;
# source installs may still run without it and keep exact UTF/charset paths.
UCHARDET_LIBRARY="${UCHARDET_LIBRARY:-}"
if [ -z "$UCHARDET_LIBRARY" ]; then
    UCHARDET_LIBRARY="$(
        ldconfig -p 2>/dev/null |
            awk '$1 == "libuchardet.so.0" && !found { print $NF; found = 1 }'
    )"
fi
if [ -z "$UCHARDET_LIBRARY" ] || [ ! -f "$UCHARDET_LIBRARY" ]; then
    printf '%s\n' "error: libuchardet.so.0 is required to build the portable AppImage" >&2
    exit 1
fi
mkdir -p "$APPDIR/usr/lib"
cp -aL "$UCHARDET_LIBRARY" "$APPDIR/usr/lib/libuchardet.so.0"

# PySide6 wheels bundle Qt itself but intentionally rely on several ordinary
# desktop libraries from the build host (GLib, fontconfig, DBus, X11/Wayland,
# and friends).  An AppImage must bring those non-driver libraries along: a
# newly installed desktop otherwise fails before Keeps can show a window.
# Keep glibc and the OpenGL/EGL driver boundary on the host; bundling either is
# unsafe because the loader and GPU drivers must match the target system.
bundle_system_libraries() {
    local bundle_dir library real_library soname path
    bundle_dir="$APPDIR/usr/lib"
    mkdir -p "$bundle_dir"

    while IFS=' ' read -r soname path; do
        [ -n "$path" ] || continue
        case "$soname" in
            /*) continue ;;
            libc.so.6|libdl.so.2|libm.so.6|libpthread.so.0|librt.so.1|libutil.so.1|\
            ld-linux-*.so.*|libGL.so.1|libEGL.so.1|libGLX.so.0|libGLdispatch.so.0)
                continue
                ;;
        esac
        case "$path" in "$APPDIR"/*) continue ;; esac
        real_library="$(readlink -f "$path")"
        cp -a "$real_library" "$bundle_dir/$(basename "$real_library")"
        ln -sfn "$(basename "$real_library")" "$bundle_dir/$soname"
    done < <(
        {
            find "$APPDIR/usr/python312" -type f \( -name '*.so' -o -name '*.so.*' \) -print0
            printf '%s\0' "$APPDIR/usr/lib/libuchardet.so.0"
        } |
            while IFS= read -r -d '' library; do
                ldd "$library" 2>/dev/null |
                    awk '/=> \/[^ ]+/ { print $1, $3 }'
            done |
            sort -u
    )
}

bundle_system_libraries
LD_LIBRARY_PATH="$APPDIR/usr/lib" "$PYTHON" -I -c "
from keeps.text_encoding import normalize_plain_text
assert normalize_plain_text('Привет мир'.encode('cp1251')).decode() == 'Привет мир'
print('legacy charset detector loads from the AppImage copy')
"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
# -I prevents a user's PYTHONHOME/PYTHONPATH and user-site packages from
# changing the private runtime inside the AppImage.
export LD_LIBRARY_PATH="$HERE/usr/lib"
exec "$HERE/usr/python312/bin/python3.12" -I -c "from keeps.app import main; main()" "$@"
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

RUNTIME="${APPIMAGE_RUNTIME:-$ROOT/packaging/runtime-x86_64}"
if [ ! -f "$RUNTIME" ]; then
    RUNTIME="$BUILD_TMP/runtime-x86_64"
    curl -fsSL -o "$RUNTIME" "$APPIMAGE_RUNTIME_URL"
fi
printf '%s  %s\n' "$APPIMAGE_RUNTIME_SHA256" "$RUNTIME" | sha256sum --check --status
chmod +x "$RUNTIME"

# mksquashfs defaults to one thread per core; against a ~1GB AppDir that can
# starve a busy desktop machine (observed locally: silent build death under
# load). Set MKSQUASHFS_PROCESSORS=2 for local builds; CI runs unthrottled.
TOOL_ARGS=()
if [ -n "${MKSQUASHFS_PROCESSORS:-}" ]; then
    TOOL_ARGS+=(--mksquashfs-opt -processors --mksquashfs-opt "$MKSQUASHFS_PROCESSORS")
fi
(cd "$BUILD_TMP" && env APPIMAGELAUNCHER_DISABLE=1 "$TOOL" --appimage-extract >/dev/null)
ARCH=x86_64 env APPIMAGELAUNCHER_DISABLE=1 "$BUILD_TMP/squashfs-root/AppRun" \
    --runtime-file "$RUNTIME" "${TOOL_ARGS[@]}" "$APPDIR" "$OUTPUT"

# appimagetool writes the final MD5 digest into the runtime's `.digest_md5`
# ELF section. Apart from that documented mutable field, the emitted ELF
# prefix must be byte-for-byte the runtime we supplied. Locate the section
# from the ELF table instead of assuming one particular runtime's offsets.
python3 - "$OUTPUT" "$RUNTIME" <<'PY'
import struct
import sys

output_path, runtime_path = sys.argv[1:]
with open(runtime_path, "rb") as stream:
    runtime = stream.read()
with open(output_path, "rb") as stream:
    output_prefix = stream.read(len(runtime))

assert runtime[:4] == b"\x7fELF", "AppImage runtime is not ELF"
assert runtime[4:6] == b"\x02\x01", "AppImage runtime must be little-endian ELF64"
section_offset = struct.unpack_from("<Q", runtime, 0x28)[0]
section_size = struct.unpack_from("<H", runtime, 0x3A)[0]
section_count = struct.unpack_from("<H", runtime, 0x3C)[0]
names_index = struct.unpack_from("<H", runtime, 0x3E)[0]
assert section_size >= 64 and section_count and names_index < section_count

def section(index):
    offset = section_offset + index * section_size
    return struct.unpack_from("<IIQQQQIIQQ", runtime, offset)

names_header = section(names_index)
names = runtime[names_header[4] : names_header[4] + names_header[5]]
digest_range = None
for index in range(section_count):
    header = section(index)
    name_offset = header[0]
    name = names[name_offset : names.find(b"\0", name_offset)].decode("ascii")
    if name == ".digest_md5":
        digest_range = range(header[4], header[4] + header[5])
        break

assert digest_range is not None, "AppImage runtime has no .digest_md5 section"
assert len(output_prefix) == len(runtime), "AppImage output is shorter than its runtime"
digest_start, digest_stop = digest_range.start, digest_range.stop
assert (
    output_prefix[:digest_start] == runtime[:digest_start]
    and output_prefix[digest_stop:] == runtime[digest_stop:]
), "appimagetool embedded an unexpected runtime"
PY

# Test the *packed* filesystem, rather than only AppDir.  This catches a
# broken runtime copy (notably a virtualenv without its standard library) and
# makes host Python configuration unable to hide it.
VERIFY_DIR="$BUILD_TMP/verify-appimage"
mkdir -p "$VERIFY_DIR"
(
    cd "$VERIFY_DIR"
    APPIMAGELAUNCHER_DISABLE=1 "$OUTPUT" --appimage-extract >/dev/null
    test -f squashfs-root/usr/python312/lib/python3.12/encodings/__init__.py
    PYTHONHOME=/nonexistent-python-home PYTHONPATH=/nonexistent-python-path \
        squashfs-root/AppRun --version | grep -Fx "$VERSION" >/dev/null
    PYTHONHOME=/nonexistent-python-home PYTHONPATH=/nonexistent-python-path \
        squashfs-root/AppRun status >/dev/null
)
printf 'created %s\n' "$OUTPUT"
