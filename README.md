# Prometheus (Hisho add-on)

## What it does

Prometheus on the Core machine keeps live numbers and a 30-day history of CPU, RAM, disk and
temperature for every fleet machine, and its alerts reach Hisho. Each fleet machine runs
node_exporter (a user unit, bound to its tailnet address only). The machines to scrape come from
Fleet.yaml: Core rewrites Prometheus's target file on every fleet check, and puts node_exporter on
a machine that is on but has none (a newly adopted one; a parked reserve GPU worker is woken the
way a job wakes it). A machine that is off shows grey, never as an alert. The page can link to
your Grafana and its dashboard.

## What it needs

- You need:
  - Nothing beyond Hisho: it runs Prometheus on the Core machine and node_exporter on each fleet machine
  - Optional: a Grafana, to link its dashboards
- Runs on: the Core machine
- Commands on that machine: `curl`, `tailscale`, `sha256sum`
- Settings in `~/Hisho/System/Config/Addons/prometheus.yaml`:
  - `version` (str, default `3.13.4`)
  - `sha256` (str, default `87f21a66f96c597a189cef8d640e8921b621fc17a9feff707c332c4f3b3ddd56`)
  - `exporter_version` (str, default `1.12.1`)
  - `exporter_sha256` (str, default `b51d8a76aa2a9156a55d501aca6276fae09e262259a5e4e831d2c2222f084e63`)
  - `port` (port, default `9090`)
  - `https_port` (port, default `9443`)
  - `exporter_port` (port, default `9100`)
  - `retention` (str, default `30d`)
  - `auto_install` (bool, default `True`)
  - `site_url` (url, required)
  - `grafana_url` (url, optional)
  - `grafana_dashboard` (str, optional)
  - `grafana_api` (url, optional)
  - `grafana_token` (secret name, optional)
  - `lan_address` (str, optional)

## How to install

On the dashboard: **Add-ons > Store > Prometheus > Install**. Or from a terminal:

```bash
hisho addon install prometheus
```

Hisho takes the newest version from this repository and copies it to the machine it runs on, runs its install and turns it on. Updates show as **Update
available** on its Store card; **Roll back** returns to the version before.

## How to use

Its card shows any machine that is hot, full or down. Its page (**Add-ons > Prometheus**) has live CPU, RAM, disk and temperature for every machine, with 30 days of history, and links to Grafana if you set one. A machine that is off shows grey, never as an alert.

## Settings

In `~/Hisho/System/Config/Addons/prometheus.yaml` or on its page: `site_url` (its tailnet address,
e.g. `https://<core>.<tailnet>.ts.net:9443`), and if you use Grafana, `grafana_url`,
`grafana_dashboard`, `grafana_api` and `grafana_token` (a Viewer service-account token's secret
name). `lan_address` also opens Prometheus on that LAN address for Grafana; Prometheus has no
login, so the whole LAN can read it. Versions are pinned with their sha256.

## How to remove

**Add-ons > Store > Prometheus > Remove**, or `hisho addon remove prometheus`. Stops Prometheus and keeps its data. Each machine's node_exporter has a Remove button on the
Prometheus page. Your settings stay in `~/Hisho/System/Config/Addons/prometheus.yaml`.

## License

Source-available under the [PolyForm Shield License 1.0.0](LICENSE.md): free to use, change and share,
for any purpose except offering a product that competes with Hisho (reselling it, for example).
