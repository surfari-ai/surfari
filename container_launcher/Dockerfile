FROM debian:bookworm

ENV DEBIAN_FRONTEND=noninteractive

# Install base dependencies
RUN apt update && apt install -y \
    sudo curl wget unzip vim gnupg \
    xfce4 xterm dbus-x11 \
    libgtk-3-0 libnss3 libxss1 libasound2 libxcomposite1 libxdamage1 \
    libxrandr2 libgbm1 libxshmfence1 libatk-bridge2.0-0 libx11-xcb1 \
    chromium \
    python3 python3-pip \
    xvfb fonts-dejavu \
    nodejs npm \
    expect sqlite3 \
    && apt clean && rm -rf /var/lib/apt/lists/*

# Install KasmVNC (Debian-compatible build)
RUN wget https://github.com/kasmtech/KasmVNC/releases/download/v1.3.4/kasmvncserver_bookworm_1.3.4_amd64.deb && \
    dpkg -i kasmvncserver_bookworm_1.3.4_amd64.deb || true && \
    apt update && apt install -f -y && \
    rm kasmvncserver_bookworm_1.3.4_amd64.deb

# Create user surfari with password surfari1!
RUN useradd -m -s /bin/bash surfari && \
    echo "surfari:surfari1!" | chpasswd && \
    usermod -aG ssl-cert surfari

# Add custom startup script
COPY startup.sh /usr/local/bin/startup.sh
COPY index.html /usr/share/kasmvnc/www/index.html
RUN chmod +x /usr/local/bin/startup.sh

USER surfari
# Create .vnc directory and copy xstartup for surfari
RUN mkdir -p /home/surfari/.vnc
COPY --chown=surfari:surfari xstartup /home/surfari/.vnc/xstartup
RUN chmod +x /home/surfari/.vnc/xstartup

# ------------------------------
# Pass version at build-time
# ------------------------------
ARG SURFARI_VERSION=0.1.5
ENV SURFARI_VERSION=${SURFARI_VERSION}

# Copy the zip archive into the image
COPY --chown=surfari:surfari surfari-${SURFARI_VERSION}-linux.zip /home/surfari/

# Unzip it in the container
RUN set -eux && \
    unzip -q /home/surfari/surfari-${SURFARI_VERSION}-linux.zip -d /home/surfari && \
    rm /home/surfari/surfari-${SURFARI_VERSION}-linux.zip

# Expose ports for HTTP (6901) and WebRTC (8444)
EXPOSE 6901 8444

ENTRYPOINT ["/usr/local/bin/startup.sh"]
