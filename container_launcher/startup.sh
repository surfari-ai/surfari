#!/bin/bash

set -e  # exit on any error

VNC_DIR="/home/surfari/.vnc"
CONFIG="$VNC_DIR/kasmvnc.yaml"

# Kill orphaned X servers or leftover locks
rm -f /tmp/.X*-lock /tmp/.X11-unix/X*

# Prevent double-start of VNC
if pgrep Xvnc > /dev/null; then
  echo "[INFO] Xvnc already running" >> /tmp/vnc.log
else
  if [ ! -f "$CONFIG" ]; then
    echo "[INFO] First-time VNC setup for surfari" >> /tmp/vnc.log

    # Create expect script on-the-fly
    cat <<EOF > /tmp/vnc_setup.exp
spawn vncserver
expect "Provide selection number:"
send "1\r"
expect "Enter username (default: surfari):"
send "\r"
expect "Password:"
send "surfari1!\r"
expect "Verify:"
send "surfari1!\r"
expect "Please choose Desktop Environment to run:"
send "2\r"
expect eof
EOF

    # Run expect directly (already running as surfari)
    expect /tmp/vnc_setup.exp
    rm /tmp/vnc_setup.exp

  else
    echo "[INFO] VNC already configured for surfari" >> /tmp/vnc.log
  fi

  vncserver -disableBasicAuth
  /home/surfari/navigation_cli/navigation_cli "$@"
fi

# Wait for log file to exist before tailing
while [ ! -f "$VNC_DIR"/*.log ]; do sleep 1; done
tail -F $VNC_DIR/*.log