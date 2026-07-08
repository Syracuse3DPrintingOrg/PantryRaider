FROM python:3.12-slim

WORKDIR /app

# rclone powers the optional "push backup to a cloud remote" feature
# (Settings > Security > Backup). ca-certificates is needed for outbound HTTPS.
# Touch calibration reads the panel's /dev/input device directly in Python (the
# appliance compose mounts /dev/input in), so no evtest binary is needed here.
#
# wireguard-tools (wg, wg-quick) plus iproute2 and iptables let a plain server
# run Forager remote access INSIDE this container (a Pi appliance does it on the
# host via the bridge instead). The kernel WireGuard module comes from the host
# and is present on virtually all modern Linux, including Unraid; this only adds
# the userspace tools. In-container WireGuard also needs NET_ADMIN and
# /dev/net/tun, which the shipped compose grants (they are inert otherwise).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        rclone ca-certificates wireguard-tools iproute2 iptables \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN mkdir -p /app/data

# Production default: no --reload, code is baked into the image. The dev
# docker-compose.yml overrides this command to add --reload alongside a
# volume mount of ./service/app so `git pull` applies changes live.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
