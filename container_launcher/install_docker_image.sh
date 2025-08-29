#!/bin/bash
set -e

# Default: do not pull image
PULL_IMAGE=false

# Parse arguments
for arg in "$@"; do
  case $arg in
    --pull-image)
      PULL_IMAGE=true
      shift
      ;;
  esac
done

echo "[INFO] Updating apt..."
sudo apt update

echo "[INFO] Installing prerequisites..."
sudo apt install -y apt-transport-https ca-certificates curl gnupg lsb-release

echo "[INFO] Adding Docker’s GPG key..."
curl -fsSL https://download.docker.com/linux/$(. /etc/os-release; echo "$ID")/gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg

echo "[INFO] Adding Docker repository..."
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] \
  https://download.docker.com/linux/$(. /etc/os-release; echo "$ID") \
  $(lsb_release -cs) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

echo "[INFO] Installing Docker..."
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io

echo "[INFO] Adding current user to docker group..."
sudo usermod -aG docker $USER

# Optional image pull
if [ "$PULL_IMAGE" = true ]; then
    echo "[INFO] Pulling public Surfari image..."
    DOCKERHUB_USER="yzhangondocker"
    IMAGE_NAME="surfari-debian-kasm:latest"
    sudo docker pull docker.io/$DOCKERHUB_USER/$IMAGE_NAME
    echo "[INFO] Done. Image is now available locally."
else
    echo "[INFO] Skipping image pull (no --pull-image flag provided)."
fi
