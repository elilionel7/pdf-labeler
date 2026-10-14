#!/bin/bash
# Builds "PDF Labeler.app" so the labeler can be launched from Finder,
# Spotlight, or the Dock without opening an editor or Terminal.
#
#   ./make_app.sh                  # installs into ~/Applications
#   ./make_app.sh /Applications    # or any other folder
#
# The app runs app.py from this folder using .venv, so code changes take
# effect on the next launch -- no need to rebuild. Re-run only if you move
# this project folder. Output/errors go to ~/.pdf-labeler/app.log.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST_DIR="${1:-$HOME/Applications}"
APP="$DEST_DIR/PDF Labeler.app"

if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
    echo "No .venv found in $PROJECT_DIR -- see README section 2 first." >&2
    exit 1
fi

mkdir -p "$DEST_DIR"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>             <string>PDF Labeler</string>
    <key>CFBundleDisplayName</key>      <string>PDF Labeler</string>
    <key>CFBundleIdentifier</key>       <string>local.pdf-labeler</string>
    <key>CFBundleExecutable</key>       <string>launcher</string>
    <key>CFBundlePackageType</key>      <string>APPL</string>
    <key>CFBundleVersion</key>          <string>1.0</string>
    <key>LSMinimumSystemVersion</key>   <string>11.0</string>
    <key>NSHighResolutionCapable</key>  <true/>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/launcher" <<LAUNCHER
#!/bin/bash
# Finder-launched apps get a bare PATH; add Homebrew so tesseract/poppler work.
export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"
mkdir -p "\$HOME/.pdf-labeler"
cd "$PROJECT_DIR"
exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/app.py" >> "\$HOME/.pdf-labeler/app.log" 2>&1
LAUNCHER
chmod +x "$APP/Contents/MacOS/launcher"

echo "Built: $APP"
echo "Open it from Finder/Spotlight, or drag it to the Dock."
