# Beet Music for Windows

The same app, the same code (`app/beet-music.py`), packaged for Windows with GTK 4 and libadwaita, so the
interface is drawn by the same toolkit as on Linux. This folder builds two things:

* `BeetMusic-Setup.exe` – installer (per-user, no administrator rights needed)
* `BeetMusic-portable.zip` – unzip anywhere and run `Beet Music.exe`

## Build it (about 10 minutes, nothing to install on your computer)

1. Create a new repository on GitHub (private is fine) and upload the contents of this folder to it
   (keep the `.github` folder – it is the build recipe; it starts with a dot, so some file managers hide it –
   in Nautilus press Ctrl+H, in Dolphin Alt+.).
2. Open the repository's **Actions** tab → **Build Windows app** → **Run workflow**.
3. When it turns green, open the run and download the **BeetMusic-Windows** artifact.
   Inside: `BeetMusic-Setup.exe` and `BeetMusic-portable.zip`.

### Or build on a Windows PC
Install [MSYS2](https://www.msys2.org), open **MSYS2 MINGW64**, install the packages listed in
`.github/workflows/build-windows.yml` with `pacman -S --needed`, then run `bash windows/bundle.sh`.
The app folder appears in `dist/BeetMusic` (run `Beet Music.exe` in it). For the installer, compile
`windows/installer.iss` with [Inno Setup](https://jrsoftware.org/isinfo.php).

## What is the same, and what is different

Same: all windows, layouts, colours, icons, the glass player bar, hover play / heart / menu on albums,
mini player, favorites, playlists, remembered scroll position, artwork lookup, GStreamer playback.
The Adwaita Sans font is bundled (and installed for your user on first launch) so text looks the same.

Handled differently on Windows (the Linux way doesn't exist there):
* **Media keys** (play/pause/next/previous) use Windows hot keys instead of MPRIS.
* **Light / dark "system" theme** is read from Windows' *Choose your default app mode* setting.
* **Settings and library data** live in `%APPDATA%\beet-music*.json` and `%LOCALAPPDATA%\beet-music`.
  Favorites and playlists store file paths, so copying them over from Linux won't match your Windows paths.
* The default music folder is your Windows *Music* folder.

## If something goes wrong
Start `Beet Music (debug).cmd` from the app folder – it runs the app with a console window that shows errors.
Windows SmartScreen may warn about the installer because it isn't code-signed; choose *More info → Run anyway*.
The very first launch takes a little longer while GStreamer indexes its plug-ins.

Fonts: Adwaita Sans (SIL Open Font License, see `fonts/`).
