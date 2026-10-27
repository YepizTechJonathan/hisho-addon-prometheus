#!/usr/bin/env bash
# Puts node_exporter on the machine it runs on, or takes it off. Core pipes this over SSH:
#   bash -s -- install <version> <sha256> <port>      bash -s -- remove
# A user unit (no root), bound to the machine's tailnet address only, so only the tailnet
# (Prometheus on the Core machine) can read it. Binaries stay in ~/.local/share/hisho/node_exporter.
set -euo pipefail
d=~/.local/share/hisho/node_exporter
unit=~/.config/systemd/user/hisho-node-exporter.service
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
if [ "${1:-}" = remove ]; then
  systemctl --user disable --now hisho-node-exporter.service 2>/dev/null || true
  rm -f "$unit"
  systemctl --user daemon-reload
  echo "node_exporter removed (binaries kept in $d)"
  exit 0
fi
[ "${1:-}" = install ] || { echo "usage: install <version> <sha256> <port> | remove" >&2; exit 2; }
v=$2 sha=$3 port=$4
[ "$(uname -m)" = x86_64 ] || { echo "only x86_64 is supported" >&2; exit 1; }
command -v tailscale >/dev/null || { echo "tailscale is not installed here" >&2; exit 1; }
mkdir -p "$d" "$(dirname "$unit")"
if [ ! -x "$d/node_exporter-$v" ]; then
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -m 120 -o "$tmp/ne.tgz" \
    "https://github.com/prometheus/node_exporter/releases/download/v$v/node_exporter-$v.linux-amd64.tar.gz"
  echo "$sha  $tmp/ne.tgz" | sha256sum -c --quiet
  tar -xzf "$tmp/ne.tgz" -C "$tmp"
  install -m 755 "$tmp/node_exporter-$v.linux-amd64/node_exporter" "$d/node_exporter-$v"
fi
ln -sfn "node_exporter-$v" "$d/node_exporter"
cat > "$d/run.sh" <<EOF
#!/bin/sh
# Bind to this machine's tailnet address only (waits for Tailscale after a boot).
ip=\$(tailscale ip -4 2>/dev/null | head -n1)
[ -n "\$ip" ] || { echo "no tailnet address yet" >&2; exit 1; }
exec "$d/node_exporter" --web.listen-address="\$ip:$port" --no-collector.wifi --collector.processes --collector.tcpstat --collector.interrupts --collector.systemd
EOF
chmod 755 "$d/run.sh"
cat > "$unit" <<EOF
[Unit]
Description=node_exporter (Hisho Prometheus add-on; tailnet only)

[Service]
ExecStart=$d/run.sh
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable hisho-node-exporter.service >/dev/null
systemctl --user restart hisho-node-exporter.service
sleep 2
ip=$(tailscale ip -4 | head -n1)
curl -s -m 5 -o /dev/null -w "node_exporter on $ip:$port: %{http_code}\n" "http://$ip:$port/metrics"
