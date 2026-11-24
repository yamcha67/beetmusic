#!/usr/bin/python3
"""Beet Music - a GTK4 / libadwaita music player for Fedora Linux (needs libadwaita 1.5+).

* Albums / Artists / Playlists tabs in the header bar, search toggle on the left,
  main menu on the right
* Songs tab: every song in one searchable list
* Album grid sorted by year (newest first) by default; light, dark or system theme
* Artwork from embedded tags, cover.jpg/folder.jpg, or fetched from the internet
  (MusicBrainz + Cover Art Archive, iTunes fallback)
* The library is saved after the first scan; later launches load instantly and
  only new, changed or removed files are read again
* Media keys and headphone buttons (play/pause, next, previous) via MPRIS, plus
  keyboard shortcuts: Space, Ctrl+Left/Right
* Glass player bar: live blurred backdrop, 2FDE6B accent, light and dark
* Repeat-song toggle (remembered between launches)
* Favorites: heart any song (Songs tab, album pages, player bar); the Favorites tab
  in the top bar lists them (saved to ~/.config/beet-music-favorites.json)
* Mini player (Ctrl+M or the top-bar button): a small window with cover, seek bar and
  transport controls; the expand button brings the full window back
* Custom symbolic icons (tabs, repeat, hearts, mini player) that follow light/dark
* Playlists (saved to ~/.config), GStreamer playback, seek, volume, prev/next

Run:      python3 beet-music.py
Install:  python3 beet-music.py --install   (menu entry + icon + ~/.local/bin/beet-music)
"""
import base64
import collections
import concurrent.futures
import hashlib
import weakref
import gc
import json
import math
import os
import re
import xml.etree.ElementTree as ET
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------- Windows support
# The same code runs on Linux and Windows. Everything below is inert on Linux.
IS_WINDOWS = os.name == "nt" or os.environ.get("BEET_FAKE_WINDOWS") == "1"  # the env var is for testing only


def _windows_bootstrap():
    """In the packaged Windows app (bin\\BeetMusic.exe, share\\, lib\\ ...) point GTK, GLib, GStreamer and
    gdk-pixbuf at the bundled files, install the bundled fonts, and give the taskbar the app's identity.
    Runs before GTK is imported, which is when those libraries read their environment."""
    if os.name != "nt":
        return
    import ctypes
    root = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    if not os.path.isdir(os.path.join(root, "share", "glib-2.0")):
        return  # not the packaged app (e.g. run from an MSYS2 shell): leave the environment alone

    def setenv(key, value):
        os.environ[key] = value
        try:
            ctypes.windll.kernel32.SetEnvironmentVariableW(key, value)
        except Exception:
            pass

    local = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "BeetMusic")
    os.makedirs(local, exist_ok=True)
    join = os.path.join
    setenv("PATH", join(root, "bin") + os.pathsep + os.environ.get("PATH", ""))
    setenv("XDG_DATA_DIRS", join(root, "share"))
    setenv("GSETTINGS_SCHEMA_DIR", join(root, "share", "glib-2.0", "schemas"))
    setenv("GI_TYPELIB_PATH", join(root, "lib", "girepository-1.0"))
    setenv("GST_PLUGIN_SYSTEM_PATH_1_0", join(root, "lib", "gstreamer-1.0"))
    setenv("GST_PLUGIN_PATH_1_0", join(root, "lib", "gstreamer-1.0"))
    setenv("GST_PLUGIN_SCANNER", join(root, "libexec", "gstreamer-1.0", "gst-plugin-scanner.exe"))
    setenv("GST_REGISTRY_1_0", join(local, "gst-registry.bin"))
    # gdk-pixbuf loader list, rebuilt here so it always points at wherever the app was installed
    loaders = join(root, "lib", "gdk-pixbuf-2.0", "2.10.0", "loaders")
    cache = join(local, "loaders.cache")
    query = join(root, "bin", "gdk-pixbuf-query-loaders.exe")
    try:
        if os.path.isfile(query) and (not os.path.exists(cache)
                                      or os.path.getmtime(cache) < os.path.getmtime(query)):
            env = dict(os.environ, GDK_PIXBUF_MODULEDIR=loaders)
            out = subprocess.run([query], capture_output=True, env=env, check=True,
                                 creationflags=0x08000000).stdout  # CREATE_NO_WINDOW
            with open(cache, "wb") as f:
                f.write(out)
        if os.path.exists(cache):
            setenv("GDK_PIXBUF_MODULE_FILE", cache)
    except Exception as e:
        print("gdk-pixbuf loader cache:", e, file=sys.stderr)
    _install_windows_fonts(join(root, "share", "fonts", "beet-music"))
    try:  # one taskbar button/icon for the app, not "python"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("io.github.aliqureshi.BeetMusic")
    except Exception:
        pass


def _install_windows_fonts(folder):
    """Install the bundled Adwaita Sans for the current user (no admin rights needed), so the app has the
    same typeface as on Linux. Done once; later launches find the files already in place."""
    if not os.path.isdir(folder):
        return
    try:
        import ctypes
        import winreg
        dest = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")
        os.makedirs(dest, exist_ok=True)
        installed = False
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows NT\CurrentVersion\Fonts")
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith((".ttf", ".otf")):
                continue
            target = os.path.join(dest, name)
            if not os.path.exists(target):
                shutil.copyfile(os.path.join(folder, name), target)
                installed = True
            winreg.SetValueEx(key, os.path.splitext(name)[0] + " (TrueType)", 0, winreg.REG_SZ, target)
            ctypes.windll.gdi32.AddFontResourceW(target)
        if installed:  # tell running programs, and give GTK a moment before it builds its font list
            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001D, 0, 0, 2, 1000, None)
    except Exception as e:
        print("font install:", e, file=sys.stderr)


_windows_bootstrap()

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Graphene, Gsk, Gst, Gtk, Pango  # noqa: E402

import mutagen  # noqa: E402


def windows_prefers_dark():
    """True/False from the Windows 'app mode' setting, or None if it can't be read."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        return None


class WindowsMediaKeys:
    """Play/pause, next and previous keys (and headphone buttons) on Windows. The Linux build gets these
    through MPRIS; here they are registered as global hot keys on a helper thread."""
    KEYS = ((0xB3, "toggle"), (0xB0, "next"), (0xB1, "prev"))  # VK_MEDIA_PLAY_PAUSE / NEXT_TRACK / PREV_TRACK

    def __init__(self, callback):
        self.callback = callback  # called on the GTK main loop with "toggle", "next" or "prev"
        threading.Thread(target=self._run, daemon=True, name="beet-media-keys").start()

    def _run(self):
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            for i, (vk, _name) in enumerate(self.KEYS, 1):
                user32.RegisterHotKey(None, i, 0x4000, vk)  # MOD_NOREPEAT; fails quietly if another app owns the key
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312 and 1 <= msg.wParam <= len(self.KEYS):  # WM_HOTKEY
                    GLib.idle_add(self.callback, self.KEYS[msg.wParam - 1][1])
        except Exception as e:  # the player works fine without media-key support
            print("Media key support unavailable:", e, file=sys.stderr)


# glibc keeps a separate heap ("arena") per thread, and the library-scan / artwork threads would each
# leave a big one resident. Two arenas is plenty here; trim_memory() hands freed pages back to the OS.
try:
    import ctypes
    _libc = ctypes.CDLL("libc.so.6")
    _libc.mallopt(-8, 2)  # M_ARENA_MAX
except Exception:  # not glibc - nothing to tune
    _libc = None


def trim_memory():
    gc.collect(1)  # young generations only: a full collect can stall scrolling for a moment
    if _libc is not None:
        try:
            _libc.malloc_trim(0)
        except Exception:
            pass
    return True
import mutagen.flac  # noqa: E402

APP_NAME = "Beet Music"
APP_ID = "io.github.aliqureshi.BeetMusic"
# MusicBrainz asks every app to identify itself with contact details.
USER_AGENT = "BeetMusic/1.0 ( your-email@example.com )"

AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav"}
COVER_STEMS = {"cover", "folder", "front", "album", "albumart"}
COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
THEMES = [("system", "Follow System"), ("light", "Light"), ("dark", "Dark")]
def apply_color_scheme(name):
    """Apply the light / dark / system choice. On Linux 'system' is handled by libadwaita; on Windows
    libadwaita can't see the OS setting, so it is read from the registry here."""
    scheme = SCHEMES[name]
    if name == "system" and IS_WINDOWS:
        dark = windows_prefers_dark()
        if dark is not None:
            scheme = Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT
    Adw.StyleManager.get_default().set_color_scheme(scheme)


SCHEMES = {"system": Adw.ColorScheme.DEFAULT,  # follow the desktop's light/dark setting
           "light": Adw.ColorScheme.FORCE_LIGHT,
           "dark": Adw.ColorScheme.FORCE_DARK}
# MPRIS: the standard D-Bus interface desktops use to send media keys and
# Bluetooth/wired headphone buttons (play/pause, next, previous) to a player.
MPRIS_NAME = "org.mpris.MediaPlayer2." + APP_ID
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_ROOT_IFACE = "org.mpris.MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
MPRIS_ROOT_XML = """<node><interface name="org.mpris.MediaPlayer2">
<method name="Raise"/><method name="Quit"/>
<property name="CanQuit" type="b" access="read"/>
<property name="CanRaise" type="b" access="read"/>
<property name="HasTrackList" type="b" access="read"/>
<property name="Identity" type="s" access="read"/>
<property name="DesktopEntry" type="s" access="read"/>
<property name="SupportedUriSchemes" type="as" access="read"/>
<property name="SupportedMimeTypes" type="as" access="read"/>
</interface></node>"""
MPRIS_PLAYER_XML = """<node><interface name="org.mpris.MediaPlayer2.Player">
<method name="Next"/><method name="Previous"/><method name="Pause"/>
<method name="PlayPause"/><method name="Stop"/><method name="Play"/>
<method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
<method name="SetPosition"><arg direction="in" name="TrackId" type="o"/><arg direction="in" name="Position" type="x"/></method>
<method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
<signal name="Seeked"><arg name="Position" type="x"/></signal>
<property name="PlaybackStatus" type="s" access="read"/>
<property name="LoopStatus" type="s" access="readwrite"/>
<property name="Rate" type="d" access="read"/>
<property name="Shuffle" type="b" access="read"/>
<property name="Metadata" type="a{sv}" access="read"/>
<property name="Volume" type="d" access="readwrite"/>
<property name="Position" type="x" access="read"/>
<property name="MinimumRate" type="d" access="read"/>
<property name="MaximumRate" type="d" access="read"/>
<property name="CanGoNext" type="b" access="read"/>
<property name="CanGoPrevious" type="b" access="read"/>
<property name="CanPlay" type="b" access="read"/>
<property name="CanPause" type="b" access="read"/>
<property name="CanSeek" type="b" access="read"/>
<property name="CanControl" type="b" access="read"/>
</interface></node>"""
SORT_MODES = ["Year (newest first)", "Year (oldest first)", "Title (A–Z)", "Artist (A–Z)"]

CONFIG = os.path.join(GLib.get_user_config_dir(), "beet-music.json")
PL_FILE = os.path.join(GLib.get_user_config_dir(), "beet-music-playlists.json")
FAV_FILE = os.path.join(GLib.get_user_config_dir(), "beet-music-favorites.json")
CACHE_ROOT = os.path.join(GLib.get_user_cache_dir(), "beet-music")
COVER_DIR = os.path.join(CACHE_ROOT, "covers")
MISS_FILE = os.path.join(CACHE_ROOT, "misses.json")
MISS_TTL = 7 * 24 * 3600  # retry albums with no online match after a week
THUMB = 320
TILE = 165
ACCENT = "#2FDE6B"   # player accent colour
BAR_SPACE = 96       # bottom padding so content can scroll clear of the floating player
GLASS_BLUR = 7.5     # px - same as CSS backdrop-filter: blur(7.5px), used in light and dark
MINI_BLUR = 6.0     # px - blur for the mini player's glass panel
RESP_MIN_WIDTH = 500  # px - the window can't be made narrower than this
RESP_LABELS = 900   # px - below this the top-bar tabs show icons only
RESP_FULL = 960     # px - below this the volume slider is hidden
RESP_COMPACT = 700  # px - below this the player bar stacks into two rows
MINI_SIZE = 135     # px - the mini player is a square window this wide

CSS = """
/* Keep GTK/libadwaita accent tokens consistent across widgets. */
@define-color accent_color #2FDE6B;
@define-color accent_bg_color #2FDE6B;
@define-color accent_fg_color #000000;
@define-color theme_selected_bg_color #2FDE6B;
@define-color theme_selected_fg_color #000000;
@define-color theme_unfocused_selected_bg_color #2FDE6B;
@define-color theme_unfocused_selected_fg_color #000000;

/* Songs: replace libadwaita's default grey/blue hover and selection. */
listview.songs-list > row:hover,
listview.songs-list > row:hover:not(:selected),
listview.songs-list > row:selected,
listview.songs-list > row:selected:hover,
listview.songs-list > row:focus,
listview.songs-list > row:focus-visible {
    background-color: #2FDE6B;
    background-image: none;
    color: #000000;
    box-shadow: none;
}
listview.songs-list > row:hover label,
listview.songs-list > row:selected label,
listview.songs-list > row:focus label,
listview.songs-list > row:hover image,
listview.songs-list > row:selected image {
    color: #000000;
}
/* Search entry focus ring, including the actual inner text node. */
window searchentry:focus-within,
window searchentry:focus,
window entry:focus-within,
window entry:focus {
    outline-color: #2FDE6B;
    border-color: #2FDE6B;
    box-shadow: 0 0 0 1px #2FDE6B;
}
window searchentry selection,
window entry selection,
window text selection {
    background-color: #2FDE6B;
    color: #000000;
}
/* Consistent keyboard focus and interactive highlights. */
window button:focus-visible,
window scale:focus-visible,
window switch:focus-visible,
window checkbutton:focus-visible,
window dropdown:focus-visible,
window row:focus-visible {
    outline: 2px solid #2FDE6B;
    outline-offset: 2px;
}
window scale highlight,
window progressbar progress,
window switch:checked slider,
window switch:checked {
    background-color: #2FDE6B;
}
window checkbutton:checked check,
window checkbutton:checked radio,
window radio:checked {
    background-color: #2FDE6B;
    color: #000000;
}
/* True black/white application surfaces, including all navigation headers. */
window.beet-dark, window.beet-dark .background,
window.beet-dark toolbarview, window.beet-dark navigationview,
window.beet-dark headerbar, window.beet-dark .top-bar,
window.beet-dark searchbar {
    background-color: #000000;
    background-image: none;
}
window.beet-light, window.beet-light .background,
window.beet-light toolbarview, window.beet-light navigationview,
window.beet-light headerbar, window.beet-light .top-bar,
window.beet-light searchbar {
    background-color: #FFFFFF;
    background-image: none;
}
/* GTK/libadwaita paints the actual page through nested view stacks,
   scrolled windows and viewports.  Override those surfaces too; otherwise
   libadwaita's default #1d1d20 remains visible behind the album grid. */
window.beet-dark .view, window.beet-dark .background,
window.beet-dark .navigation-view, window.beet-dark .navigation-page,
window.beet-dark .toolbarview, window.beet-dark .content,
window.beet-dark stack, window.beet-dark scrolledwindow,
window.beet-dark scrolledwindow > viewport,
window.beet-dark viewport, window.beet-dark gridview,
window.beet-dark listview, window.beet-dark listbox,
window.beet-dark clamp, window.beet-dark searchbar,
window.beet-dark .undershoot, window.beet-dark .overshoot {
    background-color: #000000;
    background-image: none;
}
window.beet-light .view, window.beet-light .background,
window.beet-light .navigation-view, window.beet-light .navigation-page,
window.beet-light .toolbarview, window.beet-light .content,
window.beet-light stack, window.beet-light scrolledwindow,
window.beet-light scrolledwindow > viewport,
window.beet-light viewport, window.beet-light gridview,
window.beet-light listview, window.beet-light listbox,
window.beet-light clamp, window.beet-light searchbar,
window.beet-light .undershoot, window.beet-light .overshoot {
    background-color: #FFFFFF;
    background-image: none;
}
/* Header controls remain readable and consistent with each appearance. */
window.beet-dark headerbar { color: #FFFFFF; }
window.beet-light headerbar { color: #000000; }
.tile-cover { border-radius: 12px; background-color: alpha(currentColor, 0.08); }
.tile-avatar { border-radius: 9999px; background-color: alpha(currentColor, 0.08); }
gridview.tiles > child { border-radius: 16px; margin: 4px; }
/* Keep album tiles airy and centered in both appearance modes. */
gridview.tiles { padding: 18px 24px 150px; }
window.beet-compact gridview.tiles { padding: 10px 8px 175px; }
window.beet-compact listview.songs-list { padding-bottom: 175px; }
/* Space for the player bar lives inside the scrolled content, so lists scroll under the glass. */
listview.songs-list { padding-bottom: 96px; }
.album-tile-title { font-size: 15px; font-weight: 700; }
/* Album play button, measured from the supplied references (660px tile): a frosted ring that is
   ~23% of the tile wide, holding a solid disc (65% of the ring) with a rounded play triangle.
   light: ring rgba(255,255,255,0.5) + #DEDEDE edge, black disc, white triangle
   dark:  ring rgba(0,0,0,0.5) + #323232 edge, white disc, black triangle
   The blur behind the ring is drawn by TileGlass. */
button.tile-play {
    min-width: 37px; min-height: 37px; padding: 0; margin: 0;  /* + 1px border each side = 39px, same parity as the 165px tile */
    border-radius: 9999px; background-image: none; box-shadow: none;
    background-color: rgba(255, 255, 255, 0.5); border: 1px solid #DEDEDE;
}
button.tile-play:hover { background-color: rgba(255, 255, 255, 0.64); }
/* the 25px (odd, so it centres exactly in the 37px ring) disc is the Play / Pause artwork itself */
/* Everything that sits on an album (tint, play/pause, heart, menu) only shows while the pointer is over it
   (or while the tile's menu is open). */
.tile-play-layer { opacity: 0; transition: opacity 150ms ease; }
.tile-lead.show-play .tile-play-layer, .tile-lead.menu-open .tile-play-layer { opacity: 1; }
/* Hover tint, measured from the references: white 30% (light) / black 30% (dark). */
.tile-scrim { background-color: rgba(255, 255, 255, 0.3); }
window.beet-dark .tile-scrim { background-color: rgba(0, 0, 0, 0.3); }
/* Heart (top-left) and three-dot menu (bottom-right): bare icons, black in light mode, white in dark mode. */
button.tile-icon, menubutton.tile-icon > button {
    min-width: 28px; min-height: 28px; padding: 0; margin: 0; border-radius: 9999px;
    background-image: none; box-shadow: none; border: none; background-color: transparent; color: #000000;
}
button.tile-icon image, menubutton.tile-icon > button image { -gtk-icon-size: 19px; }
menubutton.tile-dots > button image { -gtk-icon-size: 18px; }
button.tile-icon:hover, menubutton.tile-icon > button:hover { background-color: rgba(0, 0, 0, 0.12); }
window.beet-dark button.tile-icon, window.beet-dark menubutton.tile-icon > button { color: #FFFFFF; }
window.beet-dark button.tile-icon:hover, window.beet-dark menubutton.tile-icon > button:hover { background-color: rgba(255, 255, 255, 0.18); }
button.tile-icon.fav-on, button.tile-icon.fav-on image { color: #2FDE6B; }
window.beet-dark button.tile-play {
    background-color: rgba(0, 0, 0, 0.5); border-color: #323232;
}
window.beet-dark button.tile-play:hover { background-color: rgba(0, 0, 0, 0.64); }
/* Header bar height follows the native GTK4/libadwaita theme. */
viewswitcher button { border-radius: 11px; }
/* Adw.ViewSwitcher uses its own viewswitcherbutton CSS node, not a
   standard Gtk.Button node. Cover GTK/libadwaita version differences. */
window.beet-light viewswitcher viewswitcherbutton:checked,
window.beet-light viewswitcher viewswitcherbutton:selected,
window.beet-light viewswitcher button:checked,
window.beet-light viewswitcher toggle:checked,
window.beet-light viewswitcher .beet-active-tab {
    background-image: none;
    background-color: #E6E6E7;
    box-shadow: none;
}
window.beet-dark viewswitcher viewswitcherbutton:checked,
window.beet-dark viewswitcher viewswitcherbutton:selected,
window.beet-dark viewswitcher button:checked,
window.beet-dark viewswitcher toggle:checked,
window.beet-dark viewswitcher .beet-active-tab {
    background-image: none;
    background-color: #222224;
    box-shadow: none;
}
/* Some libadwaita versions apply the selected state to the child itself. */
window.beet-light viewswitcherbutton:checked { background: #E6E6E7; }
window.beet-dark viewswitcherbutton:checked { background: #222224; }


/* Explicit GTK navigation: selected tab backgrounds in both appearances. */
.beet-navigation button.beet-nav-button {
    border-radius: 11px;
    padding: 9px 13px;
    background: transparent;
    box-shadow: none;
    border: none;
    font-weight: 700;
}
window.beet-light .beet-navigation button.beet-nav-active {
    background: #E6E6E7;
    background-image: none;
    color: #000000;
}
window.beet-dark .beet-navigation button.beet-nav-active {
    background: #222224;
    background-image: none;
    color: #FFFFFF;
}
window.beet-light .beet-navigation button.beet-nav-button:not(.beet-nav-active) {
    color: #000000;
}
window.beet-dark .beet-navigation button.beet-nav-button:not(.beet-nav-active) {
    color: #FFFFFF;
}

/* Prevent libadwaita's hover foreground from turning white on white.
   Apply to both the button and its icon/label descendants. */
window.beet-light .beet-navigation button.beet-nav-button:hover,
window.beet-light .beet-navigation button.beet-nav-button:focus,
window.beet-light .beet-navigation button.beet-nav-button:active {
    color: #000000;
    background-color: #E6E6E7;
    background-image: none;
}
window.beet-light .beet-navigation button.beet-nav-button:hover label,
window.beet-light .beet-navigation button.beet-nav-button:hover image,
window.beet-light .beet-navigation button.beet-nav-button:focus label,
window.beet-light .beet-navigation button.beet-nav-button:focus image,
window.beet-light .beet-navigation button.beet-nav-button:active label,
window.beet-light .beet-navigation button.beet-nav-button:active image {
    color: #000000;
}
window.beet-dark .beet-navigation button.beet-nav-button:hover,
window.beet-dark .beet-navigation button.beet-nav-button:focus,
window.beet-dark .beet-navigation button.beet-nav-button:active {
    color: #FFFFFF;
    background-color: #222224;
    background-image: none;
}
window.beet-dark .beet-navigation button.beet-nav-button:hover label,
window.beet-dark .beet-navigation button.beet-nav-button:hover image,
window.beet-dark .beet-navigation button.beet-nav-button:focus label,
window.beet-dark .beet-navigation button.beet-nav-button:focus image,
window.beet-dark .beet-navigation button.beet-nav-button:active label,
window.beet-dark .beet-navigation button.beet-nav-button:active image {
    color: #FFFFFF;
}

.tile-accent { border-radius: 10px; background-color: #2FDE6B; }
/* Glass spec - light mode:
     background: rgba(255, 255, 255, 0.6); border: 1px solid #DEDEDE;
   dark mode (.glass-dark):
     background: rgba(0, 0, 0, 0.6);       border: 1px solid #323232;
   both: backdrop-filter: blur(7.5px)  -> GlassBackdrop / GLASS_BLUR (GTK has no backdrop-filter)
         border-radius: same as the window (libadwaita's --window-radius; 15px fallback) */
.glass-bar { border-radius: 15px; border-radius: var(--window-radius, 15px); }
.glass-controls {
    background-color: rgba(255, 255, 255, 0.6);
    border: 1px solid #DEDEDE;
    border-radius: 15px;
    border-radius: var(--window-radius, 15px);
    box-shadow: 0 3px 20px alpha(black, 0.15);
    color: #000000;
}
.glass-controls button { color: #000000; }
.glass-controls scale trough { min-height: 6px; background-image: none;
                               background-color: rgba(0, 0, 0, 0.18); }
.glass-controls scale highlight { min-height: 6px; background-image: none;
                                  background-color: #2FDE6B; }
.glass-controls scale slider { min-width: 16px; min-height: 16px; margin: -5px; border: none;
                               background-image: none; background-color: #1D1D1F;
                               box-shadow: 0 1px 3px rgba(0, 0, 0, 0.35); }
/* The play / pause disc is the supplied artwork (black disc in light mode, white in dark), so no fill here. */
.glass-controls button.play-circle { min-width: 38px; min-height: 38px; padding: 0;
                                     border-radius: 9999px; background-image: none;
                                     background-color: transparent; box-shadow: none; }
.glass-controls button.play-circle:hover { background-color: transparent; opacity: 0.85; }
/* Everything on the bar at 100%: no dimmed secondary text, no faded disabled state. */
.glass-controls .dim-label, .glass-controls label, .glass-controls image,
.glass-controls button, .glass-controls scale { opacity: 1; filter: none; }
.glass-controls button:disabled, .glass-controls scale:disabled,
.glass-controls *:disabled { opacity: 1; filter: none; }
/* Dark mode: dark glass, so foreground elements flip back to light. */
.glass-controls.glass-dark {
    background-color: rgba(0, 0, 0, 0.6);
    border: 1px solid #323232;
    color: #FFFFFF;
}
.glass-controls.glass-dark button { color: #FFFFFF; }
.glass-controls.glass-dark scale trough { background-color: rgba(255, 255, 255, 0.22); }
.glass-controls.glass-dark scale slider { background-color: #FFFFFF; }
.glass-controls.glass-dark button.play-circle { background-color: transparent; }
.glass-controls.glass-dark button.play-circle:hover { background-color: transparent; opacity: 0.85; }
.glass-controls button.repeat:checked { color: #2FDE6B; background-color: transparent; }
gridview.tiles > child:hover { background-color: alpha(currentColor, 0.06); }
button.cover-button { padding: 0; min-width: 0; min-height: 0; border-radius: 12px; }
button.cover-button:hover { opacity: 0.8; }
gridview.tiles > child:selected { background-color: alpha(currentColor, 0.12); }


/* Playlist picker is a separate GTK popover surface, not a window child.
   Give the popover itself a transparent background and style its contents
   so no rectangular black backing appears around the rounded menu. */
popover.beet-playlist-popover {
    background: transparent;
    background-image: none;
    box-shadow: none;
    border: none;
}
popover.beet-playlist-popover > contents {
    border-radius: 14px;
    background-clip: padding-box;
    padding: 4px;
    border: 1px solid alpha(currentColor, 0.13);
    box-shadow: 0 6px 20px alpha(black, 0.25);
}
popover.beet-playlist-popover > arrow {
    border: none;
    box-shadow: none;
}
/* Paint ONLY the rounded contents node: coloring the popover root
   creates an opaque rectangular backing outside the rounded corners. */
popover.beet-playlist-popover.beet-popup-dark > contents {
    background: #222224;
    background-image: none;
    color: #FFFFFF;
    border-color: #444448;
}
popover.beet-playlist-popover.beet-popup-light > contents {
    background: #FFFFFF;
    background-image: none;
    color: #000000;
    border-color: #DADADD;
}
popover.beet-playlist-popover button.beet-playlist-item {
    background: transparent;
    background-image: none;
    border: none;
    box-shadow: none;
    border-radius: 9px;
    padding: 10px 12px;
}
popover.beet-playlist-popover.beet-popup-dark button.beet-playlist-item,
popover.beet-playlist-popover.beet-popup-dark button.beet-playlist-item label {
    color: #FFFFFF;
}
popover.beet-playlist-popover.beet-popup-light button.beet-playlist-item,
popover.beet-playlist-popover.beet-popup-light button.beet-playlist-item label {
    color: #000000;
}
popover.beet-playlist-popover.beet-popup-dark button.beet-playlist-item:hover {
    background-color: #39393C;
}
popover.beet-playlist-popover.beet-popup-light button.beet-playlist-item:hover {
    background-color: #E6E6E7;
}

/* Force fixed #2FDE6B accent on suggested-action buttons instead of adaptive system color */
button.suggested-action {
    background-color: #2FDE6B;
    color: #121212;
}
button.suggested-action:hover {
    background-color: alpha(#2FDE6B, 0.85);
}

/* Heart buttons (songs list, album pages, player bar, mini player). */
button.heart-btn image { -gtk-icon-size: 18px; }
button.heart-btn.fav-on, button.heart-btn.fav-on image { color: #2FDE6B; }
.glass-controls button.heart-btn.fav-on { color: #2FDE6B; }
.glass-controls button.repeat image { -gtk-icon-size: 22px; }
.beet-navigation button.beet-nav-button image { -gtk-icon-size: 16px; }

/* Mini player: full-bleed cover inside a 2px frame, frosted glass control panel (135x135) */
window.beet-mini, window.beet-mini.csd { border-radius: 12px; }
window.beet-mini.beet-light { background-color: #FFFFFF; color: #000000; }
window.beet-mini.beet-dark { background-color: #000000; color: #FFFFFF; }
.mini-art.tile-accent { border-radius: 10px; }
.mini-panel { border-radius: 10px; border: 1px solid rgba(255, 255, 255, 0.32); }
window.beet-dark .mini-panel { border-color: rgba(255, 255, 255, 0.12); }
.mini-tint { background-color: rgba(255, 255, 255, 0.5); }
window.beet-dark .mini-tint { background-color: rgba(0, 0, 0, 0.5); }
.mini-body button.mini-btn {
    background: none; background-image: none; box-shadow: none; border: none;
    padding: 0; min-width: 0; min-height: 0; color: inherit;
}
.mini-body button.mini-btn:hover { opacity: 0.65; }
.mini-body button.mini-expand { min-width: 18px; min-height: 18px; }
.mini-body button.mini-expand image { -gtk-icon-size: 18px; }
.mini-body button.mini-skip { min-width: 20px; min-height: 20px; }
.mini-body button.mini-play { min-width: 30px; min-height: 30px; border-radius: 9999px; }
.mini-body button.mini-btn:disabled { opacity: 0.45; }
/* The expand icon sits straight on the artwork, so its colour follows the art under it, not the theme. */
.mini-body button.mini-expand.art-dark { color: #FFFFFF; -gtk-icon-shadow: 0 0 3px rgba(0, 0, 0, 0.65); }
.mini-body button.mini-expand.art-light { color: #000000; -gtk-icon-shadow: 0 0 3px rgba(255, 255, 255, 0.65); }
"""


# ------------------------------------------------- icon / install / migration
ICON_SVG = """<svg width="128" height="128" viewBox="0 0 128 128" fill="none" xmlns="http://www.w3.org/2000/svg">
<rect x="8" y="8" width="112" height="112" rx="26" fill="#2FDE6B"/>
<g transform="translate(37.3 27.6) scale(1.4933)">
<path d="M10.81 46.0272C7.04904 44.4095 3.95273 41.5144 2.04651 37.8333C0.140298 34.1522 -0.458397 29.9118 0.352029 25.8317C1.16245 21.7516 3.33208 18.0832 6.4927 15.449C9.65333 12.8148 13.6103 11.377 17.692 11.3797C18.0418 11.3797 18.3942 11.3901 18.7398 11.4108V18.9668C18.4216 18.9364 18.0962 18.9207 17.7725 18.9207C15.5404 18.9212 13.37 19.6681 11.5938 21.0468C9.81761 22.4255 8.53356 24.3601 7.93833 26.5542C7.34311 28.7483 7.46954 31.0809 8.29826 33.1947C9.12698 35.3085 10.6123 37.087 12.5267 38.2576C14.441 39.4283 16.6789 39.9266 18.8974 39.6762C21.116 39.4258 23.1929 38.4405 24.81 36.8713C26.4271 35.3021 27.4953 33.2355 27.8508 30.9881C28.2063 28.7406 27.8297 26.4362 26.7785 24.4279C26.0119 22.9708 24.9187 21.7191 23.5879 20.7746V12.4068C22.0257 11.8438 20.3938 11.5087 18.7398 11.4108L18.7396 0.858444L18.7448 0.859959C18.9344 0.918618 19.9493 1.22658 21.3102 1.52944C22.6869 1.8358 24.7173 2.20082 26.5739 2.20082C28.6589 2.20082 30.1012 1.74901 30.86 0.858285C31.3389 0.296722 31.7969 0 32.1849 0C32.5715 0 32.8719 0.289469 33.0312 0.815247C33.2389 1.49923 33.1708 2.45323 32.8478 3.36723C32.4744 4.4393 31.7789 5.36389 30.8603 6.00935C29.8507 6.72276 28.6501 7.10162 27.4219 7.09438C26.4888 7.08534 25.5652 6.90165 24.6971 6.55242C24.3128 6.40413 23.9417 6.22244 23.5879 6.00935V12.4068C25.8178 13.2128 27.8665 14.4666 29.6126 16.0947C31.3588 17.7228 32.7669 19.6917 33.7536 21.8848C34.7402 24.0779 35.2854 26.4506 35.3569 28.8624C35.4283 31.2743 35.0245 33.6762 34.1694 35.926C33.3144 38.1759 32.0253 40.2278 30.3787 41.9604C28.7321 43.6931 26.7614 45.0711 24.5832 46.013C22.4051 46.9548 20.0637 47.4414 17.6979 47.4438C15.332 47.4462 12.99 46.9646 10.81 46.0272ZM13.1918 29.4119C13.1918 28.5041 13.4558 27.6166 13.9504 26.8618C14.4449 26.107 15.1478 25.5188 15.9701 25.1714C16.7925 24.824 17.6974 24.7331 18.5704 24.9103C19.4433 25.0874 20.2452 25.5246 20.8746 26.1665C21.504 26.8085 21.9326 27.6263 22.1062 28.5167C22.2798 29.4071 22.1906 30.33 21.85 31.1687C21.5093 32.0074 20.9325 32.7242 20.1924 33.2285C19.4523 33.7329 18.5822 34.002 17.6921 34.002C17.1011 34.002 16.5158 33.8832 15.9698 33.6526C15.4238 33.4219 14.9277 33.0838 14.5098 32.6575C14.092 32.2313 13.7605 31.7253 13.5343 31.1684C13.3082 30.6115 13.1918 30.0146 13.1918 29.4119Z" fill="white"/>
</g>
</svg>
"""

SYMBOLIC_SVG = """<svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
<g transform="translate(2 0) scale(0.3333)">
<path d="M10.81 46.0272C7.04904 44.4095 3.95273 41.5144 2.04651 37.8333C0.140298 34.1522 -0.458397 29.9118 0.352029 25.8317C1.16245 21.7516 3.33208 18.0832 6.4927 15.449C9.65333 12.8148 13.6103 11.377 17.692 11.3797C18.0418 11.3797 18.3942 11.3901 18.7398 11.4108V18.9668C18.4216 18.9364 18.0962 18.9207 17.7725 18.9207C15.5404 18.9212 13.37 19.6681 11.5938 21.0468C9.81761 22.4255 8.53356 24.3601 7.93833 26.5542C7.34311 28.7483 7.46954 31.0809 8.29826 33.1947C9.12698 35.3085 10.6123 37.087 12.5267 38.2576C14.441 39.4283 16.6789 39.9266 18.8974 39.6762C21.116 39.4258 23.1929 38.4405 24.81 36.8713C26.4271 35.3021 27.4953 33.2355 27.8508 30.9881C28.2063 28.7406 27.8297 26.4362 26.7785 24.4279C26.0119 22.9708 24.9187 21.7191 23.5879 20.7746V12.4068C22.0257 11.8438 20.3938 11.5087 18.7398 11.4108L18.7396 0.858444L18.7448 0.859959C18.9344 0.918618 19.9493 1.22658 21.3102 1.52944C22.6869 1.8358 24.7173 2.20082 26.5739 2.20082C28.6589 2.20082 30.1012 1.74901 30.86 0.858285C31.3389 0.296722 31.7969 0 32.1849 0C32.5715 0 32.8719 0.289469 33.0312 0.815247C33.2389 1.49923 33.1708 2.45323 32.8478 3.36723C32.4744 4.4393 31.7789 5.36389 30.8603 6.00935C29.8507 6.72276 28.6501 7.10162 27.4219 7.09438C26.4888 7.08534 25.5652 6.90165 24.6971 6.55242C24.3128 6.40413 23.9417 6.22244 23.5879 6.00935V12.4068C25.8178 13.2128 27.8665 14.4666 29.6126 16.0947C31.3588 17.7228 32.7669 19.6917 33.7536 21.8848C34.7402 24.0779 35.2854 26.4506 35.3569 28.8624C35.4283 31.2743 35.0245 33.6762 34.1694 35.926C33.3144 38.1759 32.0253 40.2278 30.3787 41.9604C28.7321 43.6931 26.7614 45.0711 24.5832 46.013C22.4051 46.9548 20.0637 47.4414 17.6979 47.4438C15.332 47.4462 12.99 46.9646 10.81 46.0272ZM13.1918 29.4119C13.1918 28.5041 13.4558 27.6166 13.9504 26.8618C14.4449 26.107 15.1478 25.5188 15.9701 25.1714C16.7925 24.824 17.6974 24.7331 18.5704 24.9103C19.4433 25.0874 20.2452 25.5246 20.8746 26.1665C21.504 26.8085 21.9326 27.6263 22.1062 28.5167C22.2798 29.4071 22.1906 30.33 21.85 31.1687C21.5093 32.0074 20.9325 32.7242 20.1924 33.2285C19.4523 33.7329 18.5822 34.002 17.6921 34.002C17.1011 34.002 16.5158 33.8832 15.9698 33.6526C15.4238 33.4219 14.9277 33.0838 14.5098 32.6575C14.092 32.2313 13.7605 31.7253 13.5343 31.1684C13.3082 30.6115 13.1918 30.0146 13.1918 29.4119Z" fill="white"/>
</g>
</svg>
"""

DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=Beet Music
Comment=Play your local music library
Exec={exe}
Icon={app_id}
Terminal=false
Categories=AudioVideo;Audio;Player;Music;GTK;
StartupNotify=true
"""


# Shown on album tiles that have no artwork (instead of a generic icon).
PLACEHOLDER_SVG = """<svg width="216" height="290" viewBox="0 0 216 290" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M66.0246 281.341C43.0537 271.453 24.1423 253.757 12.4996 231.256C0.856905 208.755 -2.79977 182.836 2.1501 157.896C7.09998 132.957 20.3515 110.533 39.6558 94.4317C58.9601 78.3301 83.128 69.5419 108.058 69.5586C110.195 69.5586 112.347 69.6219 114.458 69.7481V115.934C112.515 115.749 110.527 115.653 108.55 115.653C94.9166 115.656 81.6605 120.221 70.812 128.648C59.9635 137.076 52.1208 148.901 48.4853 162.312C44.8498 175.724 45.622 189.982 50.6837 202.902C55.7453 215.823 64.8171 226.694 76.5096 233.849C88.2021 241.005 101.87 244.051 115.421 242.52C128.971 240.99 141.656 234.968 151.533 225.376C161.41 215.784 167.934 203.152 170.106 189.414C172.277 175.677 169.977 161.591 163.556 149.316C158.874 140.409 152.197 132.758 144.069 126.984V75.8362C134.528 72.3953 124.56 70.3467 114.458 69.7481L114.457 5.24723L114.489 5.25649C115.646 5.61504 121.845 7.49744 130.157 9.34866C138.566 11.2213 150.967 13.4525 162.307 13.4525C175.041 13.4525 183.85 10.6908 188.485 5.24626C191.41 1.81371 194.207 0 196.577 0C198.938 0 200.773 1.76938 201.746 4.98319C203.015 9.16403 202.599 14.9954 200.626 20.5822C198.345 27.1352 194.097 32.7867 188.487 36.7321C182.32 41.0928 174.988 43.4086 167.486 43.3643C161.787 43.3091 156.146 42.1863 150.843 40.0516C148.496 39.1452 146.23 38.0346 144.069 36.7321V75.8362C157.689 80.7633 170.202 88.4271 180.867 98.3788C191.532 108.331 200.132 120.365 206.158 133.771C212.185 147.176 215.514 161.679 215.951 176.421C216.387 191.164 213.921 205.845 208.698 219.598C203.476 233.35 195.603 245.892 185.546 256.483C175.489 267.073 163.452 275.497 150.148 281.254C136.844 287.011 122.544 289.985 108.094 290C93.6442 290.015 79.3396 287.071 66.0246 281.341ZM80.5723 179.78C80.5724 174.231 82.1847 168.806 85.2052 164.193C88.2257 159.579 92.5188 155.983 97.5415 153.86C102.564 151.736 108.091 151.181 113.423 152.264C118.755 153.347 123.653 156.019 127.497 159.943C131.341 163.867 133.958 168.866 135.019 174.308C136.079 179.751 135.535 185.392 133.454 190.518C131.373 195.645 127.85 200.026 123.33 203.109C118.809 206.192 113.495 207.837 108.059 207.837C104.449 207.837 100.875 207.111 97.5397 205.701C94.2049 204.291 91.1748 202.224 88.6224 199.619C86.0701 197.013 84.0455 193.92 82.6642 190.516C81.283 187.113 80.5721 183.464 80.5723 179.78Z" fill="#868688"/>
</svg>
"""
PLACEHOLDER_FILE = os.path.join(CACHE_ROOT, "placeholder.svg")
_placeholder_icon = None


PLACEHOLDER_WHITE_FILE = os.path.join(CACHE_ROOT, "placeholder-white.svg")
_placeholder_white_icon = None


def _write_if_different(path, content):
    try:
        with open(path) as f:
            if f.read() == content:
                return
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
    except OSError:
        pass


def ensure_placeholder():
    """Write the placeholder logos to the cache so GTK can load them as file icons."""
    _write_if_different(PLACEHOLDER_FILE, PLACEHOLDER_SVG)
    _write_if_different(PLACEHOLDER_WHITE_FILE, SYMBOLIC_SVG)


def placeholder_icon(white=False):
    global _placeholder_icon, _placeholder_white_icon
    if _placeholder_icon is None:
        ensure_placeholder()
        _placeholder_icon = Gio.FileIcon.new(Gio.File.new_for_path(PLACEHOLDER_FILE))
        _placeholder_white_icon = Gio.FileIcon.new(Gio.File.new_for_path(PLACEHOLDER_WHITE_FILE))
    return _placeholder_white_icon if white else _placeholder_icon


# Custom symbolic icons. Each is a square-viewBox SVG so it recolors with the theme
# (white on dark, black on light) like any other symbolic icon.
BEET_ICONS = {
    "albums": """<svg width="34" height="34" viewBox="-2.5 -0 34 34" fill="none" xmlns="http://www.w3.org/2000/svg">
<rect width="12.7591" height="19.1386" rx="2.90496" fill="#020202"/>
<rect x="15.6641" y="15.6641" width="12.7591" height="17.5437" rx="2.90496" fill="#020202"/>
<rect x="15.6641" width="12.7591" height="12.7591" rx="2.90496" fill="#020202"/>
<rect y="22.0436" width="12.7591" height="11.1642" rx="2.90496" fill="#020202"/>
</svg>
""",
    "artists": """<svg width="32" height="32" viewBox="-2 -0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M20.6516 6.76584C20.6516 10.5025 17.6224 13.5317 13.8857 13.5317C10.1491 13.5317 7.11989 10.5025 7.11989 6.76584C7.11989 3.02917 10.1491 0 13.8857 0C17.6224 0 20.6516 3.02917 20.6516 6.76584Z" fill="black"/>
<path d="M0 29.2518C0 21.7404 6.08918 15.6512 13.6006 15.6512C21.112 15.6512 27.2011 21.7404 27.2011 29.2518V31.9394C27.2011 31.9573 27.1866 31.9719 27.1686 31.9719H0.0488145C0.0218544 31.9719 0 31.9501 0 31.9231V29.2518Z" fill="black"/>
</svg>
""",
    "songs": """<svg width="28" height="28" viewBox="-0 -2 28 28" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M23.2656 0C25.5909 0 27.4763 1.61594 27.4766 3.60938V17.1465H27.4199C27.4567 17.4033 27.4766 17.6657 27.4766 17.9326C27.4766 20.9702 25.0141 23.4326 21.9766 23.4326C18.939 23.4326 16.4766 20.9702 16.4766 17.9326C16.4766 14.8951 18.939 12.4326 21.9766 12.4326C22.8141 12.4326 23.6078 12.6202 24.3184 12.9551V3.60938C24.3181 3.11115 23.8468 2.70703 23.2656 2.70703H11.6875C11.1063 2.70703 10.635 3.11115 10.6348 3.60938L11 16C11 17 11 17.2379 11 17.9326C11 20.9702 8.53757 23.4326 5.5 23.4326C2.46243 23.4326 0 20.9702 0 17.9326C3.29856e-05 14.8951 2.46245 12.4326 5.5 12.4326C6.19686 12.4326 6.86303 12.5634 7.47656 12.7998V3.60938C7.4768 1.61594 9.36224 1.45341e-08 11.6875 0H23.2656Z" fill="black"/>
</svg>
""",
    "playlists": """<svg width="28" height="28" viewBox="-0 -0 28 28" fill="none" xmlns="http://www.w3.org/2000/svg">
<rect x="12" y="2" width="16" height="4" rx="2" fill="black"/>
<rect x="12" y="12" width="16" height="4" rx="2" fill="black"/>
<rect x="12" y="22" width="16" height="4" rx="2" fill="black"/>
<circle cx="4" cy="4" r="4" fill="black"/>
<circle cx="4" cy="14" r="4" fill="black"/>
<circle cx="4" cy="24" r="4" fill="black"/>
</svg>
""",
    "favorites": """<svg width="27" height="27" viewBox="-0 -2 27 27" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M14.6937 2.04954C17.5089 -0.683181 22.0733 -0.683181 24.8885 2.04954C27.7038 4.78218 27.7038 9.21272 24.8885 11.9454L23.6949 13.1041L13.5 23L3.30512 13.1041L2.11147 11.9454C-0.703822 9.21272 -0.703822 4.78218 2.11147 2.04954C4.92666 -0.683181 9.49105 -0.683181 12.3062 2.04954L13.5 3.2082L14.6937 2.04954Z" fill="black"/>
</svg>
""",
    "repeat-song": """<svg width="42" height="42" viewBox="-0 -7.5 42 42" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M30.4017 16.0543L35.8258 6.75L41.25 16.0543H37.6391V26.3351C37.6391 26.7025 37.3384 27 36.9679 27H7.42126C7.05037 27 6.75 26.7025 6.75 26.3351V24.0735C6.75 23.7061 7.05037 23.4086 7.42126 23.4086H33.342C33.7125 23.4086 34.0133 23.1108 34.0133 22.7438V16.0543H30.4017Z" fill="black"/>
<path d="M10.8483 10.9457L5.42415 20.25L8.13406e-07 10.9457L3.61085 10.9457L3.61085 0.664869C3.61085 0.297506 3.91158 -2.67412e-06 4.28212 -2.64173e-06L33.8287 -5.86836e-08C34.1996 -2.62589e-08 34.5 0.297508 34.5 0.664871L34.5 2.92649C34.5 3.29386 34.1996 3.59136 33.8287 3.59136L7.908 3.59136C7.53747 3.59136 7.23674 3.88922 7.23674 4.25623L7.23674 10.9457L10.8483 10.9457Z" fill="black"/>
</svg>
""",
    "heart-outline": """<svg width="27" height="27" viewBox="0 -2 27 27" fill="none" xmlns="http://www.w3.org/2000/svg">
<path fill-rule="evenodd" clip-rule="evenodd" d="M14.6937 2.04954C17.5089 -0.683181 22.0733 -0.683181 24.8885 2.04954C27.7038 4.78218 27.7038 9.21272 24.8885 11.9454L23.6949 13.1041L13.5 23L3.30512 13.1041L2.11147 11.9454C-0.703822 9.21272 -0.703822 4.78218 2.11147 2.04954C4.92666 -0.683181 9.49105 -0.683181 12.3062 2.04954L13.5 3.2082L14.6937 2.04954Z M16.62 3.47 L15.14 4.89 L14.78 5.18 L14.38 5.39 L13.95 5.51 L13.73 5.55 L13.27 5.55 L12.83 5.46 L12.62 5.39 L12.22 5.18 L11.86 4.89 L10.38 3.47 L9.91 3.13 L9.24 2.77 L8.52 2.52 L7.78 2.38 L7.02 2.35 L6.27 2.44 L5.53 2.63 L4.84 2.94 L4.51 3.13 L4.19 3.35 L3.75 3.74 L3.48 4.02 L3.24 4.32 L2.85 4.94 L2.57 5.61 L2.41 6.29 L2.35 6.82 L2.36 7.35 L2.44 7.87 L2.52 8.22 L2.70 8.72 L2.85 9.05 L3.13 9.53 L3.36 9.83 L3.77 10.28 L13.50 19.72 L23.39 10.12 L23.76 9.68 L24.15 9.05 L24.43 8.39 L24.59 7.70 L24.65 7.00 L24.59 6.29 L24.43 5.61 L24.15 4.94 L23.76 4.32 L23.52 4.02 L23.25 3.74 L22.96 3.47 L22.65 3.24 L22.33 3.03 L21.99 2.85 L21.29 2.57 L20.55 2.41 L19.79 2.35 L19.04 2.41 L18.30 2.57 L17.59 2.85 L17.26 3.03 L16.93 3.24Z" fill="black"/>
</svg>
""",
    "more": """<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
<circle cx="12" cy="5.6" r="2.5" fill="black"/><circle cx="12" cy="12" r="2.5" fill="black"/><circle cx="12" cy="18.4" r="2.5" fill="black"/>
</svg>
""",
    "play": """<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M9.07 4.96 Q7.20 3.80 7.20 6.00 L7.20 18.00 Q7.20 20.20 9.07 19.04 L18.53 13.16 Q20.40 12.00 18.53 10.84Z" fill="black"/>
</svg>
""",
    "pause": """<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
<rect x="5.2" y="3.8" width="5" height="16.4" rx="1.6" fill="black"/><rect x="13.8" y="3.8" width="5" height="16.4" rx="1.6" fill="black"/>
</svg>
""",
    "previous": """<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
<rect x="3.6" y="4.4" width="3.2" height="15.2" rx="1.4" fill="black"/><path d="M18.81 5.25 Q20.40 4.20 20.40 6.10 L20.40 17.90 Q20.40 19.80 18.81 18.75 L10.19 13.05 Q8.60 12.00 10.19 10.95Z" fill="black"/>
</svg>
""",
    "next": """<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
<rect x="17.2" y="4.4" width="3.2" height="15.2" rx="1.4" fill="black"/><path d="M5.19 5.25 Q3.60 4.20 3.60 6.10 L3.60 17.90 Q3.60 19.80 5.19 18.75 L13.81 13.05 Q15.40 12.00 13.81 10.95Z" fill="black"/>
</svg>
""",
    "close": """<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
<rect x="2.2" y="10.6" width="19.6" height="2.8" rx="1.4" transform="rotate(45 12 12)" fill="black"/><rect x="2.2" y="10.6" width="19.6" height="2.8" rx="1.4" transform="rotate(-45 12 12)" fill="black"/>
</svg>
""",
    "expand": """<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
<rect x="2.6" y="10.8" width="18.8" height="2.4" rx="1.2" transform="rotate(-45 12 12)" fill="black"/><path d="M19.60 3.60 Q20.40 3.60 20.40 4.40 L20.40 11.60 Q20.40 12.40 19.83 11.83 L12.17 4.17 Q11.60 3.60 12.40 3.60Z" fill="black"/><path d="M4.40 20.40 Q3.60 20.40 3.60 19.60 L3.60 12.40 Q3.60 11.60 4.17 12.17 L11.83 19.83 Q12.40 20.40 11.60 20.40Z" fill="black"/>
</svg>
""",
    "mini-player": """<svg width="31" height="31" viewBox="0 -2 31 31" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M24.5 0C28.0899 0 31 2.91015 31 6.5V20.5C31 24.0899 28.0899 27 24.5 27H6.5C2.91015 27 0 24.0899 0 20.5V6.5C0 2.91015 2.91015 0 6.5 0H24.5ZM6.5 3C4.567 3 3 4.567 3 6.5V20.5C3 22.433 4.567 24 6.5 24H24.5C26.433 24 28 22.433 28 20.5V6.5C28 4.567 26.433 3 24.5 3H6.5ZM22.7168 12.0371C23.5452 12.0371 24.2168 12.7087 24.2168 13.5371V19.5371C24.2167 20.3655 23.5452 21.0371 22.7168 21.0371H14.2168C13.3884 21.0371 12.7169 20.3655 12.7168 19.5371V13.5371C12.7168 12.7087 13.3884 12.0371 14.2168 12.0371H22.7168Z" fill="black"/>
</svg>
""",
}


def ensure_icons():
    """Write the custom icons into a private icon-theme folder and register it with GTK."""
    root = os.path.join(CACHE_ROOT, "icons")
    folder = os.path.join(root, "hicolor", "symbolic", "apps")
    for key, svg in BEET_ICONS.items():
        _write_if_different(os.path.join(folder, f"beet-{key}-symbolic.svg"), svg)
    Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(root)


def _heart_set(btn, on):
    """Show a button as a filled (favorite) or outlined heart."""
    btn.set_icon_name("beet-favorites-symbolic" if on else "beet-heart-outline-symbolic")
    if on:
        btn.add_css_class("fav-on")
    else:
        btn.remove_css_class("fav-on")
    btn.set_tooltip_text("Remove from Favorites" if on else "Add to Favorites")


def _make_heart_button():
    btn = Gtk.Button(icon_name="beet-heart-outline-symbolic", valign=Gtk.Align.CENTER,
                     tooltip_text="Add to Favorites")
    btn.add_css_class("flat")
    btn.add_css_class("heart-btn")
    return btn


# Logo shown on the loading / empty-library screen.
LOADING_LOGO_SVG = """<svg width="291" height="382" viewBox="0 0 291 382" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M88.6553 370.113C57.8109 357.105 32.4173 333.825 16.784 304.225C1.15062 274.624 -3.75943 240.526 2.88708 207.718C9.53358 174.909 27.3272 145.41 53.2483 124.228C79.1694 103.046 111.621 91.4847 145.096 91.5067C147.966 91.5067 150.855 91.59 153.69 91.756V152.515C151.08 152.271 148.412 152.145 145.756 152.145C127.45 152.149 109.651 158.155 95.0836 169.241C80.5167 180.328 69.9858 195.884 65.1042 213.527C60.2227 231.17 61.2595 249.927 68.0561 266.925C74.8526 283.922 87.034 298.223 102.734 307.637C118.434 317.05 136.788 321.057 154.983 319.044C173.178 317.03 190.211 309.108 203.473 296.489C216.735 283.871 225.495 267.253 228.411 249.181C231.327 231.108 228.238 212.579 219.617 196.43C213.33 184.713 204.364 174.647 193.45 167.052V99.765C180.639 95.2384 167.253 92.5415 153.688 91.7541V6.90291L153.731 6.91509C155.286 7.38678 163.609 9.86313 174.77 12.2985C186.061 14.762 202.713 17.6972 217.939 17.6972C235.039 17.6972 246.867 14.0641 253.09 6.90163C257.018 2.38599 260.774 0 263.956 0C267.127 0 269.59 2.32767 270.897 6.55555C272.601 12.0556 272.042 19.7269 269.393 27.0765C266.331 35.6972 260.626 43.1321 253.093 48.3223C244.813 54.059 234.967 57.1054 224.894 57.0472C217.241 56.9746 209.666 55.4974 202.547 52.6893C199.395 51.4968 196.352 50.0358 193.45 48.3223V99.7618C211.738 106.244 228.54 116.329 242.861 129.421C257.181 142.512 268.729 158.345 276.821 175.98C284.913 193.615 289.385 212.694 289.97 232.088C290.556 251.482 287.245 270.797 280.232 288.888C273.219 306.979 262.648 323.479 249.144 337.412C235.639 351.344 219.477 362.425 201.613 369.999C183.749 377.573 164.548 381.485 145.145 381.505C125.742 381.524 106.532 377.65 88.6534 370.112L88.6553 370.113ZM108.189 236.506C108.19 229.206 110.355 222.07 114.41 216.001C118.466 209.932 124.231 205.201 130.975 202.408C137.719 199.614 145.14 198.884 152.3 200.308C159.46 201.732 166.036 205.248 171.198 210.41C176.359 215.572 179.874 222.149 181.298 229.308C182.722 236.468 181.991 243.889 179.197 250.633C176.403 257.377 171.672 263.141 165.603 267.197C159.533 271.252 152.397 273.417 145.097 273.416C140.25 273.416 135.451 272.461 130.973 270.606C126.495 268.751 122.426 266.033 118.999 262.605C115.572 259.178 112.853 255.109 110.998 250.631C109.144 246.153 108.189 241.353 108.189 236.506V236.506Z" fill="#868688"/>
</svg>
"""
LOADING_LOGO_FILE = os.path.join(CACHE_ROOT, "loading-logo.svg")


def loading_logo_paintable(size=128):
    """Crisp vector paintable of the Beet logo for the loading status page."""
    _write_if_different(LOADING_LOGO_FILE, LOADING_LOGO_SVG)
    gicon = Gio.FileIcon.new(Gio.File.new_for_path(LOADING_LOGO_FILE))
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    return theme.lookup_by_gicon(gicon, size, 1, Gtk.TextDirection.NONE, 0)


def _icon_path():
    return os.path.join(GLib.get_user_data_dir(), "icons", "hicolor", "scalable", "apps",
                        APP_ID + ".svg")


def ensure_icon():
    """Make sure the app icon is in the user's icon theme."""
    path = _icon_path()
    try:
        with open(path) as f:
            if f.read() == ICON_SVG:
                return
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(ICON_SVG)
    except OSError:
        pass


def install_app():
    """Install icon, launcher script and menu entry into ~/.local."""
    ensure_icon()
    sym_dir = os.path.join(GLib.get_user_data_dir(), "icons", "hicolor", "symbolic", "apps")
    os.makedirs(sym_dir, exist_ok=True)
    with open(os.path.join(sym_dir, APP_ID + "-symbolic.svg"), "w") as f:
        f.write(SYMBOLIC_SVG)
    apps = os.path.join(GLib.get_user_data_dir(), "applications")
    bin_dir = os.path.expanduser("~/.local/bin")
    os.makedirs(apps, exist_ok=True)
    os.makedirs(bin_dir, exist_ok=True)
    exe = os.path.join(bin_dir, "beet-music")
    if os.path.abspath(__file__) != exe:
        shutil.copyfile(os.path.abspath(__file__), exe)
    os.chmod(exe, 0o755)
    with open(os.path.join(apps, APP_ID + ".desktop"), "w") as f:
        f.write(DESKTOP_ENTRY.format(exe=exe, app_id=APP_ID))
    for cmd in (["update-desktop-database", apps],
                ["gtk-update-icon-cache", "-f", "-t",
                 os.path.join(GLib.get_user_data_dir(), "icons", "hicolor")]):
        try:
            subprocess.run(cmd, check=False, capture_output=True)
        except OSError:
            pass
    print(f"Installed {APP_NAME}: launcher {exe}, menu entry and icon in ~/.local/share")


def migrate_legacy():
    """Keep playlists, settings and cached covers from the old 'Tunebox' name."""
    cfg, cache = GLib.get_user_config_dir(), GLib.get_user_cache_dir()
    for old, new in ((os.path.join(cfg, "tunebox.json"), CONFIG),
                     (os.path.join(cfg, "tunebox-playlists.json"), PL_FILE),
                     (os.path.join(cache, "tunebox"), CACHE_ROOT)):
        if os.path.exists(old) and not os.path.exists(new):
            try:
                os.rename(old, new)
            except OSError:
                pass


# ---------------------------------------------------------------- helpers
def first(tags, key, default=""):
    v = tags.get(key)
    return str(v[0]).strip() if v else default


def parse_year(s):
    m = re.match(r"\s*(\d{4})", s or "")
    return int(m.group(1)) if m else 0


def parse_int(s):
    try:
        return int(str(s).split("/")[0])
    except ValueError:
        return 0


def fmt_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def fmt_long(sec):
    m = round(sec / 60)
    return f"{m // 60} hr {m % 60} min" if m >= 60 else f"{max(m, 1)} min"


def sign(a, b):
    return (a > b) - (a < b)


def esc(s):
    return GLib.markup_escape_text(s)


def cover_key(artist, title):
    return hashlib.sha1(f"{artist}\x00{title}".casefold().encode()).hexdigest()


def cover_file_for(key):
    return os.path.join(COVER_DIR, key + ".png")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


# ---------------------------------------------------------------- artwork
def make_thumb(data, dest):
    """Decode image bytes, downscale to THUMB px, save as PNG in the cache."""
    try:
        loader = GdkPixbuf.PixbufLoader()
        loader.write(data)
        loader.close()
        pb = loader.get_pixbuf()
        if pb is None:
            return False
        w, h = pb.get_width(), pb.get_height()
        scale = THUMB / max(w, h)
        if scale < 1:
            pb = pb.scale_simple(max(1, int(w * scale)), max(1, int(h * scale)),
                                 GdkPixbuf.InterpType.BILINEAR)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        pb.savev(dest, "png", [], [])
        return True
    except Exception:
        return False


def embedded_art(path):
    try:
        f = mutagen.File(path)
        if f is None:
            return None
        pics = getattr(f, "pictures", None)  # FLAC
        if pics:
            return pics[0].data
        tags = f.tags
        if tags is None:
            return None
        if hasattr(tags, "getall"):  # ID3 (MP3)
            apic = tags.getall("APIC")
            if apic:
                return apic[0].data
        if "covr" in tags:  # MP4 / M4A
            return bytes(tags["covr"][0])
        for b64 in tags.get("metadata_block_picture", []):  # Ogg / Opus
            return mutagen.flac.Picture(base64.b64decode(b64)).data
    except Exception:
        pass
    return None


def folder_art(directory):
    try:
        for name in sorted(os.listdir(directory)):
            stem, ext = os.path.splitext(name.lower())
            if stem in COVER_STEMS and ext in COVER_EXTS:
                with open(os.path.join(directory, name), "rb") as f:
                    return f.read(16_000_000)
    except OSError:
        pass
    return None


def _http_get(url, limit=8_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read(limit)


def _norm(s):
    return re.sub(r"[\W_]+", "", s.casefold())


def _similar(a, b):
    na, nb = _norm(a), _norm(b)
    return bool(na and nb and (na in nb or nb in na))


def _lucene(s):
    return re.sub(r'(["\\])', r"\\\1", s)


def fetch_cover_bytes(title, artist):
    """Look up cover art online. Returns (image_bytes_or_None, service_reached)."""
    known = artist != "Unknown Artist"
    reached = False

    # 1) MusicBrainz release-group search -> Cover Art Archive
    try:
        q = f'releasegroup:"{_lucene(title)}"'
        if known:
            q += f' AND artist:"{_lucene(artist)}"'
        url = "https://musicbrainz.org/ws/2/release-group/?" + urllib.parse.urlencode(
            {"query": q, "fmt": "json", "limit": 5})
        data = json.loads(_http_get(url))
        reached = True
        for rg in data.get("release-groups", []):
            if rg.get("score", 0) < 85 or not _similar(rg.get("title", ""), title):
                continue
            try:
                return _http_get(f"https://coverartarchive.org/release-group/{rg['id']}/front-500"), True
            except Exception:
                continue  # no front cover for this release group
    except urllib.error.HTTPError:
        reached = True
    except Exception:
        pass

    # 2) iTunes Search API fallback (no key needed)
    try:
        term = f"{artist} {title}" if known else title
        url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": term, "entity": "album", "media": "music", "limit": 5})
        results = json.loads(_http_get(url)).get("results", [])
        reached = True
        for r in results:
            if not _similar(r.get("collectionName", ""), title):
                continue
            if known and not _similar(r.get("artistName", ""), artist):
                continue
            art = r.get("artworkUrl100", "").replace("100x100bb", "600x600bb")
            if art:
                return _http_get(art), True
    except urllib.error.HTTPError:
        reached = True
    except Exception:
        pass
    return None, reached


# ------------------------------------------------------------ data model
class Track(GObject.Object):
    favorite = GObject.Property(type=bool, default=False)

    def __init__(self, path, title, artist, number, disc, length):
        super().__init__()
        self.path, self.title, self.artist = path, title, artist
        self.number, self.disc, self.length = number, disc, length
        self.album = None
        self.haystack = ""  # lower-cased "title artist album" used by song search


# Cover textures are decoded pixels (a 320px cover is ~300 KB), so they are loaded at the size that is
# actually drawn and only while something is showing them. Widgets call acquire_cover() when they bind
# an album/artist and release_cover() when they let go; at zero the texture is dropped again.
COVER_KINDS = {"cover": TILE, "cover_small": 48, "cover_large": 200}  # property -> logical px
_COVER_SCALE = [1]  # device pixels per logical pixel on the sharpest monitor


def detect_cover_scale():
    try:
        mons = Gdk.Display.get_default().get_monitors()
        scale = max((mons.get_item(i).get_scale() for i in range(mons.get_n_items())), default=1.0)
        _COVER_SCALE[0] = max(1, math.ceil(scale))
    except Exception:
        _COVER_SCALE[0] = 1


def cover_pixels(logical):
    return min(THUMB, max(16, math.ceil(logical * _COVER_SCALE[0])))


def load_cover_pixbuf(path, logical):
    px = cover_pixels(logical)
    return GdkPixbuf.Pixbuf.new_from_file_at_scale(path, px, px, True)


# Decoding artwork is slow, so it never happens on the UI thread while scrolling: a small worker
# pool decodes, and recently used textures stay in a byte-limited LRU cache so scrolling back is instant.
COVER_CACHE_BYTES = 64 * 1024 * 1024
_cover_cache = collections.OrderedDict()  # (path, mtime_ns, px) -> (texture, bytes)
_cover_cache_size = [0]
_cover_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="beet-cover")


def _cover_key(path, logical):
    try:
        return (path, os.stat(path).st_mtime_ns, cover_pixels(logical))
    except OSError:
        return None


def _cover_cache_get(key):
    hit = _cover_cache.get(key)
    if hit is None:
        return None
    _cover_cache.move_to_end(key)
    return hit[0]


def _cover_cache_put(key, tex):
    if key in _cover_cache:
        return
    size = tex.get_width() * tex.get_height() * 4
    _cover_cache[key] = (tex, size)
    _cover_cache_size[0] += size
    while _cover_cache_size[0] > COVER_CACHE_BYTES and len(_cover_cache) > 1:
        _k, (_t, sz) = _cover_cache.popitem(last=False)
        _cover_cache_size[0] -= sz


class CoverMixin:
    def _init_covers(self):
        self._cover_refs = {kind: 0 for kind in COVER_KINDS}
        self._cover_pending = set()
        self._cover_gen = 0

    def acquire_cover(self, kind="cover"):
        self._cover_refs[kind] += 1
        if self.get_property(kind) is None:
            self._load_cover(kind)

    def release_cover(self, kind="cover"):
        n = max(0, self._cover_refs[kind] - 1)
        self._cover_refs[kind] = n
        if n == 0:
            self.set_property(kind, None)

    def refresh_covers(self):
        """The artwork file changed: reload whatever is currently on screen (notifies bound widgets)."""
        self._cover_gen += 1  # results of loads that were already running are now stale
        self._cover_pending.clear()
        for kind, n in self._cover_refs.items():
            if n:
                self.set_property(kind, None)
                self._load_cover(kind)

    def _load_cover(self, kind):
        path = self._cover_source()
        if not path or kind in self._cover_pending:
            return
        logical = COVER_KINDS[kind]
        key = _cover_key(path, logical)
        if key is None:
            self._cover_failed()
            return
        tex = _cover_cache_get(key)
        if tex is not None:  # decoded recently: no work at all
            self.set_property(kind, tex)
            return
        self._cover_pending.add(kind)
        _cover_pool.submit(self._decode_cover, kind, path, logical, key, self._cover_gen)

    def _decode_cover(self, kind, path, logical, key, gen):  # worker thread
        tex = None
        if self._cover_refs[kind] > 0 and gen == self._cover_gen:  # skip tiles already scrolled away
            try:
                tex = Gdk.Texture.new_for_pixbuf(load_cover_pixbuf(path, logical))
            except Exception:
                tex = False
        GLib.idle_add(self._cover_ready, kind, path, key, gen, tex)

    def _cover_ready(self, kind, path, key, gen, tex):  # UI thread
        if gen != self._cover_gen:
            return False
        self._cover_pending.discard(kind)
        if tex is False:
            self._cover_failed()
        elif tex is not None:
            _cover_cache_put(key, tex)
            if self._cover_refs[kind] > 0 and self.get_property(kind) is None:
                self.set_property(kind, tex)
        elif self._cover_refs[kind] > 0 and self.get_property(kind) is None:
            self._load_cover(kind)  # was skipped as off-screen but is wanted again
        return False


class Album(CoverMixin, GObject.Object):
    cover = GObject.Property(type=Gdk.Texture)
    cover_small = GObject.Property(type=Gdk.Texture)
    cover_large = GObject.Property(type=Gdk.Texture)

    def __init__(self, title, artist, year):
        super().__init__()
        self._init_covers()
        self.title, self.artist, self.year = title, artist, year
        self.tracks = []
        self.key = cover_key(artist, title)
        path = cover_file_for(self.key)
        self.cover_file = path if os.path.exists(path) else None

    def _cover_source(self):
        return self.cover_file

    def _cover_failed(self):
        self.cover_file = None


class Artist(CoverMixin, GObject.Object):
    cover = GObject.Property(type=Gdk.Texture)
    cover_small = GObject.Property(type=Gdk.Texture)
    cover_large = GObject.Property(type=Gdk.Texture)

    def __init__(self, name):
        super().__init__()
        self._init_covers()
        self.name = name
        self.albums = []

    def _cover_source(self):  # an artist shows the artwork of their newest album that has any
        for a in sorted(self.albums, key=lambda x: -x.year):
            if a.cover_file:
                return a.cover_file
        return None

    def _cover_failed(self):
        pass


# ----------------------------------------------------------- library index
# The scanned library is saved to disk. On launch it loads instantly from that
# saved index; only new, changed or removed files are ever read again.
LIB_FILE = os.path.join(CACHE_ROOT, "library.json")
INDEX_VERSION = 1


def load_index(folder):
    data = load_json(LIB_FILE, {})
    if isinstance(data, dict) and data.get("version") == INDEX_VERSION and data.get("folder") == folder:
        return {"folder": folder, "files": data.get("files", {}),
                "art_checked": set(data.get("art_checked", []))}
    return {"folder": folder, "files": {}, "art_checked": set()}


def save_index(index):
    save_json(LIB_FILE, {"version": INDEX_VERSION, "folder": index["folder"],
                         "files": index["files"], "art_checked": sorted(index["art_checked"])})


def list_audio(folder):
    """Cheap pass: path -> (mtime_ns, size) for every audio file. No tags are read."""
    found = {}
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                path = os.path.join(root, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                found[path] = (st.st_mtime_ns, st.st_size)
    return found


def read_record(path, mtime, size):
    rec = {"m": mtime, "s": size}
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        f = None
    if f is None:
        rec["bad"] = True  # unreadable; remembered so it isn't retried every launch
        return rec
    t = f.tags or {}
    album = first(t, "album") or "Unknown Album"
    artist = first(t, "albumartist") or first(t, "artist") or "Unknown Artist"
    rec.update(
        album=album, artist=artist,
        year=parse_year(first(t, "originaldate") or first(t, "date")),
        title=first(t, "title") or os.path.splitext(os.path.basename(path))[0],
        tartist=first(t, "artist") or artist,
        number=parse_int(first(t, "tracknumber")),
        disc=parse_int(first(t, "discnumber")),
        length=getattr(f.info, "length", 0) or 0,
    )
    return rec


def rec_key(rec):
    return cover_key(rec["artist"], rec["album"])


def sync_library(folder, index, on_work=lambda: None):
    """Bring the saved index up to date. Only new/changed files are read."""
    files = index["files"]
    found = list_audio(folder)
    removed = [p for p in files if p not in found]
    todo = [p for p, sig in found.items()
            if p not in files or (files[p]["m"], files[p]["s"]) != tuple(sig)]
    stats = {"added": sum(1 for p in todo if p not in files),
             "updated": sum(1 for p in todo if p in files),
             "removed": len(removed), "changed": bool(todo or removed)}
    if not stats["changed"]:
        return stats
    on_work()
    touched = set()
    for p in removed:
        rec = files.pop(p)
        if not rec.get("bad"):
            touched.add(rec_key(rec))
    for p in todo:
        old = files.get(p)
        if old and not old.get("bad"):
            touched.add(rec_key(old))
        rec = read_record(p, *found[p])
        files[p] = rec
        if not rec.get("bad"):
            touched.add(rec_key(rec))
    index["art_checked"] -= touched  # look for artwork again on albums that changed
    index["dirty"] = True
    return stats


def build_albums(index):
    """Turn the saved index into Album/Track objects (fast: no tag reading)."""
    albums = {}
    files = index["files"]
    for path in sorted(files):
        rec = files[path]
        if rec.get("bad"):
            continue
        key = (rec["album"].casefold(), rec["artist"].casefold())
        album = albums.get(key)
        if album is None:
            album = albums[key] = Album(rec["album"], rec["artist"], rec["year"])
        elif not album.year and rec["year"]:
            album.year = rec["year"]
        track = Track(path, rec["title"], rec["tartist"], rec["number"], rec["disc"], rec["length"])
        track.album = album
        album.tracks.append(track)

    checked = index["art_checked"]
    for album in albums.values():
        album.tracks.sort(key=lambda x: (x.disc, x.number or 9999, x.title.casefold()))
        if album.cover_file or album.key in checked:
            continue
        # local artwork once per album: cover.jpg / folder.jpg, then art embedded in the tags
        data = folder_art(os.path.dirname(album.tracks[0].path))
        if not data:
            for tr in album.tracks[:3]:
                data = embedded_art(tr.path)
                if data:
                    break
        if data and make_thumb(data, cover_file_for(album.key)):
            album.cover_file = cover_file_for(album.key)
        checked.add(album.key)
        index["dirty"] = True
    return list(albums.values())


def album_cmp(mode):
    """Compare function for Gtk.CustomSorter. Modes follow SORT_MODES."""
    def cmp(a, b, _data=None):
        ta = (a.title.casefold(), a.artist.casefold())
        tb = (b.title.casefold(), b.artist.casefold())
        if mode in (0, 1):  # year; albums with no year always go last
            if a.year != b.year:
                if not a.year:
                    return 1
                if not b.year:
                    return -1
                r = sign(a.year, b.year)
                return -r if mode == 0 else r
            return sign(a.artist.casefold(), b.artist.casefold()) or sign(ta, tb)
        if mode == 3:  # artist, then year (discography order), then title
            return (sign(a.artist.casefold(), b.artist.casefold())
                    or sign(a.year, b.year) or sign(ta, tb))
        return sign(ta, tb)
    return cmp


def make_cover(size, circular=False, accent=False):
    """A square (or round) cover widget. Returns (widget, picture).

    The Gtk.Picture is an overlay child so its intrinsic image size never
    affects layout; the placeholder icon underneath sets the fixed size.
    """
    if accent:  # the player: green square with the white logo
        base = Gtk.Image(gicon=placeholder_icon(white=True), pixel_size=max(16, int(size * 0.55)))
    elif circular:  # artists without a picture keep a generic person icon
        base = Gtk.Image(icon_name="avatar-default-symbolic", pixel_size=size // 3)
        base.add_css_class("dim-label")
    else:  # albums without artwork show the Beet Music logo
        base = Gtk.Image(gicon=placeholder_icon(), pixel_size=max(16, int(size * 0.5)))
    base.set_size_request(size, size)
    pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER)
    overlay = Gtk.Overlay(child=base, halign=Gtk.Align.START, valign=Gtk.Align.START)
    overlay.add_overlay(pic)
    overlay.set_overflow(Gtk.Overflow.HIDDEN)
    overlay.add_css_class("tile-accent" if accent else "tile-avatar" if circular else "tile-cover")
    return overlay, pic


# The play / pause / next / previous artwork, exactly as designed. It is drawn straight from these SVGs
# (never looked up in an icon theme), so it looks identical whatever icon pack is installed.
TRANSPORT_SVGS = {
    ("next", "light"): """<svg width="27" height="25" viewBox="0 0 27 25" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M1.83902e-06 3.46415L3.13534e-07 20.9136C1.76613e-07 22.4798 1.7187 23.4383 3.05114 22.6151L17.6517 13.5953C18.9391 12.8 18.9111 10.9184 17.6006 10.1618L3 1.73209C1.66667 0.962294 1.97361e-06 1.92455 1.83902e-06 3.46415Z" fill="black"/>
<rect x="22.5" y="4.5836e-05" width="4" height="24" rx="2" fill="black"/>
</svg>""",
    ("next", "dark"): """<svg width="27" height="25" viewBox="0 0 27 25" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M1.83902e-06 3.46415L3.13534e-07 20.9136C1.76613e-07 22.4798 1.7187 23.4383 3.05114 22.6151L17.6517 13.5953C18.9391 12.8 18.9111 10.9184 17.6006 10.1618L3 1.73209C1.66667 0.962294 1.97361e-06 1.92455 1.83902e-06 3.46415Z" fill="white"/>
<rect x="22.5" y="4.5836e-05" width="4" height="24" rx="2" fill="white"/>
</svg>""",
    ("previous", "light"): """<svg width="27" height="25" viewBox="0 0 27 25" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M26.5 21.0359V3.58641C26.5 2.02022 24.7813 1.06176 23.4489 1.88491L8.84827 10.9047C7.56088 11.7001 7.5889 13.5817 8.8994 14.3383L23.5 22.768C24.8333 23.5378 26.5 22.5755 26.5 21.0359Z" fill="black"/>
<rect y="0.5" width="4" height="24" rx="2" fill="black"/>
</svg>""",
    ("previous", "dark"): """<svg width="27" height="25" viewBox="0 0 27 25" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M26.5 21.0359V3.58641C26.5 2.02022 24.7813 1.06176 23.4489 1.88491L8.84827 10.9047C7.56088 11.7001 7.5889 13.5817 8.8994 14.3383L23.5 22.768C24.8333 23.5378 26.5 22.5755 26.5 21.0359Z" fill="white"/>
<rect y="0.5" width="4" height="24" rx="2" fill="white"/>
</svg>""",
    ("play", "light"): """<svg width="70" height="70" viewBox="0 0 70 70" fill="none" xmlns="http://www.w3.org/2000/svg">
<circle cx="35" cy="35" r="35" fill="black"/>
<path d="M27.1782 24.8776L27.1782 43.8976C27.1782 45.6048 29.0516 46.6494 30.5039 45.7522L46.4185 35.9207C47.8218 35.0537 47.7913 33.0027 46.3628 32.1780L30.4482 22.9897C28.9949 22.1506 27.1782 23.1995 27.1782 24.8776Z" fill="white"/>
</svg>""",
    ("play", "dark"): """<svg width="70" height="70" viewBox="0 0 70 70" fill="none" xmlns="http://www.w3.org/2000/svg">
<circle cx="35" cy="35" r="35" fill="white"/>
<path d="M27.1782 24.8776L27.1782 43.8976C27.1782 45.6048 29.0516 46.6494 30.5039 45.7522L46.4185 35.9207C47.8218 35.0537 47.7913 33.0027 46.3628 32.1780L30.4482 22.9897C28.9949 22.1506 27.1782 23.1995 27.1782 24.8776Z" fill="black"/>
</svg>""",
    ("pause", "light"): """<svg width="70" height="70" viewBox="0 0 70 70" fill="none" xmlns="http://www.w3.org/2000/svg">
<circle cx="35" cy="35" r="35" fill="black"/>
<rect x="23" y="21" width="10" height="28" rx="2" fill="white"/>
<rect x="37" y="21" width="10" height="28" rx="2" fill="white"/>
</svg>""",
    ("pause", "dark"): """<svg width="70" height="70" viewBox="0 0 70 70" fill="none" xmlns="http://www.w3.org/2000/svg">
<circle cx="35" cy="35" r="35" fill="white"/>
<rect x="23" y="21" width="10" height="28" rx="2" fill="black"/>
<rect x="37" y="21" width="10" height="28" rx="2" fill="black"/>
</svg>""",
    # Triangle from the Play artwork on its own, for the labelled "Play" buttons: it takes the colour of the
    # button's text (currentColor), so it always matches the label whatever the theme or accent colour.
    ("playglyph", "light"): """<svg width="22" height="26" viewBox="27 22.5 22 26" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M28.1477 26.1779L28.1477 45.1978C28.1477 46.9050 30.0211 47.9496 31.4734 47.0524L47.3881 37.2209C48.7914 36.3539 48.7608 34.3029 47.3324 33.4782L31.4177 24.2899C29.9644 23.4508 28.1477 24.4997 28.1477 26.1779Z" fill="currentColor"/>
</svg>""",
}
TRANSPORT_SVGS[("playglyph", "dark")] = TRANSPORT_SVGS[("playglyph", "light")]


_TRANSPORT_SHAPES = {}


def _svg_size(kind):
    m = re.search(r'width="([\d.]+)" height="([\d.]+)"', TRANSPORT_SVGS[(kind, "light")])
    return float(m.group(1)), float(m.group(2))


def _rounded_rect_path(x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    return (f"M {x + r} {y} H {x + w - r} A {r} {r} 0 0 1 {x + w} {y + r} V {y + h - r} "
            f"A {r} {r} 0 0 1 {x + w - r} {y + h} H {x + r} A {r} {r} 0 0 1 {x} {y + h - r} "
            f"V {y + r} A {r} {r} 0 0 1 {x + r} {y} Z")


def transport_shapes(kind, dark):
    """The supplied SVG as vector shapes [(Gsk.Path, colour)], so GTK draws it sharp at any size or scale.
    Handles the elements the artwork uses: path, rect (with rx) and circle."""
    key = (kind, bool(dark))
    if key not in _TRANSPORT_SHAPES:
        shapes = []
        try:
            root = ET.fromstring(TRANSPORT_SVGS[(kind, "dark" if dark else "light")])
            vb = [float(v) for v in root.get("viewBox").split()]
            for el in root:
                tag = el.tag.rsplit("}", 1)[-1]
                f = lambda name, d=0.0: float(el.get(name, d))
                if tag == "path":
                    d = el.get("d")
                elif tag == "rect":
                    d = _rounded_rect_path(f("x"), f("y"), f("width"), f("height"), f("rx"))
                elif tag == "circle":
                    cx, cy, r = f("cx"), f("cy"), f("r")
                    d = f"M {cx - r} {cy} A {r} {r} 0 1 0 {cx + r} {cy} A {r} {r} 0 1 0 {cx - r} {cy} Z"
                else:
                    continue
                fill = el.get("fill", "black")
                if fill == "currentColor":
                    color = None  # the widget's text colour, resolved when drawing
                else:
                    color = Gdk.RGBA()
                    color.parse(fill)
                shapes.append((Gsk.Path.parse(d), color))
            _TRANSPORT_SHAPES[key] = (vb, shapes)
        except Exception:
            _TRANSPORT_SHAPES[key] = None
    return _TRANSPORT_SHAPES[key]


class TransportIcon(Gtk.Widget):
    """Draws one of the supplied transport artworks, as true vector shapes, inside a width x height box
    (logical px), keeping its proportions. It follows light/dark mode on its own and, being vector,
    is sharp at every size and display scale."""
    __gtype_name__ = "BeetTransportIcon"
    _live = weakref.WeakSet()
    _hooked = False

    def __init__(self, kind, width, height):
        super().__init__(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.set_can_target(False)
        self.kind, self._w, self._h = kind, width, height
        TransportIcon._live.add(self)
        if not TransportIcon._hooked:
            TransportIcon._hooked = True
            Adw.StyleManager.get_default().connect(
                "notify::dark", lambda *_: [w.queue_draw() for w in list(TransportIcon._live)])

    def set_kind(self, kind):
        if kind != self.kind:
            self.kind = kind
            self.queue_draw()

    def do_measure(self, orientation, _for_size):
        n = self._w if orientation == Gtk.Orientation.HORIZONTAL else self._h
        return n, n, -1, -1

    def do_snapshot(self, snapshot):
        art = transport_shapes(self.kind, Adw.StyleManager.get_default().get_dark())
        if art is None:
            return
        (vx, vy, vw, vh), shapes = art
        k = min(self._w / vw, self._h / vh)
        snapshot.save()
        snapshot.translate(Graphene.Point().init((self.get_width() - vw * k) / 2 - vx * k,
                                                 (self.get_height() - vh * k) / 2 - vy * k))
        snapshot.scale(k, k)
        for path, color in shapes:
            snapshot.append_fill(path, Gsk.FillRule.WINDING, color or self.get_color())
        snapshot.restore()


_SRGB_TO_LINEAR = [((c / 255) / 12.92 if c / 255 <= 0.04045 else (((c / 255) + 0.055) / 1.055) ** 2.4)
                   for c in range(256)]


def art_luminance(texture, size, region):
    """Mean relative luminance (0 black .. 1 white) of the part of a cover-fitted square widget of `size` px
    that lies under `region` = (x0, y0, x1, y1), or None if it can't be read."""
    w, h = texture.get_width(), texture.get_height()
    if w <= 0 or h <= 0:
        return None
    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
    data, stride = downloader.download_bytes()
    raw = data.get_data()
    scale = max(size / w, size / h)  # Gtk.ContentFit.COVER
    off_x, off_y = (w * scale - size) / 2, (h * scale - size) / 2
    x0, y0, x1, y1 = region
    tx0, tx1 = max(0, int((x0 + off_x) / scale)), min(w, int((x1 + off_x) / scale) + 1)
    ty0, ty1 = max(0, int((y0 + off_y) / scale)), min(h, int((y1 + off_y) / scale) + 1)
    total, n = 0.0, 0
    for y in range(ty0, ty1):
        row = y * stride
        for x in range(tx0, tx1):
            i = row + x * 4
            if raw[i + 3] < 8:
                continue
            total += (0.2126 * _SRGB_TO_LINEAR[raw[i]] + 0.7152 * _SRGB_TO_LINEAR[raw[i + 1]]
                      + 0.0722 * _SRGB_TO_LINEAR[raw[i + 2]])
            n += 1
    return total / n if n else None


def adapt_icon_to_art(button, art_pic, size, region):
    """Make an icon button that sits on artwork white on dark art and black on light art."""
    tex = art_pic.get_paintable()
    lum = None
    if isinstance(tex, Gdk.Texture):
        try:
            lum = art_luminance(tex, size, region)
        except Exception:
            lum = None
    button.remove_css_class("art-dark")
    button.remove_css_class("art-light")
    if lum is not None:  # 0.179 is where black and white text have equal contrast
        button.add_css_class("art-dark" if lum < 0.179 else "art-light")


def _tile_hover(lead, widgets, on):
    """Show or hide an album tile's overlay controls (hidden controls must not catch clicks)."""
    if on:
        lead.add_css_class("show-play")
    else:
        lead.remove_css_class("show-play")
    for w in widgets:
        w.set_can_target(on)


def _album_heart_set(btn, on):
    """Album heart: filled green when every song of the album is a favorite, outline otherwise."""
    btn.set_icon_name("beet-favorites-symbolic" if on else "beet-heart-outline-symbolic")
    if on:
        btn.add_css_class("fav-on")
    else:
        btn.remove_css_class("fav-on")
    btn.set_tooltip_text("Remove album from Favorites" if on else "Add album to Favorites")


def _tile_icon(btn, pausing):
    """Pause icon while this album is playing, play icon otherwise."""
    btn.get_child().set_kind("pause" if pausing else "play")
    btn.set_tooltip_text("Pause album" if pausing else "Play album")


class TileGlass(Gtk.Widget):
    """The blurred-glass layer behind an album's play button: a round, blurred copy of the cover art."""
    __gtype_name__ = "BeetTileGlass"

    def __init__(self, source):
        super().__init__()
        self.source = source  # the cover widget (art, placeholder and hover tint) that gets blurred
        self.paintable = Gtk.WidgetPaintable.new(source)
        self._paint_id = self.paintable.connect("invalidate-contents", self._on_invalidate)
        self.set_can_target(False)
        self.set_can_focus(False)

    def _on_invalidate(self, *_):
        try:
            self.queue_draw()
        except RuntimeError:  # the tile is already being torn down
            pass

    def do_dispose(self):
        if self._paint_id:  # stop listening so a dying tile is never poked again
            self.paintable.disconnect(self._paint_id)
            self._paint_id = 0
        Gtk.Widget.do_dispose(self)

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        sw, sh = self.source.get_width(), self.source.get_height()
        if w <= 0 or h <= 0 or sw <= 0 or sh <= 0:
            return
        ok, origin = self.compute_point(self.source, Graphene.Point().init(0, 0))
        if not ok:
            return
        round_rect = Gsk.RoundedRect()
        round_rect.init_from_rect(Graphene.Rect().init(0, 0, w, h), min(w, h) / 2)
        snapshot.push_rounded_clip(round_rect)
        snapshot.push_blur(GLASS_BLUR)
        snapshot.save()
        snapshot.translate(Graphene.Point().init(-origin.x, -origin.y))
        self.paintable.snapshot(snapshot, sw, sh)
        snapshot.restore()
        snapshot.pop()  # blur
        snapshot.pop()  # round clip


class GlassBackdrop(Gtk.Widget):
    """Draws a live, blurred copy of the content underneath it (a real "glass" backdrop)."""
    __gtype_name__ = "BeetGlassBackdrop"

    def __init__(self, source, blur=None):
        super().__init__()
        self.source = source
        self.blur = GLASS_BLUR if blur is None else blur
        self.paintable = Gtk.WidgetPaintable.new(source)
        self.paintable.connect("invalidate-contents", lambda *_: self.queue_draw())
        self.paintable.connect("invalidate-size", lambda *_: self.queue_draw())
        self.set_can_target(False)
        self.set_can_focus(False)
        self.set_hexpand(True)
        self.set_vexpand(True)

    def do_snapshot(self, snapshot):
        # Snapshot the underlying navigation view in its own coordinate space,
        # then translate it so the player bar shows the exact region behind it.
        w, h = self.get_width(), self.get_height()
        sw, sh = self.source.get_width(), self.source.get_height()
        if w <= 0 or h <= 0 or sw <= 0 or sh <= 0:
            return

        ok, origin = self.compute_point(self.source, Graphene.Point().init(0, 0))
        if not ok:
            return

        snapshot.push_clip(Graphene.Rect().init(0, 0, w, h))
        snapshot.push_blur(self.blur)
        snapshot.save()
        snapshot.translate(Graphene.Point().init(-origin.x, -origin.y))
        self.paintable.snapshot(snapshot, sw, sh)
        snapshot.restore()
        snapshot.pop()  # finish the blurred backdrop
        snapshot.pop()  # clip to the player bar bounds


# ----------------------------------------------------------------- window
class PlayerWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME,
                         default_width=1180, default_height=780)
        self.queue, self.index = [], 0
        self.is_playing = False
        self._user_seek = 0
        self._binds = {}
        self._now_album = None
        self._now_bind = None
        self.misses = load_json(MISS_FILE, {})
        self._fetching = False
        self._mpris_conn = None
        self._serial = 0
        self._syncing = False
        self._rerun = False
        self._last_check = 0.0
        self.config = load_json(CONFIG, {})
        self.playlists = load_json(PL_FILE, [])
        favs = load_json(FAV_FILE, [])
        self.favorites = [p for p in favs if isinstance(p, str)] if isinstance(favs, list) else []
        self.fav_set = set(self.favorites)
        self.mini = None
        self._row_hooks = {}
        self._play_hooks = {}
        detect_cover_scale()
        GLib.timeout_add_seconds(60, trim_memory)  # give freed memory back to the OS now and then
        self.connect("close-request", self._on_close_request)
        self.artists, self.tracks_by_path = {}, {}
        self.search_text = ""
        # Albums scroll position, as a 0..1 fraction of the scrollable range (survives library reloads and restarts).
        try:
            self._album_ratio = min(1.0, max(0.0, float(self.config.get("album_scroll", 0.0))))
        except (TypeError, ValueError):
            self._album_ratio = 0.0
        self._scroll_lock = True        # True while the position is being restored/not trustworthy
        self._scroll_save_id = 0
        self._lib_sig = None
        mode = self.config.get("sort", 0)
        self.sort_mode = mode if mode in range(len(SORT_MODES)) else 0

        self._init_player()
        self._build_ui()
        self._add_actions()
        # Apply solid surfaces after loading the saved appearance setting.
        style_manager = Adw.StyleManager.get_default()
        style_manager.connect("notify::dark", self._update_surface_theme)
        self._update_surface_theme(style_manager)

        self.folder = self.config.get("folder") or GLib.get_user_special_dir(
            GLib.UserDirectory.DIRECTORY_MUSIC) or os.path.expanduser("~/Music")
        if self.folder and os.path.isdir(self.folder):
            self.refresh_library()
        else:
            self.empty_page.set_title("No Music Folder")
        self.refresh_playlists()
        GLib.timeout_add(500, self._tick)
        self.connect("notify::is-active", self._on_active)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self._init_mpris()

    # ---- playback engine
    def _init_player(self):
        self.player = Gst.ElementFactory.make("playbin", "player")
        self.player.set_property("volume", 0.8)
        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._on_eos)
        bus.connect("message::error", self._on_error)

    def _on_eos(self, *_):
        if self.repeat_btn.get_active() and self.queue:
            self.load()  # repeat song: play the same track again
        else:
            self.next()

    def _on_repeat_toggled(self, btn):
        self.config["repeat"] = btn.get_active()
        save_json(CONFIG, self.config)
        self._mpris_notify(["LoopStatus"])

    def _on_volume(self, scale):
        self.player.set_property("volume", scale.get_value())
        self._mpris_notify(["Volume"])

    def _on_error(self, _bus, msg):
        err, _dbg = msg.parse_error()
        self.player.set_state(Gst.State.NULL)
        self._set_playing(False)
        self.now_title.set_text("Playback error")
        self.now_artist.set_text(err.message)

    def play_tracks(self, tracks, idx):
        if not tracks:
            return
        self.queue, self.index = list(tracks), idx
        self.load()
        self._set_controls(True)

    def load(self):
        self._serial += 1
        t = self.queue[self.index]
        self.player.set_state(Gst.State.NULL)
        self.player.set_property("uri", Gio.File.new_for_path(t.path).get_uri())
        self.player.set_state(Gst.State.PLAYING)
        self._set_playing(True)
        self.now_title.set_text(t.title)
        self.now_title.set_tooltip_text(t.title)
        self.now_artist.set_text(t.artist)
        self.seek.set_range(0, max(t.length, 1))
        self.seek.set_value(0)
        self.total_lbl.set_text(fmt_time(t.length))
        if self._now_bind:
            self._now_bind.unbind()
            self._now_bind = None
            self.now_pic.set_paintable(None)
            self._now_album.release_cover()
        self._now_album = t.album
        if t.album:
            t.album.acquire_cover()
            self._now_bind = t.album.bind_property(
                "cover", self.now_pic, "paintable", GObject.BindingFlags.SYNC_CREATE)
        self._refresh_fav_buttons()
        self._mpris_notify(["Metadata", "PlaybackStatus", "CanGoNext", "CanGoPrevious", "CanSeek"])

    def toggle(self, *_):
        if not self.queue:
            return
        self.player.set_state(Gst.State.PAUSED if self.is_playing else Gst.State.PLAYING)
        self._set_playing(not self.is_playing)

    def next(self, *_):
        if not self.queue:
            return
        if self.index + 1 < len(self.queue):
            self.index += 1
            self.load()
        else:  # end of queue
            self.player.set_state(Gst.State.NULL)
            self._set_playing(False)
            self.seek.set_value(0)
            self.pos_lbl.set_text("0:00")

    def prev(self, *_):
        if not self.queue:
            return
        ok, pos = self.player.query_position(Gst.Format.TIME)
        if (ok and pos > 3 * Gst.SECOND) or self.index == 0:
            self.player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
        else:
            self.index -= 1
            self.load()

    # ---- keyboard shortcuts
    def _on_key(self, _ctrl, keyval, _keycode, state, win=None):
        # Dedicated media keys (also sent by many headsets) always work
        if keyval == Gdk.KEY_AudioPlay:
            if not self.is_playing:
                self.toggle()
            return True
        if keyval == Gdk.KEY_AudioPause:
            if self.is_playing:
                self.toggle()
            return True
        if keyval == Gdk.KEY_AudioNext:
            self.next()
            return True
        if keyval == Gdk.KEY_AudioPrev:
            self.prev()
            return True
        focus = (win or self).get_focus()
        if isinstance(focus, Gtk.Editable):  # typing in a text box: leave keys alone
            return False
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        other = bool(state & (Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK))
        if other:
            return False
        if keyval == Gdk.KEY_space and not ctrl:
            if isinstance(focus, (Gtk.Button, Gtk.ToggleButton)):
                return False  # Space should still press a focused button
            self.toggle()
            return True
        if ctrl and keyval == Gdk.KEY_Right:
            self.next()
            return True
        if ctrl and keyval == Gdk.KEY_Left:
            self.prev()
            return True
        return False

    # ---- media keys / headphones via MPRIS (GNOME, KDE and others route keys here)
    def _init_windows_media_keys(self):
        actions = {"toggle": self.toggle, "next": self.next, "prev": self.prev}
        self._win_keys = WindowsMediaKeys(lambda name: (actions[name](), False)[1])

    def _init_mpris(self):
        if IS_WINDOWS:  # no D-Bus session bus on Windows
            self._init_windows_media_keys()
            return
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            for xml in (MPRIS_ROOT_XML, MPRIS_PLAYER_XML):
                info = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
                conn.register_object(MPRIS_PATH, info, self._mpris_call,
                                     self._mpris_get, self._mpris_set)
            Gio.bus_own_name_on_connection(conn, MPRIS_NAME, Gio.BusNameOwnerFlags.NONE,
                                           None, None)
            self._mpris_conn = conn
        except Exception as e:  # the player works fine without media-key support
            print("Media key support unavailable:", e, file=sys.stderr)

    def _mpris_call(self, _conn, _sender, _path, iface, method, params, invocation):
        try:
            if iface == MPRIS_ROOT_IFACE:
                if method == "Raise":
                    self.raise_window()
                elif method == "Quit":
                    self.get_application().quit()
            elif method == "PlayPause":
                self.toggle()
            elif method == "Play" and not self.is_playing:
                self.toggle()
            elif method == "Pause" and self.is_playing:
                self.toggle()
            elif method == "Stop" and self.queue:
                self.player.set_state(Gst.State.PAUSED)
                self.player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
                self._set_playing(False)
            elif method == "Next":
                self.next()
            elif method == "Previous":
                self.prev()
            elif method == "Seek":
                ok, pos = self.player.query_position(Gst.Format.TIME)
                if ok:
                    self._seek_to(pos // 1000 + params.unpack()[0])
            elif method == "SetPosition":
                self._seek_to(params.unpack()[1])
        except Exception as e:
            print("Media key error:", e, file=sys.stderr)
        invocation.return_value(None)

    def _seek_to(self, microseconds):
        if self.queue:
            self.player.seek_simple(Gst.Format.TIME,
                                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                                    max(0, int(microseconds)) * 1000)

    def _mpris_get(self, _conn, _sender, _path, iface, prop):
        V = GLib.Variant
        if iface == MPRIS_ROOT_IFACE:
            return {"CanQuit": V("b", True), "CanRaise": V("b", True),
                    "HasTrackList": V("b", False), "Identity": V("s", APP_NAME),
                    "DesktopEntry": V("s", APP_ID), "SupportedUriSchemes": V("as", ["file"]),
                    "SupportedMimeTypes": V("as", ["audio/mpeg", "audio/flac", "audio/ogg",
                                                   "audio/mp4", "audio/x-wav"])}.get(prop)
        return self._mpris_player_props().get(prop)

    def _mpris_set(self, _conn, _sender, _path, iface, prop, value):
        if iface == MPRIS_PLAYER_IFACE:
            if prop == "Volume":
                self.vol_scale.set_value(max(0.0, min(1.0, value.get_double())))
                return True
            if prop == "LoopStatus":
                self.repeat_btn.set_active(value.get_string() == "Track")
                return True
        return False

    def _mpris_player_props(self):
        V = GLib.Variant
        t = self.queue[self.index] if self.queue else None
        if t:
            meta = {"mpris:trackid": V("o", f"/io/github/studioone/BeetMusic/track/{self._serial}"),
                    "mpris:length": V("x", int(t.length * 1_000_000)),
                    "xesam:title": V("s", t.title), "xesam:artist": V("as", [t.artist]),
                    "xesam:album": V("s", t.album.title if t.album else "")}
            if t.album and t.album.cover_file and os.path.exists(t.album.cover_file):
                meta["mpris:artUrl"] = V("s", Gio.File.new_for_path(t.album.cover_file).get_uri())
        else:
            meta = {"mpris:trackid": V("o", "/org/mpris/MediaPlayer2/TrackList/NoTrack")}
        ok, pos = self.player.query_position(Gst.Format.TIME)
        status = "Playing" if self.is_playing else ("Paused" if self.queue else "Stopped")
        return {
            "PlaybackStatus": V("s", status),
            "LoopStatus": V("s", "Track" if self.repeat_btn.get_active() else "None"),
            "Rate": V("d", 1.0), "Shuffle": V("b", False),
            "Metadata": V("a{sv}", meta),
            "Volume": V("d", float(self.player.get_property("volume"))),
            "Position": V("x", int(pos // 1000) if ok else 0),
            "MinimumRate": V("d", 1.0), "MaximumRate": V("d", 1.0),
            "CanGoNext": V("b", bool(self.queue) and self.index + 1 < len(self.queue)),
            "CanGoPrevious": V("b", bool(self.queue)),
            "CanPlay": V("b", True), "CanPause": V("b", True),
            "CanSeek": V("b", bool(self.queue)), "CanControl": V("b", True),
        }

    def _mpris_notify(self, names):
        if self._mpris_conn is None:
            return
        props = self._mpris_player_props()
        changed = {n: props[n] for n in names}
        try:
            self._mpris_conn.emit_signal(
                None, MPRIS_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (MPRIS_PLAYER_IFACE, changed, [])))
        except GLib.Error:
            pass

    def _show_playing_album(self, *_):
        """Open the album page (track list) of the song that is playing."""
        if self.queue and self.queue[self.index].album:
            self.open_album(self.queue[self.index].album)

    def _set_controls(self, on):
        for w in self._transport:
            w.set_sensitive(on)

    def _album_is_current(self, album):
        t = self.queue[self.index] if self.queue else None
        return bool(t and t.album is not None and t.album.key == album.key)

    def _album_is_playing(self, album):
        return bool(self.is_playing and self._album_is_current(album))

    def _tile_play_clicked(self, album):
        """Play button on an album tile: pause/resume if it is this album, otherwise start it."""
        if self._album_is_current(album):
            self.toggle()
        else:
            self.play_tracks(album.tracks, 0)

    def _sync_tile_buttons(self):
        for btn, _hid, album, *_rest in list(getattr(self, "_play_hooks", {}).values()):
            _tile_icon(btn, self._album_is_playing(album))

    def _album_all_favorite(self, album):
        return bool(album.tracks) and all(t.path in self.fav_set for t in album.tracks)

    def _sync_tile_hearts(self):
        for _btn, _hid, album, heart, _hhid in list(getattr(self, "_play_hooks", {}).values()):
            _album_heart_set(heart, self._album_all_favorite(album))

    def toggle_album_favorite(self, album):
        """Heart on an album tile: favorite every song on it, or un-favorite them if all already are."""
        tracks = list(album.tracks)
        if not tracks:
            return
        if self._album_all_favorite(album):
            for t in tracks:
                if t.path in self.fav_set:
                    self.favorites.remove(t.path)
                    self.fav_set.discard(t.path)
                t.favorite = False
            msg = f"Removed “{album.title}” from Favorites"
        else:
            for t in reversed(tracks):  # keep the album's own order at the top of Favorites
                if t.path not in self.fav_set:
                    self.favorites.insert(0, t.path)
                    self.fav_set.add(t.path)
                t.favorite = True
            msg = f"Added “{album.title}” to Favorites"
        save_json(FAV_FILE, self.favorites)
        self._rebuild_favorites()
        self._refresh_fav_buttons()
        self._sync_tile_hearts()
        self.toasts.add_toast(Adw.Toast.new(msg))

    def _set_playing(self, playing):
        self.is_playing = playing
        self.play_icon.set_kind("pause" if playing else "play")
        if self.mini:
            self.mini_play_icon.set_kind("pause" if playing else "play")
        self._mpris_notify(["PlaybackStatus", "CanGoNext", "CanGoPrevious"])
        self._sync_tile_buttons()

    def _on_seek(self, _scale, _scroll, value):
        self._user_seek = GLib.get_monotonic_time()
        if self.queue:
            self.player.seek_simple(Gst.Format.TIME,
                                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                                    int(value * Gst.SECOND))
        return False  # let GTK move the slider

    def _tick(self):
        if self.queue and self.is_playing:
            ok_d, dur = self.player.query_duration(Gst.Format.TIME)
            ok_p, pos = self.player.query_position(Gst.Format.TIME)
            if ok_d and dur > 0:
                secs = dur / Gst.SECOND
                if abs(self.seek.get_adjustment().get_upper() - secs) > 0.5:
                    self.seek.set_range(0, secs)
                    self.total_lbl.set_text(fmt_time(secs))
            if ok_p:
                self.pos_lbl.set_text(fmt_time(pos / Gst.SECOND))
                if GLib.get_monotonic_time() - self._user_seek > 1_000_000:
                    self.seek.set_value(pos / Gst.SECOND)
        return True

    # ---- main UI
    def _build_ui(self):
        # --- Albums tab
        self.album_store = Gio.ListStore(item_type=Album)
        self.album_filter = Gtk.CustomFilter.new(self._match_album, None)
        fm = Gtk.FilterListModel(model=self.album_store, filter=self.album_filter)
        self.album_sort = Gtk.SortListModel(
            model=fm, sorter=Gtk.CustomSorter.new(album_cmp(self.sort_mode), None))
        album_grid = self._make_grid(
            self.album_sort,
            self._tile_factory(False, lambda a: a.title, lambda a: a.artist,
                               lambda a: f"{a.title} — {a.artist}" + (f" ({a.year})" if a.year else "")),
            self.open_album)
        self.album_scroller = album_grid
        album_grid.get_vadjustment().connect("value-changed", self._on_album_scrolled)

        open_btn = Gtk.Button(label="Open Music Folder", halign=Gtk.Align.CENTER)
        open_btn.add_css_class("suggested-action")
        open_btn.add_css_class("pill")
        open_btn.set_action_name("win.open-folder")
        self.empty_page = Adw.StatusPage(paintable=loading_logo_paintable(), title="Loading…",
                                         description="Choose the folder that holds your music.",
                                         child=open_btn)
        self.albums_stack = Gtk.Stack()
        self.albums_stack.add_named(album_grid, "grid")
        self.albums_stack.add_named(self.empty_page, "empty")
        self.albums_stack.set_visible_child_name("empty")

        # --- Artists tab
        self.artist_store = Gio.ListStore(item_type=Artist)
        self.artist_filter = Gtk.CustomFilter.new(self._match_artist, None)
        afm = Gtk.FilterListModel(model=self.artist_store, filter=self.artist_filter)
        asm = Gtk.SortListModel(model=afm, sorter=Gtk.CustomSorter.new(
            lambda a, b, _d: sign(a.name.casefold(), b.name.casefold()), None))
        artist_grid = self._make_grid(
            asm,
            self._tile_factory(True, lambda a: a.name,
                               lambda a: f"{len(a.albums)} album" + ("s" if len(a.albums) != 1 else ""),
                               lambda a: a.name),
            self.open_artist)

        # --- Songs tab (one flat, searchable list of every song)
        self.song_store = Gio.ListStore(item_type=Track)
        self.song_filter = Gtk.CustomFilter.new(self._match_song, None)
        sfm = Gtk.FilterListModel(model=self.song_store, filter=self.song_filter)
        self.song_sel = Gtk.SingleSelection(model=sfm, autoselect=False, can_unselect=True)
        songs_page = Gtk.ScrolledWindow(child=self._make_song_view(self.song_sel), vexpand=True,
                                        hscrollbar_policy=Gtk.PolicyType.NEVER)

        # --- Favorites tab (same rows as Songs, only the hearted songs)
        self.fav_store = Gio.ListStore(item_type=Track)
        self.fav_filter = Gtk.CustomFilter.new(self._match_song, None)
        ffm = Gtk.FilterListModel(model=self.fav_store, filter=self.fav_filter)
        self.fav_sel = Gtk.SingleSelection(model=ffm, autoselect=False, can_unselect=True)
        fav_page = Gtk.ScrolledWindow(child=self._make_song_view(self.fav_sel), vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.fav_empty = Adw.StatusPage(icon_name="beet-favorites-symbolic", title="No Favorites",
                                        description="Tap the heart on any song to save it here.")
        self.fav_stack = Gtk.Stack()
        self.fav_stack.add_named(fav_page, "list")
        self.fav_stack.add_named(self.fav_empty, "empty")
        self.fav_stack.set_visible_child_name("empty")

        # --- Playlists tab
        new_btn = Gtk.Button(child=Adw.ButtonContent(icon_name="list-add-symbolic",
                                                     label="New Playlist"),
                             halign=Gtk.Align.END)
        new_btn.connect("clicked", lambda _b: self.prompt_new_playlist())
        self.pl_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.pl_list.add_css_class("boxed-list")
        pl_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         margin_top=24, margin_bottom=24 + BAR_SPACE, margin_start=24, margin_end=24)
        pl_box.append(new_btn)
        pl_box.append(self.pl_list)
        pl_empty_btn = Gtk.Button(label="New Playlist", halign=Gtk.Align.CENTER)
        pl_empty_btn.add_css_class("suggested-action")
        pl_empty_btn.add_css_class("pill")
        pl_empty_btn.connect("clicked", lambda _b: self.prompt_new_playlist())
        self.pl_empty = Adw.StatusPage(
            icon_name="beet-playlists-symbolic", title="No Playlists",
            description="Create a playlist, then add songs from any album.", child=pl_empty_btn)
        self.pl_stack = Gtk.Stack()
        self.pl_stack.add_named(Gtk.ScrolledWindow(
            child=Adw.Clamp(child=pl_box, maximum_size=700),
            hscrollbar_policy=Gtk.PolicyType.NEVER), "list")
        self.pl_stack.add_named(self.pl_empty, "empty")

        # --- view stack + header
        self.stack = Adw.ViewStack(vexpand=True)
        self.stack.add_titled_with_icon(self.albums_stack, "albums", "Albums", "beet-albums-symbolic")
        self.stack.add_titled_with_icon(artist_grid, "artists", "Artists", "beet-artists-symbolic")
        self.stack.add_titled_with_icon(songs_page, "songs", "Songs", "beet-songs-symbolic")
        self.stack.add_titled_with_icon(self.pl_stack, "playlists", "Playlists", "beet-playlists-symbolic")
        self.stack.add_titled_with_icon(self.fav_stack, "favorites", "Favorites", "beet-favorites-symbolic")

        # Explicit navigation buttons instead of Adw.ViewSwitcher internals.
        # libadwaita owns ViewSwitcher selection painting, which can override
        # application CSS; ordinary GTK buttons make the requested colors reliable.
        switcher = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        switcher.add_css_class("beet-navigation")
        self._nav_buttons = {}
        self._nav_labels = []
        for page, title, icon_name in (
            ("albums", "Albums", "beet-albums-symbolic"),
            ("artists", "Artists", "beet-artists-symbolic"),
            ("songs", "Songs", "beet-songs-symbolic"),
            ("playlists", "Playlists", "beet-playlists-symbolic"),
            ("favorites", "Favorites", "beet-favorites-symbolic"),
        ):
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            content.set_halign(Gtk.Align.CENTER)
            content.append(Gtk.Image.new_from_icon_name(icon_name))
            nav_label = Gtk.Label(label=title)
            self._nav_labels.append(nav_label)
            content.append(nav_label)
            button = Gtk.Button(child=content)
            button.add_css_class("beet-nav-button")
            button.set_tooltip_text(title)
            button.connect("clicked", lambda _button, name=page:
                           self.stack.set_visible_child_name(name))
            switcher.append(button)
            self._nav_buttons[page] = button
        self.search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic",
                                           tooltip_text="Search (Ctrl+F)")
        self.spinner = Gtk.Spinner()
        # Use GTK4's native menu button and menu model.  Do not style its
        # popover: the system theme determines its compact size and spacing.
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic",
                                  primary=True, tooltip_text="Main menu")
        menu_btn.set_menu_model(self._build_menu())
        # Keep the native GTK menu layout; only clear its rectangular outer surface.
        menu_btn.get_popover().add_css_class("beet-native-main-menu")
        self._nav_switcher = switcher
        self.stack.connect("notify::visible-child", self._sync_active_nav_button)
        self.stack.connect("notify::visible-child-name", self._sync_active_nav_button)
        switcher.connect("map", self._sync_active_nav_button)
        self._sync_active_nav_button()
        header = Adw.HeaderBar(title_widget=switcher)
        header.pack_start(self.search_btn)
        self.mini_btn = Gtk.Button(icon_name="beet-mini-player-symbolic",
                                  tooltip_text="Mini player (Ctrl+M)")
        self.mini_btn.set_action_name("win.mini-player")
        header.pack_end(menu_btn)
        header.pack_end(self.mini_btn)
        header.pack_end(self.spinner)

        self.search_entry = Gtk.SearchEntry(hexpand=True, placeholder_text="Search albums, artists, songs and playlists")
        self.search_bar = Gtk.SearchBar(child=Adw.Clamp(child=self.search_entry, maximum_size=520))
        self.search_bar.connect_entry(self.search_entry)
        self.search_btn.bind_property("active", self.search_bar, "search-mode-enabled",
                                      GObject.BindingFlags.BIDIRECTIONAL)
        self.search_bar.connect("notify::search-mode-enabled",
                                lambda b, _p: None if b.get_search_mode()
                                else self.search_entry.set_text(""))
        self.search_entry.connect("search-changed", self._on_search)

        main = Adw.ToolbarView(content=self.stack)
        main.add_top_bar(header)
        main.add_top_bar(self.search_bar)

        self.nav = Adw.NavigationView(vexpand=True)
        self.nav.add(Adw.NavigationPage(title=APP_NAME, child=main))

        # --- player bar: always visible, compact, transport controls centred
        self.pos_lbl = Gtk.Label(label="0:00", width_chars=4, xalign=1)
        self.total_lbl = Gtk.Label(label="0:00", width_chars=4, xalign=0)
        for lbl in (self.pos_lbl, self.total_lbl):
            lbl.add_css_class("dim-label")
            lbl.add_css_class("caption")
            lbl.add_css_class("numeric")
        self.seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.seek.set_draw_value(False)
        self.seek.set_valign(Gtk.Align.CENTER)
        self.seek.set_size_request(120, -1)
        self.seek.set_hexpand(True)
        self.seek.connect("change-value", self._on_seek)
        seek_row = Gtk.Box(spacing=8, hexpand=True)
        for w in (self.pos_lbl, self.seek, self.total_lbl):
            seek_row.append(w)

        prev_btn = Gtk.Button(child=TransportIcon("previous", 21, 19), valign=Gtk.Align.CENTER)
        self.play_icon = TransportIcon("play", 38, 38)
        self.play_btn = Gtk.Button(child=self.play_icon, valign=Gtk.Align.CENTER)
        next_btn = Gtk.Button(child=TransportIcon("next", 21, 19), valign=Gtk.Align.CENTER)
        for b in (prev_btn, next_btn):
            b.add_css_class("flat")
            b.add_css_class("circular")
            b.add_css_class("transport")
        self.play_btn.add_css_class("play-circle")
        self.play_btn.add_css_class("circular")
        for b, cb in ((prev_btn, self.prev), (self.play_btn, self.toggle), (next_btn, self.next)):
            b.connect("clicked", cb)
        transport = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        for b in (prev_btn, self.play_btn, next_btn):
            transport.append(b)
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        center.append(transport)
        center.append(seek_row)
        self._transport = (prev_btn, self.play_btn, next_btn, self.seek)

        now_ov, self.now_pic = make_cover(48, accent=True)
        self.now_btn = Gtk.Button(child=now_ov, valign=Gtk.Align.CENTER,
                                  tooltip_text="Show album and track list")
        self.now_btn.add_css_class("flat")
        self.now_btn.add_css_class("cover-button")
        self.now_btn.connect("clicked", self._show_playing_album)
        self._transport += (self.now_btn,)
        self.now_title = Gtk.Label(label="Nothing Playing", xalign=0,
                                   ellipsize=Pango.EllipsizeMode.END, max_width_chars=24)
        self.now_title.add_css_class("heading")
        self.now_artist = Gtk.Label(label="No Album", xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=28)
        self.now_artist.add_css_class("dim-label")
        self.now_artist.add_css_class("caption")
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        text.append(self.now_title)
        text.append(self.now_artist)
        left = Gtk.Box(spacing=10, halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        left.set_size_request(-1, -1)
        left.set_hexpand(True)
        left.append(self.now_btn)
        left.append(text)
        self.fav_btn = _make_heart_button()
        self.fav_btn.connect("clicked", lambda _b: self.queue and self.toggle_favorite(self.queue[self.index]))
        left.append(self.fav_btn)
        self._transport += (self.fav_btn,)

        vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.05)
        vol.set_value(0.8)
        vol.set_draw_value(False)
        vol.set_valign(Gtk.Align.CENTER)
        vol.set_size_request(95, -1)
        vol.connect("value-changed", self._on_volume)
        self.vol_scale = vol
        right = Gtk.Box(spacing=8, halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        right.set_size_request(-1, -1)
        right.set_hexpand(True)
        self.repeat_btn = Gtk.ToggleButton(icon_name="beet-repeat-song-symbolic",
                                           tooltip_text="Repeat song", hexpand=True,
                                           halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        self.repeat_btn.add_css_class("flat")
        self.repeat_btn.add_css_class("circular")
        self.repeat_btn.add_css_class("repeat")
        self.repeat_btn.set_active(bool(self.config.get("repeat", False)))
        self.repeat_btn.connect("toggled", self._on_repeat_toggled)
        right.append(self.repeat_btn)
        vol_icon = Gtk.Image(icon_name="audio-volume-high-symbolic", margin_start=6)
        right.append(vol_icon)
        right.append(vol)
        self._responsive_volume = (vol_icon, vol)

        # Match the album artwork's vertical clearance above and below.
        row = Gtk.CenterBox(margin_start=14, margin_end=14, margin_top=11, margin_bottom=11)
        row.set_start_widget(left)
        row.set_center_widget(center)
        row.set_end_widget(right)
        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        controls.add_css_class("glass-controls")
        # Light: 60% white + #DEDEDE border. Dark: 60% black + #323232 border.
        # Both: 7.5px blur, window-matching radius. (get_dark also follows the system theme.)
        style_manager = Adw.StyleManager.get_default()
        def update_glass_theme(manager, *_args):
            controls.remove_css_class("glass-dark")
            if manager.get_dark():
                controls.add_css_class("glass-dark")
        style_manager.connect("notify::dark", update_glass_theme)
        update_glass_theme(style_manager)
        controls.append(row)
        # Switch the same widgets between wide and stacked layouts as the window resizes.
        self._responsive_row = row
        self._responsive_controls = controls
        self._responsive_left = left
        self._responsive_center = center
        self._responsive_right = right
        self._responsive_seek_row = seek_row
        self._responsive_compact = False
        self._responsive_width = -1
        self._responsive_top = Gtk.Box(spacing=8)  # compact layout: track info + repeat
        self._responsive_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7,
                                         margin_start=12, margin_end=12,
                                         margin_top=8, margin_bottom=10)
        # The same translucent glass treatment is used on album and track-list pages.
        # Keep the navigation content visible through a moderately blurred backdrop.
        self.player_bar = Gtk.Overlay(
            child=GlassBackdrop(self.nav),
            valign=Gtk.Align.END,
            hexpand=True,
        )
        self.player_bar.add_overlay(controls)
        # Redraw the live blurred snapshot when switching appearance modes.
        style_manager.connect("notify::dark", lambda *_: self.player_bar.queue_draw())
        self.player_bar.set_measure_overlay(controls, True)
        # Clip both the blurred pixels and controls to the rounded glass surface.
        self.player_bar.add_css_class("glass-bar")  # window-matching radius so the clip is rounded too
        self.player_bar.set_overflow(Gtk.Overflow.HIDDEN)
        self._set_controls(False)

        outer = Gtk.Overlay(child=self.nav)
        outer.add_overlay(self.player_bar)
        self.toasts = Adw.ToastOverlay(child=outer)
        self.set_content(self.toasts)
        # Tab labels disappear below RESP_LABELS. Done as a breakpoint (not by hand) so the window's
        # minimum width is computed *with* the labels hidden and it can be resized narrow in one go.
        self.set_size_request(RESP_MIN_WIDTH, -1)  # narrowest width the icon-only layout fits in
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {RESP_LABELS}sp"))
        for lbl in self._nav_labels:
            bp.add_setter(lbl, "visible", False)
        self.add_breakpoint(bp)
        self.add_tick_callback(lambda _w, _clock: self._update_responsive())

    # ---- mini player
    def _build_mini(self):
        win = Gtk.Window(application=self.get_application(), title=APP_NAME, resizable=False,
                         default_width=MINI_SIZE, default_height=MINI_SIZE)
        win.set_titlebar(Gtk.Box())  # empty titlebar: CSD corners and shadow, no visible bar
        win.set_size_request(MINI_SIZE, MINI_SIZE)
        win.add_css_class("beet-mini")

        def icon_btn(icon, css, tip, cb):
            # `icon` is an icon name, or (artwork kind, width, height) for the transport artwork
            if isinstance(icon, tuple):
                b = Gtk.Button(child=TransportIcon(*icon), tooltip_text=tip, focus_on_click=False,
                               halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            else:
                b = Gtk.Button(icon_name=icon, tooltip_text=tip, focus_on_click=False,
                               halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            b.add_css_class("mini-btn")
            for c in css.split():
                b.add_css_class(c)
            b.connect("clicked", cb)
            return b

        # Full-bleed cover, 4px inside the window frame.
        art, art_pic = make_cover(MINI_SIZE - 4, accent=True)
        art.add_css_class("mini-art")
        art.set_halign(Gtk.Align.CENTER)
        art.set_valign(Gtk.Align.CENTER)
        self.now_pic.bind_property("paintable", art_pic, "paintable", GObject.BindingFlags.SYNC_CREATE)

        expand = icon_btn("beet-expand-symbolic", "mini-expand", "Back to full player",
                          lambda _b: self._leave_mini())
        expand.set_halign(Gtk.Align.START)
        expand.set_valign(Gtk.Align.START)
        expand.set_margin_start(11)
        expand.set_margin_top(10)
        # art sits 2px inside the window; the icon is an 18px box at (11, 10), with a little margin
        icon_region = (6, 5, 30, 29)
        art_pic.connect("notify::paintable",
                        lambda *_: adapt_icon_to_art(expand, art_pic, MINI_SIZE - 4, icon_region))
        adapt_icon_to_art(expand, art_pic, MINI_SIZE - 4, icon_region)

        # Frosted glass panel: a live blurred copy of the cover, a tint, then the controls.
        prev = icon_btn(("previous", 17, 16), "mini-skip", "Previous", self.prev)
        self.mini_play = icon_btn(("pause" if self.is_playing else "play", 30, 30),
                                  "mini-play", "Play / Pause", self.toggle)
        self.mini_play_icon = self.mini_play.get_child()
        nxt = icon_btn(("next", 17, 16), "mini-skip", "Next", self.next)
        controls = Gtk.Box(spacing=16, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        for w in (prev, self.mini_play, nxt):
            controls.append(w)
        for src, dst in zip(self._transport[:3], (prev, self.mini_play, nxt)):
            src.bind_property("sensitive", dst, "sensitive", GObject.BindingFlags.SYNC_CREATE)

        tint = Gtk.Box()
        tint.add_css_class("mini-tint")
        tint.set_can_target(False)
        panel = Gtk.Overlay(child=GlassBackdrop(art, blur=MINI_BLUR), halign=Gtk.Align.FILL,
                            valign=Gtk.Align.END, margin_start=5, margin_end=5, margin_bottom=5)
        panel.set_size_request(MINI_SIZE - 10, 54)
        panel.set_overflow(Gtk.Overflow.HIDDEN)
        panel.add_css_class("mini-panel")
        panel.add_overlay(tint)
        panel.add_overlay(controls)

        root = Gtk.Overlay(child=art, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        root.set_size_request(MINI_SIZE, MINI_SIZE)
        root.set_overflow(Gtk.Overflow.HIDDEN)
        root.add_css_class("mini-body")
        root.add_overlay(panel)
        root.add_overlay(expand)
        win.set_child(Gtk.WindowHandle(child=root))  # drag the window by any empty area
        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", lambda *_: panel.queue_draw())

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", lambda c, k, kc, s: self._on_key(c, k, kc, s, win))
        win.add_controller(keys)
        win.connect("close-request", lambda _w: self.get_application().quit() or False)
        self.mini = win

    def _on_close_request(self, _win):
        """Closing the main window must end the app. The mini player is a second window; if it
        were left behind (hidden), GTK would keep the application running forever."""
        mini, self.mini = self.mini, None
        if mini is not None:
            mini.destroy()
        try:
            self.player.set_state(Gst.State.NULL)  # release the audio device before exiting
        except Exception:
            pass
        return False

    def _toggle_mini(self, *_):
        if self.mini is None:
            self._build_mini()
        self._update_surface_theme(Adw.StyleManager.get_default())
        # A hidden window forgets its maximized / fullscreen state, so note it for the way back.
        self._mini_restore = (self.is_maximized(), self.is_fullscreen())
        self.mini.present()
        self.set_visible(False)

    def _leave_mini(self):
        maximized, fullscreen = getattr(self, "_mini_restore", (False, False))
        if maximized:
            self.maximize()
        if fullscreen:
            self.fullscreen()
        self.unminimize()
        self.present()
        # Hide the mini player only once the full window is up, then raise the full window again
        # so the compositor can't leave it minimized or behind when the mini player goes away.
        GLib.timeout_add(150, self._finish_leave_mini)

    def _finish_leave_mini(self):
        if self.mini:
            self.mini.set_visible(False)
        self.unminimize()
        self.present()
        return False

    def raise_window(self):
        if self.mini and self.mini.get_visible():
            self.mini.present()
        else:
            self.present()

    def _update_responsive(self):
        """Adapt the top bar and the player bar to the window width.

        >= RESP_FULL     track info | transport | repeat + volume (tabs have labels from RESP_LABELS)
        RESP_COMPACT..   volume slider hidden, repeat stays
        < RESP_COMPACT   player bar stacks: [track info ........ repeat] over [transport + seek]
        (The tab labels are handled by a breakpoint, see __init__.)
        """
        width = self.get_width()
        if width <= 0 or width == self._responsive_width:
            return True
        self._responsive_width = width
        compact = width < RESP_COMPACT
        if compact != self._responsive_compact:
            self._responsive_compact = compact
            row, top, stack = self._responsive_row, self._responsive_top, self._responsive_stack
            left, center, right = self._responsive_left, self._responsive_center, self._responsive_right
            if compact:
                self.add_css_class("beet-compact")
                row.set_start_widget(None)
                row.set_center_widget(None)
                row.set_end_widget(None)
                self._responsive_controls.remove(row)
                right.set_hexpand(False)
                top.append(left)
                top.append(right)
                stack.append(top)
                stack.append(center)
                self._responsive_controls.append(stack)
            else:
                self.remove_css_class("beet-compact")
                stack.remove(top)
                stack.remove(center)
                self._responsive_controls.remove(stack)
                top.remove(left)
                top.remove(right)
                right.set_hexpand(True)
                row.set_start_widget(left)
                row.set_center_widget(center)
                row.set_end_widget(right)
                self._responsive_controls.append(row)
        for w in self._responsive_volume:
            w.set_visible(width >= RESP_FULL and not compact)
        self._responsive_left.set_size_request(0, -1)
        self._responsive_center.set_size_request(0, -1)
        self.seek.set_size_request(70 if width < 450 else 120, -1)
        return True

    def _build_menu(self):
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Mini Player", "win.mini-player")
        section.append("Open Music Folder…", "win.open-folder")
        section.append("Rescan Library", "win.rescan")
        section.append("Fetch Missing Artwork", "win.fetch-art")
        menu.append_section(None, section)
        sort = Gio.Menu()
        for i, name in enumerate(SORT_MODES):
            sort.append(name, f"win.sort::{i}")
        menu.append_submenu("Sort Albums By", sort)
        theme = Gio.Menu()
        for key, name in THEMES:
            theme.append(name, f"win.theme::{key}")
        menu.append_submenu("Appearance", theme)
        return menu

    def _add_actions(self):
        for name, cb in (("open-folder", self._on_open),
                         ("rescan", lambda: self.refresh_library(manual=True)),
                         ("fetch-art", self.fetch_artwork),
                         ("mini-player", self._toggle_mini),
                         ("search", lambda: self.search_btn.set_active(not self.search_btn.get_active()))):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, cb=cb: cb())
            self.add_action(action)
        sort = Gio.SimpleAction.new_stateful("sort", GLib.VariantType.new("s"),
                                             GLib.Variant("s", str(self.sort_mode)))
        sort.connect("activate", self._on_sort_action)
        self.add_action(sort)
        theme = self.config.get("theme", "system")
        if theme not in SCHEMES:
            theme = "system"
        action = Gio.SimpleAction.new_stateful("theme", GLib.VariantType.new("s"),
                                               GLib.Variant("s", theme))
        action.connect("activate", self._on_theme_action)
        self.add_action(action)
        apply_color_scheme(theme)
        if IS_WINDOWS:  # notice when the Windows light/dark setting changes while the app is open
            self._win_dark = windows_prefers_dark()
            GLib.timeout_add_seconds(2, self._follow_windows_theme)

    def _sync_active_nav_button(self, *_args):
        """Mark the selected page explicitly, independent of libadwaita internals."""
        selected = self.stack.get_visible_child_name()
        for page, button in self._nav_buttons.items():
            if page == selected:
                button.add_css_class("beet-nav-active")
            else:
                button.remove_css_class("beet-nav-active")
        return False

    def _update_surface_theme(self, manager, *_args):
        for w in (self, self.mini):
            if w is None:
                continue
            w.remove_css_class("beet-dark")
            w.remove_css_class("beet-light")
            w.add_css_class("beet-dark" if manager.get_dark() else "beet-light")

    def _follow_windows_theme(self):
        dark = windows_prefers_dark()
        if dark != self._win_dark:
            self._win_dark = dark
            if self.config.get("theme", "system") == "system":
                apply_color_scheme("system")
        return True

    def _on_theme_action(self, action, param):
        name = param.get_string()
        if name not in SCHEMES:
            return
        action.set_state(param)
        apply_color_scheme(name)
        self.config["theme"] = name
        save_json(CONFIG, self.config)

    def _on_sort_action(self, action, param):
        action.set_state(param)
        self.sort_mode = int(param.get_string())
        self.album_sort.set_sorter(Gtk.CustomSorter.new(album_cmp(self.sort_mode), None))
        self.config["sort"] = self.sort_mode
        save_json(CONFIG, self.config)

    # ---- tile grids (albums / artists)
    def _make_grid(self, model, factory, on_activate):
        sel = Gtk.SingleSelection(model=model, autoselect=False, can_unselect=True)
        grid = Gtk.GridView(model=sel, factory=factory, min_columns=1, max_columns=12,
                            single_click_activate=True)
        grid.add_css_class("tiles")

        def activated(_view, pos):
            item = sel.get_item(pos)
            sel.unselect_all()
            if item:
                on_activate(item)
        grid.connect("activate", activated)
        return Gtk.ScrolledWindow(child=grid, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _tile_factory(self, circular, title_fn, sub_fn, tip_fn):
        factory = Gtk.SignalListItemFactory()
        with_play = not circular  # album grids (main + artist page) only

        def setup(_f, item):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                          margin_top=7, margin_bottom=12, margin_start=10, margin_end=10)
            cover, pic = make_cover(TILE, circular)
            cover.set_halign(Gtk.Align.CENTER)
            lead = cover
            if with_play:  # album tiles get a glass play/pause button, centred, shown on hover
                lead = Gtk.Overlay(child=cover, halign=Gtk.Align.CENTER)
                lead.add_css_class("tile-lead")
                scrim = Gtk.Box()  # hover tint; lives inside the cover so it is rounded and blurred with it
                scrim.add_css_class("tile-scrim")
                scrim.add_css_class("tile-play-layer")
                scrim.set_can_target(False)
                cover.add_overlay(scrim)
                glass = TileGlass(cover)
                glass.set_size_request(39, 39)  # odd, like the 165px tile and the button, so all centre on whole pixels
                btn = Gtk.Button(child=TransportIcon("play", 25, 25),
                                 focus_on_click=False, focusable=False, tooltip_text="Play album")
                btn.add_css_class("tile-play")
                btn.set_can_target(False)
                for w in (glass, btn):
                    w.add_css_class("tile-play-layer")
                    w.set_halign(Gtk.Align.CENTER)
                    w.set_valign(Gtk.Align.CENTER)
                    lead.add_overlay(w)
                heart = Gtk.Button(icon_name="beet-heart-outline-symbolic", focus_on_click=False,
                                   focusable=False, halign=Gtk.Align.START, valign=Gtk.Align.START,
                                   margin_start=5, margin_top=3)
                dots = Gtk.MenuButton(icon_name="beet-more-symbolic", focusable=False,
                                      halign=Gtk.Align.END, valign=Gtk.Align.END,
                                      margin_end=0, margin_bottom=4, tooltip_text="More")
                heart.add_css_class("tile-icon")
                dots.add_css_class("tile-icon")
                dots.add_css_class("tile-dots")
                heart.set_can_target(False)
                dots.set_can_target(False)
                for w in (heart, dots):
                    w.add_css_class("tile-play-layer")
                    lead.add_overlay(w)
                # keep the controls visible while this tile's menu is open
                dots.connect("notify::active", lambda b, _p, lead=lead: (
                    lead.add_css_class("menu-open") if b.get_active() else lead.remove_css_class("menu-open")))
            title = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=18, margin_top=6)
            title.add_css_class("album-tile-title")
            sub = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=22)
            sub.add_css_class("dim-label")
            sub.add_css_class("caption")
            sub.add_css_class("album-tile-subtitle")
            for w in (lead, title, sub):
                box.append(w)
            if with_play:
                motion = Gtk.EventControllerMotion()
                motion.connect("notify::contains-pointer",
                               lambda c, _p, lead=lead, ws=(btn, heart, dots): _tile_hover(
                                   lead, ws, c.get_property("contains-pointer")))
                box.add_controller(motion)
            item.set_child(box)

        def bind(_f, item):
            obj = item.get_item()
            lead = item.get_child().get_first_child()
            cover = lead.get_child() if with_play else lead
            # the Gtk.Picture is the first overlay child (right after the placeholder)
            pic = cover.get_first_child().get_next_sibling() if with_play else cover.get_last_child()
            title = lead.get_next_sibling()
            sub = title.get_next_sibling()
            if with_play:
                # overlay order: glass, play button, heart, menu
                dots = lead.get_last_child()
                heart = dots.get_prev_sibling()
                btn = heart.get_prev_sibling()
                self._play_hooks[item] = (
                    btn, btn.connect("clicked", lambda _b, a=obj: self._tile_play_clicked(a)), obj,
                    heart, heart.connect("clicked", lambda _b, a=obj: self.toggle_album_favorite(a)))
                dots.set_create_popup_func(self._create_album_menu, obj)
                _tile_icon(btn, self._album_is_playing(obj))
                _album_heart_set(heart, self._album_all_favorite(obj))
            title.set_text(title_fn(obj))
            sub.set_text(sub_fn(obj))
            title.set_tooltip_text(tip_fn(obj))
            self._bind_cover(item, obj, pic)

        def unbind(_f, item):
            self._unbind_cover(item)
            hook = self._play_hooks.pop(item, None)
            if hook:
                hook[0].disconnect(hook[1])
                hook[3].disconnect(hook[4])

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        factory.connect("unbind", unbind)
        return factory

    # ---- search
    def _match_album(self, a, _data):
        q = self.search_text
        return not q or q in a.title.casefold() or q in a.artist.casefold()

    def _match_artist(self, a, _data):
        q = self.search_text
        return not q or q in a.name.casefold()

    def _match_song(self, t, _data):
        q = self.search_text
        return not q or q in t.haystack

    def _song_setup(self, _f, item):
        row = Gtk.Box(spacing=12, margin_top=2, margin_bottom=2)
        cover, _pic = make_cover(40)
        cover.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(title)
        text.append(sub)
        dur = Gtk.Label(valign=Gtk.Align.CENTER)
        dur.add_css_class("dim-label")
        dur.add_css_class("numeric")
        heart = _make_heart_button()
        add = Gtk.MenuButton(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text="Add to playlist")
        add.add_css_class("flat")
        for w in (cover, text, dur, heart, add):
            row.append(w)
        item.set_child(row)

    def _song_bind(self, _f, item):
        t = item.get_item()
        cover = item.get_child().get_first_child()
        pic = cover.get_last_child()
        text = cover.get_next_sibling()
        title = text.get_first_child()
        sub = title.get_next_sibling()
        dur = text.get_next_sibling()
        heart = dur.get_next_sibling()
        add = heart.get_next_sibling()
        title.set_text(t.title)
        album = t.album.title if t.album else ""
        sub.set_text(" · ".join(p for p in (t.artist, album) if p))
        dur.set_text(fmt_time(t.length))
        add.set_create_popup_func(self._create_playlist_popover, [t])
        _heart_set(heart, t.favorite)
        self._row_hooks[item] = (
            heart, heart.connect("clicked", lambda _b, t=t: self.toggle_favorite(t)),
            t, t.connect("notify::favorite", lambda tr, _p, h=heart: _heart_set(h, tr.favorite)))
        if t.album:
            self._bind_cover(item, t.album, pic, "cover_small")

    def _bind_cover(self, item, obj, pic, kind="cover"):
        obj.acquire_cover(kind)
        binding = obj.bind_property(kind, pic, "paintable", GObject.BindingFlags.SYNC_CREATE)
        self._binds[item] = (binding, obj, kind, pic)

    def _unbind_cover(self, item):
        hook = self._binds.pop(item, None)
        if hook:
            binding, obj, kind, pic = hook
            binding.unbind()
            pic.set_paintable(None)  # the picture itself would otherwise keep the texture alive
            obj.release_cover(kind)

    def _song_unbind(self, _f, item):
        self._unbind_cover(item)
        hook = self._row_hooks.pop(item, None)
        if hook:
            heart, hid, track, nid = hook
            heart.disconnect(hid)
            track.disconnect(nid)

    def _make_song_view(self, sel):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._song_setup)
        factory.connect("bind", self._song_bind)
        factory.connect("unbind", self._song_unbind)
        view = Gtk.ListView(model=sel, factory=factory, single_click_activate=True)
        view.add_css_class("rich-list")
        view.add_css_class("songs-list")
        view.connect("activate", lambda _v, pos: self._on_song_activated(sel, pos))
        return view

    def _on_song_activated(self, sel, pos):
        """Play the clicked song; the songs currently listed (after search) become the queue."""
        tracks = [sel.get_item(i) for i in range(sel.get_n_items())]
        sel.unselect_all()
        self.play_tracks(tracks, pos)

    # ---- favorites
    def toggle_favorite(self, track):
        if track.path in self.fav_set:
            self.favorites.remove(track.path)
            self.fav_set.discard(track.path)
        else:
            self.favorites.insert(0, track.path)  # newest favorite first
            self.fav_set.add(track.path)
        track.favorite = track.path in self.fav_set
        save_json(FAV_FILE, self.favorites)
        self._rebuild_favorites()
        self._refresh_fav_buttons()
        self._sync_tile_hearts()

    def _rebuild_favorites(self):
        tracks = [self.tracks_by_path[p] for p in self.favorites if p in self.tracks_by_path]
        self.fav_store.remove_all()
        self.fav_store.splice(0, 0, tracks)
        self.fav_stack.set_visible_child_name("list" if tracks else "empty")

    def _refresh_fav_buttons(self):
        t = self.queue[self.index] if self.queue else None
        on = bool(t and t.favorite)
        _heart_set(self.fav_btn, on)

    def _bind_row_heart(self, heart, t):
        """Keep a heart button in a one-off row (album / playlist page) in step with the track."""
        _heart_set(heart, t.favorite)
        heart.connect("clicked", lambda _b: self.toggle_favorite(t))
        nid = t.connect("notify::favorite", lambda tr, _p: _heart_set(heart, tr.favorite))
        heart.connect("destroy", lambda _w: t.disconnect(nid))

    def _on_search(self, entry):
        was_searching = bool(self.search_text)
        self.search_text = entry.get_text().strip().casefold()
        if self.search_text and not was_searching:
            self._scroll_lock = True  # filtered results shouldn't overwrite the browse position
        elif was_searching and not self.search_text:
            GLib.idle_add(lambda: (self._restore_album_scroll(), False)[1])
        self.album_filter.changed(Gtk.FilterChange.DIFFERENT)
        self.artist_filter.changed(Gtk.FilterChange.DIFFERENT)
        self.song_filter.changed(Gtk.FilterChange.DIFFERENT)
        self.fav_filter.changed(Gtk.FilterChange.DIFFERENT)
        self.refresh_playlists()

    # ---- detail pages
    def _push_page(self, title, content, end_widget=None, clamp=820, tag=None):
        content.set_margin_bottom(content.get_margin_bottom() + BAR_SPACE)  # clear of the player
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        if end_widget:
            header.pack_end(end_widget)
        view.add_top_bar(header)
        view.set_content(Gtk.ScrolledWindow(
            child=Adw.Clamp(child=content, maximum_size=clamp),
            hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.nav.push(Adw.NavigationPage(title=title, child=view, tag=tag))

    def _heading_button(self, label, icon, cb):
        if icon == "media-playback-start-symbolic":  # the Play artwork's triangle, not a theme icon
            content = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
            content.append(TransportIcon("playglyph", 12, 14))
            content.append(Gtk.Label(label=label))
        else:
            content = Adw.ButtonContent(icon_name=icon, label=label)
        btn = Gtk.Button(child=content)
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.connect("clicked", cb)
        return btn

    def open_album(self, album):
        tag = f"album-{album.key}"
        existing = self.nav.find_page(tag)
        if existing is not None:  # already in the stack: go back to it instead of stacking a copy
            self.nav.pop_to_page(existing)
            return
        cover, pic = make_cover(200)
        album.acquire_cover("cover_large")
        cover_binding = album.bind_property("cover_large", pic, "paintable", GObject.BindingFlags.SYNC_CREATE)

        def drop_cover(_w):  # the page is gone: let the big texture go
            cover_binding.unbind()
            album.release_cover("cover_large")
        cover.connect("destroy", drop_cover)

        title = Gtk.Label(label=album.title, xalign=0, wrap=True)
        title.add_css_class("title-1")
        artist = Gtk.Label(label=album.artist, xalign=0, wrap=True)
        artist.add_css_class("title-4")
        meta_parts = [str(album.year) if album.year else "", f"{len(album.tracks)} songs",
                      fmt_long(sum(t.length for t in album.tracks))]
        meta = Gtk.Label(label=" · ".join(p for p in meta_parts if p), xalign=0)
        meta.add_css_class("dim-label")

        buttons = Gtk.Box(spacing=8, margin_top=8)
        buttons.append(self._heading_button("Play", "media-playback-start-symbolic",
                                            lambda _b: self.play_tracks(album.tracks, 0)))
        buttons.append(self._add_to_playlist_button(album.tracks, "Add to Playlist"))

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                       valign=Gtk.Align.CENTER, hexpand=True)
        for w in (title, artist, meta, buttons):
            info.append(w)
        head = Gtk.Box(spacing=24)
        head.append(cover)
        head.append(info)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                       margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        body.append(head)
        body.append(self._track_list(album.tracks, lambda i: self.play_tracks(album.tracks, i)))
        self._push_page(album.title, body, tag=tag)

    def open_artist(self, artist):
        store = Gio.ListStore(item_type=Album)
        store.splice(0, 0, artist.albums)
        model = Gtk.SortListModel(model=store, sorter=Gtk.CustomSorter.new(album_cmp(0), None))
        grid = self._make_grid(
            model,
            self._tile_factory(False, lambda a: a.title,
                               lambda a: str(a.year) if a.year else "",
                               lambda a: f"{a.title} — {a.artist}"),
            self.open_album)
        view = Adw.ToolbarView(content=grid)
        view.add_top_bar(Adw.HeaderBar())
        self.nav.push(Adw.NavigationPage(title=artist.name, child=view))

    def _track_list(self, tracks, on_play, on_remove=None, album_mode=True):
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        for i, t in enumerate(tracks):
            row = Adw.ActionRow(title=esc(t.title), activatable=True)
            if album_mode:
                num = Gtk.Label(label=str(t.number or i + 1), width_chars=3, xalign=1)
                num.add_css_class("dim-label")
                num.add_css_class("numeric")
                row.add_prefix(num)
            else:
                row.set_subtitle(esc(f"{t.artist} — {t.album.title}" if t.album else t.artist))
            dur = Gtk.Label(label=fmt_time(t.length))
            dur.add_css_class("dim-label")
            dur.add_css_class("numeric")
            row.add_suffix(dur)
            heart = _make_heart_button()
            self._bind_row_heart(heart, t)
            row.add_suffix(heart)
            if on_remove:
                rm = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text="Remove from playlist")
                rm.add_css_class("flat")
                rm.connect("clicked", lambda _b, t=t: on_remove(t))
                row.add_suffix(rm)
            else:
                row.add_suffix(self._add_to_playlist_button([t]))
            row.connect("activated", lambda _r, i=i: on_play(i))
            lb.append(row)
        return lb

    # ---- playlists
    def save_playlists(self):
        save_json(PL_FILE, self.playlists)

    def playlist_tracks(self, p):
        return [self.tracks_by_path[x] for x in p["tracks"] if x in self.tracks_by_path]

    def refresh_playlists(self):
        row = self.pl_list.get_first_child()
        while row:
            nxt = row.get_next_sibling()
            self.pl_list.remove(row)
            row = nxt
        q = self.search_text
        for p in self.playlists:
            if q and q not in p["name"].casefold():
                continue
            n = len(self.playlist_tracks(p))
            r = Adw.ActionRow(title=esc(p["name"]), subtitle=f"{n} song" + ("" if n == 1 else "s"),
                              activatable=True)
            r.add_prefix(Gtk.Image(icon_name="beet-playlists-symbolic"))
            r.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            r.connect("activated", lambda _r, p=p: self.open_playlist(p))
            self.pl_list.append(r)
        self.pl_stack.set_visible_child_name("list" if self.playlists else "empty")

    def add_to_playlist(self, p, tracks):
        added = 0
        for t in tracks:
            if t.path not in p["tracks"]:
                p["tracks"].append(t.path)
                added += 1
        self.save_playlists()
        self.refresh_playlists()
        self.toasts.add_toast(Adw.Toast.new(
            f"Added {added} song{'' if added == 1 else 's'} to “{p['name']}”"))

    def _add_to_playlist_button(self, tracks, label=None):
        if label:
            btn = Gtk.MenuButton(child=Adw.ButtonContent(icon_name="list-add-symbolic", label=label))
            btn.add_css_class("pill")
        else:
            btn = Gtk.MenuButton(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text="Add to playlist")
            btn.add_css_class("flat")
        btn.set_create_popup_func(self._create_playlist_popover, tracks)
        return btn

    def _styled_popover(self):
        pop = Gtk.Popover()
        # The GTK arrow and root surface can expose a square backing around
        # rounded contents; use a clean arrowless popover instead.
        pop.set_has_arrow(False)
        pop.add_css_class("beet-playlist-popover")
        pop.add_css_class("beet-popup-dark" if Adw.StyleManager.get_default().get_dark()
                          else "beet-popup-light")
        return pop

    def _popup_item(self, pop, box, text, cb, close=True):
        b = Gtk.Button(child=Gtk.Label(label=text, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                       max_width_chars=26))
        b.add_css_class("flat")
        b.add_css_class("beet-playlist-item")
        b.connect("clicked", lambda _b: (pop.popdown() if close else None, cb()))
        box.append(b)

    def _playlist_choices_box(self, pop, tracks):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        for p in self.playlists:
            self._popup_item(pop, box, p["name"], lambda p=p: self.add_to_playlist(p, tracks))
        if self.playlists:
            box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        self._popup_item(pop, box, "New Playlist…", lambda: self.prompt_new_playlist(tracks))
        return box

    def _create_playlist_popover(self, btn, tracks):
        pop = self._styled_popover()
        pop.set_child(self._playlist_choices_box(pop, tracks))
        btn.set_popover(pop)

    def _create_album_menu(self, btn, album):
        """Three-dot menu on an album tile: one entry, Add to Playlist, which opens the playlist choices."""
        pop = self._styled_popover()
        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                       margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        self._popup_item(pop, menu, "Add to Playlist",
                         lambda: pop.set_child(self._playlist_choices_box(pop, list(album.tracks))),
                         close=False)
        pop.set_child(menu)
        btn.set_popover(pop)

    def prompt_new_playlist(self, tracks=None):
        entry = Gtk.Entry(placeholder_text="Playlist name", activates_default=True)
        dlg = Adw.AlertDialog(heading="New Playlist", body="Give your playlist a name.")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("create", "Create")
        dlg.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("create")
        dlg.set_close_response("cancel")

        def on_response(_d, response):
            name = entry.get_text().strip()
            if response != "create" or not name:
                return
            p = {"name": name, "tracks": []}
            self.playlists.append(p)
            if tracks:
                self.add_to_playlist(p, tracks)
            else:
                self.save_playlists()
                self.refresh_playlists()

        dlg.connect("response", on_response)
        dlg.present(self)
        entry.grab_focus()

    def open_playlist(self, p):
        title = Gtk.Label(label=p["name"], xalign=0, wrap=True)
        title.add_css_class("title-1")
        count = Gtk.Label(xalign=0)
        count.add_css_class("dim-label")
        play = self._heading_button("Play", "media-playback-start-symbolic",
                                    lambda _b: self.play_tracks(self.playlist_tracks(p), 0))
        play.set_halign(Gtk.Align.START)
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        for w in (title, count, play, holder):
            body.append(w)

        def remove(t):
            p["tracks"].remove(t.path)
            self.save_playlists()
            rebuild()
            self.refresh_playlists()

        def rebuild():
            tracks = self.playlist_tracks(p)
            count.set_text(f"{len(tracks)} song" + ("" if len(tracks) == 1 else "s"))
            play.set_sensitive(bool(tracks))
            old = holder.get_first_child()
            if old:
                holder.remove(old)
            holder.append(self._track_list(tracks, lambda i: self.play_tracks(tracks, i),
                                           on_remove=remove, album_mode=False))

        rebuild()
        delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete playlist")
        delete.connect("clicked", lambda _b: self._confirm_delete(p))
        self._push_page(p["name"], body, end_widget=delete)

    def _confirm_delete(self, p):
        dlg = Adw.AlertDialog(heading=f"Delete “{p['name']}”?",
                              body="This only removes the playlist, not your music files.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_close_response("cancel")

        def on_response(_d, response):
            if response == "delete":
                self.playlists.remove(p)
                self.save_playlists()
                self.refresh_playlists()
                self.nav.pop()

        dlg.connect("response", on_response)
        dlg.present(self)

    # ---- library
    def _on_open(self):
        dlg = Gtk.FileDialog(title="Choose your music folder")
        dlg.select_folder(self, None, self._on_folder_chosen)

    def _on_folder_chosen(self, dlg, result):
        try:
            folder = dlg.select_folder_finish(result)
        except GLib.Error:
            return
        self.folder = folder.get_path()
        self.config["folder"] = self.folder
        self._album_ratio = 0.0  # a different library starts from the top
        self.config["album_scroll"] = 0.0
        save_json(CONFIG, self.config)
        self.refresh_library(rerun=True)

    def refresh_library(self, manual=False, rerun=False):
        """Show the saved library at once, then look for new, changed or removed files."""
        if self._syncing:
            if rerun:
                self._rerun = True
            return
        if not self.folder or not os.path.isdir(self.folder):
            return
        self._syncing = True
        self._last_check = time.monotonic()
        threading.Thread(target=self._library_thread, args=(self.folder, manual),
                         daemon=True).start()

    def _library_thread(self, folder, manual):
        stats = {"added": 0, "updated": 0, "removed": 0, "changed": False}
        try:
            index = load_index(folder)
            had_cache = bool(index["files"])
            if had_cache:  # instant start: no scanning, straight from the saved index
                GLib.idle_add(self._library_loaded, build_albums(index))
            else:  # very first sync
                GLib.idle_add(self._set_busy)
            if manual:
                GLib.idle_add(self._set_busy)
            stats = sync_library(folder, index, lambda: GLib.idle_add(self._set_busy))
            if stats["changed"] or not had_cache:
                GLib.idle_add(self._library_loaded, build_albums(index))
            if index.pop("dirty", False):
                save_index(index)
        except Exception as e:  # keep the app usable whatever happens
            print("Library sync failed:", e, file=sys.stderr)
        finally:
            GLib.idle_add(self._sync_finished, stats, manual)

    def _set_busy(self):
        self.spinner.start()
        if self.album_store.get_n_items() == 0:
            self.empty_page.set_title("Scanning…")

    # ---- remembering where you scrolled in Albums
    def _on_album_scrolled(self, adj):
        if self._scroll_lock:
            return
        span = adj.get_upper() - adj.get_page_size()
        self._album_ratio = min(1.0, max(0.0, adj.get_value() / span)) if span > 0 else 0.0
        if not self._scroll_save_id:  # write to disk a moment after scrolling settles
            self._scroll_save_id = GLib.timeout_add_seconds(2, self._save_album_scroll)

    def _save_album_scroll(self):
        self._scroll_save_id = 0
        if self.config.get("album_scroll") != round(self._album_ratio, 4):
            self.config["album_scroll"] = round(self._album_ratio, 4)
            save_json(CONFIG, self.config)
        return False

    def _restore_album_scroll(self):
        """Put the album grid back where it was. The grid measures itself lazily, so keep
        nudging it as its size settles, then hand control back to the user."""
        self._scroll_lock = True
        adj = self.album_scroller.get_vadjustment()
        ratio = self._album_ratio

        def apply(*_):
            span = adj.get_upper() - adj.get_page_size()
            if span > 0:
                adj.set_value(ratio * span)

        hid = adj.connect("changed", apply)
        GLib.idle_add(lambda: (apply(), False)[1])

        def finish():
            adj.disconnect(hid)
            apply()
            self._scroll_lock = bool(self.search_text)  # while searching, keep the browse position
            return False
        GLib.timeout_add(1200, finish)

    def _library_loaded(self, albums):
        sig = tuple((a.key, a.year, tuple((t.path, t.title, t.artist, t.length) for t in a.tracks))
                    for a in albums)
        if sig == self._lib_sig:
            return False  # nothing changed: leave the lists (and the scroll position) alone
        self._lib_sig = sig
        self._scroll_lock = True  # emptying the model snaps the grid to the top; don't record that
        self.album_store.remove_all()
        self.album_store.splice(0, 0, albums)
        self.artists, self.tracks_by_path = {}, {}
        for a in albums:
            self.artists.setdefault(a.artist, Artist(a.artist)).albums.append(a)
            for t in a.tracks:
                self.tracks_by_path[t.path] = t
                t.haystack = f"{t.title} {t.artist} {a.title}".casefold()
                t.favorite = t.path in self.fav_set
        self.artist_store.remove_all()
        self.artist_store.splice(0, 0, list(self.artists.values()))
        self.song_store.remove_all()
        self.song_store.splice(0, 0, sorted(self.tracks_by_path.values(),
                                            key=lambda t: (t.title.casefold(), t.artist.casefold())))
        self._rebuild_favorites()
        self._refresh_fav_buttons()
        GLib.timeout_add_seconds(2, lambda: (trim_memory(), False)[1])
        if albums:
            self.albums_stack.set_visible_child_name("grid")
            self._restore_album_scroll()
        else:
            self.empty_page.set_title("No Music Found")
            self.albums_stack.set_visible_child_name("empty")
        self.refresh_playlists()
        return False

    def _sync_finished(self, stats, manual):
        self._syncing = False
        self.spinner.stop()
        if stats["changed"]:
            parts = []
            if stats["added"]:
                parts.append(f"{stats['added']} new song" + ("" if stats["added"] == 1 else "s"))
            if stats["updated"]:
                parts.append(f"{stats['updated']} updated")
            if stats["removed"]:
                parts.append(f"{stats['removed']} removed")
            self.toasts.add_toast(Adw.Toast.new("Library updated: " + ", ".join(parts)))
        elif manual:
            self.toasts.add_toast(Adw.Toast.new("Library is up to date"))
        if self._rerun:
            self._rerun = False
            self.refresh_library()
        return False

    def _on_active(self, *_):
        """Pick up music added while the app was in the background (checked at most once a minute)."""
        if self.is_active() and not self._fetching and time.monotonic() - self._last_check > 60:
            self.refresh_library()

    # ---- online artwork
    def fetch_artwork(self):
        if self._fetching:
            return
        now = time.time()
        todo = []
        for i in range(self.album_store.get_n_items()):
            a = self.album_store.get_item(i)
            if (a.cover_file is None and a.title != "Unknown Album"
                    and now - self.misses.get(a.key, 0) > MISS_TTL):
                todo.append(a)
        if not todo:
            self.toasts.add_toast(Adw.Toast.new("No missing artwork to fetch"))
            return
        self._fetching = True
        self._fetch_ok = self._fetch_reached = 0
        self.spinner.start()
        self.toasts.add_toast(Adw.Toast.new(f"Fetching artwork for {len(todo)} albums…"))
        threading.Thread(target=self._fetch_thread, args=(todo,), daemon=True).start()

    def _fetch_thread(self, todo):
        for i, album in enumerate(todo):
            if i:
                time.sleep(1.1)  # MusicBrainz allows ~1 request per second
            data, reached = fetch_cover_bytes(album.title, album.artist)
            ok = bool(data) and make_thumb(data, cover_file_for(album.key))
            GLib.idle_add(self._fetch_progress, album, ok, reached)
        GLib.idle_add(self._fetch_done, len(todo))

    def _fetch_progress(self, album, ok, reached):
        if reached:
            self._fetch_reached += 1
        if ok:
            self._fetch_ok += 1
            album.cover_file = cover_file_for(album.key)
            album.refresh_covers()  # notifies every widget currently showing this album
            artist = self.artists.get(album.artist)
            if artist:
                artist.refresh_covers()
        elif reached:
            self.misses[album.key] = time.time()
        return False

    def _fetch_done(self, total):
        self._fetching = False
        self.spinner.stop()
        save_json(MISS_FILE, self.misses)
        if self._fetch_reached == 0:
            msg = "Couldn't reach the artwork services — check your connection"
        else:
            msg = f"Found artwork for {self._fetch_ok} of {total} albums"
        self.toasts.add_toast(Adw.Toast.new(msg))
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        if IS_WINDOWS:  # GTK on Windows defaults to Segoe UI; the app is designed around Adwaita Sans
            Gtk.Settings.get_default().set_property("gtk-font-name", "Adwaita Sans, Segoe UI 11")
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        if os.path.exists(_icon_path()) or not theme.has_icon(APP_ID):
            ensure_icon()
        Gtk.Window.set_default_icon_name(APP_ID)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.DEFAULT)
        ensure_icons()
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # Popovers are separate native surfaces.  A display-level provider with
        # higher priority prevents the theme's opaque rectangular popover root
        # from showing outside the rounded GTK `contents` node.
        playlist_surface_css = Gtk.CssProvider()
        playlist_surface_css.load_from_string("""
            /* Native menu: remove the square outer backdrop without changing
               GTK's menu contents, spacing, colors, or hover behavior. */
            popover.beet-native-main-menu,
            popover.beet-native-main-menu.background {
                background-color: transparent;
                background-image: none;
                border: none;
                box-shadow: none;
            }
            popover.beet-playlist-popover,
            popover.beet-playlist-popover.background {
                background-color: transparent;
                background-image: none;
                border: none;
                box-shadow: none;
            }
            popover.beet-playlist-popover > contents {
                border-radius: 14px;
                background-clip: padding-box;
                box-shadow: 0 6px 20px rgba(0,0,0,0.20);
            }
            popover.beet-playlist-popover.beet-popup-dark > contents {
                background-color: #222224;
                color: #FFFFFF;
            }
            popover.beet-playlist-popover.beet-popup-light > contents {
                background-color: #FFFFFF;
                color: #000000;
            }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), playlist_surface_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)
        # GTK tooltips are separate popup surfaces, not descendants of our
        # beet-light/beet-dark window. Style their foreground independently.
        tooltip_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), tooltip_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

        def sync_tooltip_colors(manager, *_args):
            if manager.get_dark():
                foreground, background = "#FFFFFF", "#303030"
            else:
                foreground, background = "#000000", "#FFFFFF"
            tooltip_css.load_from_string(
                "tooltip, tooltip.background {"
                f" background-color: {background}; color: {foreground}; }}"
                f" tooltip label {{ color: {foreground}; }}"
            )

        manager = Adw.StyleManager.get_default()
        manager.connect("notify::dark", sync_tooltip_colors)
        sync_tooltip_colors(manager)
        self.set_accels_for_action("win.search", ["<Control>f"])
        self.set_accels_for_action("win.open-folder", ["<Control>o"])
        self.set_accels_for_action("win.mini-player", ["<Control>m"])

    def do_activate(self):
        win = self.props.active_window or PlayerWindow(self)
        win.raise_window() if isinstance(win, PlayerWindow) else win.present()


if __name__ == "__main__":
    if "--install" in sys.argv:
        install_app()
        sys.exit(0)
    migrate_legacy()
    Gst.init(None)
    sys.exit(App().run(sys.argv))
