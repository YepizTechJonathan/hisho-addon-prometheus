#!/usr/bin/env bash
# Runs on the Core machine (in this folder) during `hisho addon install prometheus`.
# Downloads the pinned Prometheus release (checked against its sha256), writes its config and a
# user unit, starts it on 127.0.0.1 and publishes it on the tailnet. Scrape targets are written
# by Core from Fleet.yaml (targets/fleet.json); node_exporter goes on each machine from Core too.
set -euo pipefail
d=~/.local/share/hisho/prometheus
v=${HISHO_OPT_VERSION}
[ "$(uname -m)" = x86_64 ] || { echo "Prometheus add-on: only x86_64 is supported here" >&2; exit 1; }
mkdir -p "$d/data" "$d/targets" ~/.config/systemd/user
if [ ! -x "$d/prometheus-$v" ]; then
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -m 300 -o "$tmp/p.tgz" \
    "https://github.com/prometheus/prometheus/releases/download/v$v/prometheus-$v.linux-amd64.tar.gz"
  echo "${HISHO_OPT_SHA256}  $tmp/p.tgz" | sha256sum -c --quiet
  tar -xzf "$tmp/p.tgz" -C "$tmp"
  install -m 755 "$tmp/prometheus-$v.linux-amd64/prometheus" "$d/prometheus-$v"
  install -m 755 "$tmp/prometheus-$v.linux-amd64/promtool" "$d/promtool-$v"
fi
ln -sfn "prometheus-$v" "$d/prometheus"
ln -sfn "promtool-$v" "$d/promtool"
sed "s/@PORT@/${HISHO_OPT_PORT}/g" prometheus.yml > "$d/prometheus.yml"
install -m 644 rules.yml "$d/rules.yml"
[ -f "$d/targets/fleet.json" ] || echo "[]" > "$d/targets/fleet.json"
"$d/promtool" check config "$d/prometheus.yml" >/dev/null
lan=""
if [ -n "${HISHO_OPT_LAN_ADDRESS:-}" ]; then
  ip -4 -o addr show | grep -qw "inet ${HISHO_OPT_LAN_ADDRESS}" \
    || { echo "lan_address ${HISHO_OPT_LAN_ADDRESS} is not an address of this machine" >&2; exit 1; }
  lan=" --web.listen-address=${HISHO_OPT_LAN_ADDRESS}:${HISHO_OPT_PORT}"
fi
sed -e "s/@PORT@/${HISHO_OPT_PORT}/g" -e "s/@RETENTION@/${HISHO_OPT_RETENTION}/g" -e "s/@LAN@/${lan}/" \
  units/hisho-prometheus.service > ~/.config/systemd/user/hisho-prometheus.service
systemctl --user daemon-reload
systemctl --user enable hisho-prometheus.service >/dev/null
systemctl --user restart hisho-prometheus.service
for i in $(seq 1 30); do
  curl -s -m 3 -o /dev/null "http://127.0.0.1:${HISHO_OPT_PORT}/-/ready" && break
  sleep 2
done
curl -s -m 5 -o /dev/null -w "prometheus local: %{http_code}\n" "http://127.0.0.1:${HISHO_OPT_PORT}/-/ready"
# The owner is Tailscale's operator on a Souko (no sudo); elsewhere it publishes with sudo.
tailscale serve --bg --https="${HISHO_OPT_HTTPS_PORT}" "http://127.0.0.1:${HISHO_OPT_PORT}" >/dev/null 2>&1 \
  || sudo -n tailscale serve --bg --https="${HISHO_OPT_HTTPS_PORT}" "http://127.0.0.1:${HISHO_OPT_PORT}" >/dev/null
echo "published: ${HISHO_OPT_SITE_URL} (tailnet only)"
