#!/bin/bash
set -e  # Exit on error
set -x  # Print commands before execution

# ------------------------------
# 0. Extract version from pyproject.toml
# ------------------------------
cd ..
VERSION=$(grep -m1 '^version *= *' pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/')
if [[ -z "$VERSION" ]]; then
  echo "❌ Could not extract version from pyproject.toml"
  exit 1
fi
echo "📌 Project version: $VERSION"

# ------------------------------
# 1. Clean previous build artifacts
# ------------------------------
rm -rf build dist

# ------------------------------
# 2. Ensure archive exists
# ------------------------------
ZIP_NAME="surfari-${VERSION}-linux.zip"

if [ ! -f "installers/$ZIP_NAME" ]; then
  echo "ℹ️  $ZIP_NAME does not exist. Building it first..."
  bash installers/build_and_package.sh
  rm -rf build dist
fi

# ------------------------------
# 3. Copy archive into container_launcher
# ------------------------------
cp "installers/$ZIP_NAME" "container_launcher/$ZIP_NAME"

cd container_launcher

# ------------------------------
# 4. Clean docker environment
# ------------------------------
docker rm -f $(docker ps -aq) || true
docker rmi -f $(docker images -q) || true
docker builder prune -af

# ------------------------------
# 5. Build docker image with version + latest tags
# ------------------------------
docker build \
  --build-arg SURFARI_VERSION="$VERSION" \
  -t surfari-debian-kasm:$VERSION \
  -t surfari-debian-kasm:latest \
  .

# ------------------------------
# 6. Cleanup copied archive
# ------------------------------
rm "$ZIP_NAME"

echo "✅ Docker image built:"
echo "   - surfari-debian-kasm:$VERSION"
echo "   - surfari-debian-kasm:latest"
