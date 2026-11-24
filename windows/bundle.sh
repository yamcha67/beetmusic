#!/usr/bin/env bash
# Builds the portable Windows app folder  dist/BeetMusic  from an MSYS2 MINGW64 installation.
#   Run inside an "MSYS2 MINGW64" shell:   bash windows/bundle.sh
# Needs (pacman -S --needed):  see .github/workflows/build-windows.yml  (the package list lives there)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${MINGW_PREFIX:-/mingw64}"
DIST="$ROOT/dist/BeetMusic"
PYVER="$(python -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "==> Bundling from $PREFIX (Python $PYVER) into $DIST"

rm -rf "$ROOT/dist"
mkdir -p "$DIST/bin" "$DIST/lib" "$DIST/libexec/gstreamer-1.0" "$DIST/share/fonts/beet-music" "$DIST/app"
roots=()   # every binary whose DLL dependencies must travel with the app

# ---- mutagen normally comes from pacman; fall back to pip if that package isn't available
python -c "import mutagen" 2>/dev/null || pip install --break-system-packages mutagen

# ---- Python + its libraries (GTK, libadwaita, GStreamer, mutagen come in through site-packages)
cp -r "$PREFIX/lib/python$PYVER" "$DIST/lib/"
( cd "$DIST/lib/python$PYVER" && rm -rf test idlelib tkinter turtledemo ensurepip lib2to3 \
    site-packages/pip* site-packages/setuptools* site-packages/pkg_resources* site-packages/wheel* \
    site-packages/_distutils_hack 2>/dev/null || true )
find "$DIST/lib" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
roots+=("$PREFIX/bin/python.exe" "$PREFIX/bin/pythonw.exe" "$PREFIX/bin/gdk-pixbuf-query-loaders.exe")
roots+=("$PREFIX"/bin/libpython3*.dll)
while IFS= read -r f; do roots+=("$f"); done < <(find "$DIST/lib/python$PYVER" \( -name '*.pyd' -o -name '*.dll' \))

# ---- libraries that GTK / GStreamer load at run time (found through typelibs, not through imports)
for pat in libgtk-4-* libadwaita-1-* libgraphene-1.0-* libpangocairo-1.0-* libpangowin32-1.0-* \
           libgdk_pixbuf-2.0-* librsvg-2-* libcairo-gobject-* libgirepository*  libgio-2.0-* \
           libgst*-1.0-* libharfbuzz-gobject-* libepoxy-*; do
  for f in "$PREFIX"/bin/$pat.dll; do [ -f "$f" ] && roots+=("$f"); done
done

# ---- typelibs, gdk-pixbuf loaders (PNG, JPEG, SVG ...), schemas, icons
cp -r "$PREFIX/lib/girepository-1.0" "$DIST/lib/"
cp -r "$PREFIX/lib/gdk-pixbuf-2.0" "$DIST/lib/"
while IFS= read -r f; do roots+=("$f"); done < <(find "$DIST/lib/gdk-pixbuf-2.0" -name '*.dll')
mkdir -p "$DIST/share/glib-2.0"
cp -r "$PREFIX/share/glib-2.0/schemas" "$DIST/share/glib-2.0/"
glib-compile-schemas "$DIST/share/glib-2.0/schemas"
cp -r "$PREFIX/share/icons" "$DIST/share/"
[ -d "$PREFIX/share/gtk-4.0" ] && cp -r "$PREFIX/share/gtk-4.0" "$DIST/share/"

# ---- GStreamer: just the plug-ins a music player needs (decoders, parsers, tags, Windows audio output)
mkdir -p "$DIST/lib/gstreamer-1.0"
plugins="coreelements typefindfunctions playback audioconvert audioresample volume autodetect audioparsers \
 id3demux apetag icydemux isomp4 flac wavparse wavpack ogg vorbis opus opusparse mpg123 lame mpegaudioparse \
 libav matroska replaygain gio app audiorate wasapi wasapi2 directsound"
for p in $plugins; do
  if [ -f "$PREFIX/lib/gstreamer-1.0/libgst$p.dll" ]; then
    cp "$PREFIX/lib/gstreamer-1.0/libgst$p.dll" "$DIST/lib/gstreamer-1.0/"
    roots+=("$DIST/lib/gstreamer-1.0/libgst$p.dll")
  else
    echo "   (note: GStreamer plug-in '$p' not installed - skipped)"
  fi
done
cp "$PREFIX/libexec/gstreamer-1.0/gst-plugin-scanner.exe" "$DIST/libexec/gstreamer-1.0/"
roots+=("$DIST/libexec/gstreamer-1.0/gst-plugin-scanner.exe")

# ---- collect every DLL those binaries need, recursively, from the MSYS2 prefix
echo "==> Collecting DLL dependencies (${#roots[@]} binaries)"
printf '%s\n' "${roots[@]}" | xargs -d '\n' ntldd -R 2>/dev/null \
  | grep -i 'mingw64' | sed 's/.*=> *//; s/ (0x[0-9a-fA-F]*)$//' | sort -u \
  | while IFS= read -r f; do cp -n "$(cygpath -u "$f")" "$DIST/bin/" 2>/dev/null || true; done
for exe in python.exe pythonw.exe gdk-pixbuf-query-loaders.exe; do cp -n "$PREFIX/bin/$exe" "$DIST/bin/"; done
cp "$DIST/bin/pythonw.exe" "$DIST/bin/BeetMusic.exe"   # own process name (and icon) in Task Manager / taskbar

# ---- the app itself, fonts, launcher
cp "$ROOT/app/beet-music.py" "$DIST/app/"
cp "$ROOT"/fonts/*.ttf "$ROOT"/fonts/LICENSE* "$DIST/share/fonts/beet-music/"
cp "$ROOT/assets/beet-music.ico" "$DIST/"
windres "$ROOT/windows/launcher.rc" -O coff -o "$ROOT/windows/launcher.res"
gcc -O2 -s -mwindows -municode "$ROOT/windows/launcher.c" "$ROOT/windows/launcher.res" -o "$DIST/Beet Music.exe"
cat > "$DIST/Beet Music (debug).cmd" <<'CMD'
@echo off
rem Starts the app with a console window so error messages stay visible.
cd /d "%~dp0"
bin\python.exe -X utf8 app\beet-music.py
pause
CMD

# ---- self-test: the bundled interpreter must be able to load everything the app needs
echo "==> Self-test"
export PATH="$DIST/bin:$PATH" GI_TYPELIB_PATH="$(cygpath -w "$DIST/lib/girepository-1.0")" \
       GST_PLUGIN_SYSTEM_PATH_1_0="$(cygpath -w "$DIST/lib/gstreamer-1.0")" \
       GST_PLUGIN_PATH_1_0="$(cygpath -w "$DIST/lib/gstreamer-1.0")" \
       GST_PLUGIN_SCANNER="$(cygpath -w "$DIST/libexec/gstreamer-1.0/gst-plugin-scanner.exe")" \
       GSETTINGS_SCHEMA_DIR="$(cygpath -w "$DIST/share/glib-2.0/schemas")" \
       XDG_DATA_DIRS="$(cygpath -w "$DIST/share")"
"$DIST/bin/python.exe" - <<'PY'
import gi
for ns, ver in (("Gtk","4.0"),("Adw","1"),("Gst","1.0"),("GdkPixbuf","2.0"),("Graphene","1.0"),("Gsk","4.0")):
    gi.require_version(ns, ver)
from gi.repository import Gtk, Adw, Gst, GdkPixbuf, Gsk
import mutagen
Gst.init(None)
for name in ("playbin", "autoaudiosink", "audioconvert", "volume"):
    assert Gst.ElementFactory.find(name), f"GStreamer element missing: {name}"
print("Gtk", Gtk.get_major_version(), Gtk.get_minor_version(), "| libadwaita", Adw.get_minor_version(),
      "| GStreamer", Gst.version_string(), "| mutagen", mutagen.version_string)
assert hasattr(Gsk.Path, "parse"), "GTK 4.14+ is required"
print("self-test OK")
PY
du -sh "$DIST"
echo "==> Done: $DIST"
