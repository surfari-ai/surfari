#!/bin/bash
set -e  # Exit immediately on any error
include_bundled_chromium=false

current_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$current_dir/.."
echo "Current working directory: $PWD"

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
else
  echo "Using existing virtual environment at .venv"
fi

# --- Activate virtual environment (cross-platform) ---
if [[ -n "$VIRTUAL_ENV" ]]; then
  echo "Already inside a virtual environment: $VIRTUAL_ENV"
else
  if [[ -f ".venv/bin/activate" ]]; then
    # Unix-like
    # shellcheck disable=SC1091
    source .venv/bin/activate
    echo "Activated virtual environment (.venv/bin/activate)"
  elif [[ -f ".venv/Scripts/activate" ]]; then
    # Git Bash / MSYS Python venv
    # shellcheck disable=SC1091
    source .venv/Scripts/activate
    echo "Activated virtual environment (.venv/Scripts/activate)"
  else
    echo "Could not find an activation script in .venv."
    echo "   Tried: .venv/bin/activate and .venv/Scripts/activate"
    exit 1
  fi
fi

pip install --upgrade pip

echo "Installing dependencies..."
pip install -e . 
python -m playwright install chromium

echo "Cleaning up previous builds..."
rm -rf build dist

cd surfari

for fname in security/google_client_secret.json; do
  if [ ! -f "$fname" ]; then
    echo "Creating empty $fname..."
    cp $HOME/.surfari/google_client_secret.json "$fname"
  else
    echo "$fname already exists."
  fi
done

cd ..

echo "Building the project with PyInstaller..."
PYTHONPATH=surfari python -m PyInstaller navigation_cli.spec

echo "Detecting OS..."
if [[ "$OSTYPE" == "darwin"* ]]; then
  PLAYWRIGHT_INSTALL_FOLDER="$HOME/Library/Caches/ms-playwright"
  OS_SUFFIX="macos"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
  PLAYWRIGHT_INSTALL_FOLDER="$HOME/.cache/ms-playwright"
  OS_SUFFIX="linux"
elif [[ "$OSTYPE" == "msys"* || "$OSTYPE" == "cygwin"* || "$OSTYPE" == "win32" ]]; then
  PLAYWRIGHT_INSTALL_FOLDER="$LOCALAPPDATA/ms-playwright"
  OS_SUFFIX="windows"
elif grep -qEi "(Microsoft|WSL)" /proc/version &> /dev/null; then
  PLAYWRIGHT_INSTALL_FOLDER="$HOME/.cache/ms-playwright"
  OS_SUFFIX="wsl"
else
  echo "Unsupported OS: $OSTYPE"
  exit 1
fi

echo "PLAYWRIGHT_INSTALL_FOLDER: $PLAYWRIGHT_INSTALL_FOLDER"
echo "OS_SUFFIX: $OS_SUFFIX"

if [ "$include_bundled_chromium" = true ]; then
  CHROMIUM_FOLDER=$(find "$PLAYWRIGHT_INSTALL_FOLDER" -maxdepth 1 -type d -name "chromium-*" | sort -V | tail -n 1)

  if [[ -z "$CHROMIUM_FOLDER" ]]; then
    echo "No chromium-* folder found in $PLAYWRIGHT_INSTALL_FOLDER"
    exit 1
  fi

  DEST="dist/navigation_cli/_internal/playwright/driver/package/.local-browsers"
  mkdir -p "$DEST"
  cp -r "$CHROMIUM_FOLDER" "$DEST"
  OS_SUFFIX="${OS_SUFFIX}-chromium"

  echo "Copied Chromium to: $DEST/$(basename "$CHROMIUM_FOLDER")"
fi


ARCHIVE_BASENAME="surfari-${OS_SUFFIX}-dist"
ZIP_NAME="${ARCHIVE_BASENAME}.zip"
if [ -f "$ZIP_NAME" ]; then
  echo "Removing existing archive: $ZIP_NAME"
  rm "$ZIP_NAME"
fi
echo "Creating archive..."

python -c "import shutil; shutil.make_archive('$ARCHIVE_BASENAME', 'zip', 'dist')"

echo "Build complete! Created archive: $ZIP_NAME"

mv "$ZIP_NAME" installers/
echo "Moved archive to installers directory: installers/$ZIP_NAME"
