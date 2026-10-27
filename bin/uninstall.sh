#!/usr/bin/env bash
# Stops Prometheus and removes its unit and tailnet address. Keeps its history and binaries
# (~/.local/share/hisho/prometheus): deleting those is a separate, deliberate step.
# node_exporter on each machine is removed from the Prometheus page (Remove), or by hand:
#   systemctl --user disable --now hisho-node-exporter; rm ~/.config/systemd/user/hisho-node-exporter.service
set -euo pipefail
sudo -n tailscale serve --https="${HISHO_OPT_HTTPS_PORT}" off >/dev/null 2>&1 || true
systemctl --user disable --now hisho-prometheus.service || true
rm -f ~/.config/systemd/user/hisho-prometheus.service
systemctl --user daemon-reload
