#!/bin/bash
set -e  # Exit on error
set -x  # Print commands before execution

cd ..
rm -rf build dist
# check if installers/surfari-linux-dist.zip exists
if [ ! -f installers/surfari-linux-dist.zip ]; then
  echo "Info: installers/surfari-linux-dist.zip does not exist. Build it first."
  bash installers/build_and_package.sh
  rm -rf build dist
fi

cp installers/surfari-linux-dist.zip container_launcher/surfari-linux-dist.zip

cd container_launcher

docker rm -f $(docker ps -aq) || true
docker rmi -f $(docker images -q) || true
docker builder prune -af

docker build -t surfari-debian-kasm .
rm surfari-linux-dist.zip
