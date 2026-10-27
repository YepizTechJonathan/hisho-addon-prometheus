"""Prometheus integration for Hisho (see addon.yaml). Runs inside Core, on the machine Prometheus
runs on. Fleet.yaml is the only list of machines: every check rewrites Prometheus's target file
from it and puts node_exporter on a machine that is on but has none. "Off" comes from Hisho's own
heartbeats, so a machine that is simply off (Souko away, Ren parked) is grey, never an alert."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

EXPORTER_SCRIPT = Path(__file__).resolve().parent / "bin" / "node-exporter.sh"
DATA = Path("~/.local/share/hisho/prometheus").expanduser()
FRESH = 150            # a heartbeat younger than this means the machine is on (agents report ~every minute;
                       # shorter keeps a machine that is shutting down from paging "not sending")
RETRY = 3600           # try an automatic node_exporter install on a machine at most this often
_tried: dict[str, float] = {}
_woke: dict[str, float] = {}
_lock = threading.Lock()   # installs run in threads; exporters.json is read-modify-write
NAMES = {"DiskAlmostFull": "{mountpoint} almost full", "LowMemory": "low on memory",
         "HighTemperature": "CPU running hot", "SSDHot": "SSD running hot"}
REAL_FS = 'fstype!~"tmpfs|overlay|squashfs|ramfs|devtmpfs|efivarfs|fuse.*"'
QUERIES = {
    "cpu": '100 * (1 - avg by (machine) (rate(node_cpu_seconds_total{job="node",mode="idle"}[2m])))',
    "ram": '100 * (1 - node_memory_MemAvailable_bytes{job="node"} / node_memory_MemTotal_bytes{job="node"})',
    # the CPU's own sensor: the hottest sensor overall is often a NIC or SSD that runs hot by design
    "temp": 'max by (machine) (node_hwmon_temp_celsius{job="node"} * on (machine, chip) group_left (chip_name) '
            'node_hwmon_chip_names{chip_name=~"k10temp|coretemp|zenpower|cpu_thermal"})',
    "disk": f'100 * (1 - node_filesystem_avail_bytes{{job="node",{REAL_FS}}} '
            f'/ node_filesystem_size_bytes{{job="node",{REAL_FS}}})',
}


# ── targets from the fleet ───────────────────────────────────────────────

def address(fleet: Any, machine: str) -> str:
    """Where Prometheus reaches a machine: the tailnet address in Fleet.yaml ssh, else its name."""
    ssh = (getattr(fleet, "ssh", None) or {}).get(machine) or ""
    return ssh.rpartition("@")[2] or machine


def targets(fleet: Any, port: int) -> list[dict[str, Any]]:
    return [{"targets": [f"{address(fleet, m)}:{port}"], "labels": {"machine": m}} for m in fleet.machines]


def write_targets(fleet: Any, port: int, directory: Path | None = None) -> bool:
    """Rewrite targets/fleet.json if the fleet changed. Prometheus picks it up within a minute."""
    directory = directory or DATA / "targets"
    if not directory.is_dir():
        return False
    path = directory / "fleet.json"
    text = json.dumps(targets(fleet, port), indent=1) + "\n"
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


# ── reading Prometheus and Hisho ──────────────────────────────────────────

def _local(ctx: Any) -> bool:
    from hisho import addons
    return addons.is_local(addons.host_of(ctx.addon))


def base_url(ctx: Any) -> str:
    if _local(ctx):
        return f"http://127.0.0.1:{ctx.options.get('port') or 9090}"
    return str(ctx.options.get("site_url") or "").rstrip("/")


def _get(ctx: Any, path: str, **params: str) -> Any:
    url = base_url(ctx) + path + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(url, timeout=8) as resp:
        body = json.loads(resp.read().decode())
    if body.get("status") != "success":
        raise RuntimeError(body.get("error") or "Prometheus said no")
    return body["data"]


def read(ctx: Any, metrics: bool = False) -> dict[str, Any]:
    out = {"targets": _get(ctx, "/api/v1/targets", state="active").get("activeTargets") or [],
           "alerts": _get(ctx, "/api/v1/alerts").get("alerts") or []}
    if metrics:
        out["build"] = _get(ctx, "/api/v1/status/buildinfo")
        out["metrics"] = {k: _get(ctx, "/api/v1/query", query=q).get("result") or [] for k, q in QUERIES.items()}
    return out


def heartbeat_ages(ctx: Any) -> dict[str, float]:
    """Seconds since each machine's agent last reported (read where Core keeps them)."""
    from hisho import addons
    if addons.is_local(ctx.fleet.core_host):
        from hisho.agent import HeartbeatStore
        store = HeartbeatStore()
        return {name: age for name, hb in store.read_all().items() if (age := store.age_seconds(hb)) is not None}
    with urllib.request.urlopen(ctx.fleet.core_url + "/api/agent/heartbeats", timeout=8) as resp:
        body = json.loads(resp.read().decode())
    return {str(h.get("canonical")): float(h["heartbeat_age_seconds"]) for h in body.get("heartbeats") or []
            if h.get("canonical") and h.get("heartbeat_age_seconds") is not None}


def held() -> tuple[bool, set[str]]:
    from hisho import maintenance
    return maintenance.alerts_held(Path(os.environ.get("HISHO_ROOT") or "~/Hisho").expanduser())


# ── what it means ─────────────────────────────────────────────────────────

def health(target_list: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str((t.get("labels") or {}).get("machine")): t for t in target_list
            if t.get("scrapePool") == "node" or (t.get("labels") or {}).get("job") == "node"}


def states(machines: list[str], target_health: dict[str, dict[str, Any]], ages: dict[str, float],
           hold_all: bool, held_machines: set[str]) -> dict[str, str]:
    """up: Prometheus reads it. held: in maintenance. off: no fresh heartbeat (normal, grey).
    new: on, not scraped yet. missing: on, but node_exporter doesn't answer (the only state that
    needs anything)."""
    out = {}
    for m in machines:
        if (target_health.get(m) or {}).get("health") == "up":
            out[m] = "up"
        elif hold_all or m in held_machines:
            out[m] = "held"
        elif ages.get(m) is None or ages[m] > FRESH:
            out[m] = "off"
        elif (target_health.get(m) or {}).get("health") != "down":
            out[m] = "new"      # not scraped yet (just added to the target file): wait, don't page
        else:
            out[m] = "missing"
    return out


def alert_title(a: dict[str, Any]) -> str:
    labels = a.get("labels") or {}
    name = str(labels.get("alertname") or "alert")
    what = NAMES.get(name, name).format(mountpoint=labels.get("mountpoint", "a disk"))
    return f"{labels.get('machine') or 'fleet'}: {what}"


def firing(alerts: list[dict[str, Any]], hold_all: bool, held_machines: set[str]) -> list[dict[str, Any]]:
    return [a for a in alerts if a.get("state") == "firing" and not hold_all
            and (a.get("labels") or {}).get("machine") not in held_machines]


def findings(st: dict[str, str], alerts: list[dict[str, Any]], auto: bool) -> list[dict[str, Any]]:
    out = []
    for m, s in st.items():
        if s == "missing":
            out.append({"entry": m, "severity": "warn", "title": f"{m} isn't sending its numbers",
                        "detail": f"{m} is on but Prometheus can't read its node_exporter. "
                                  + ("Hisho reinstalls it within the hour; or press Install on the Prometheus page."
                                     if auto else "Press Install on the Prometheus page.")})
    for a in alerts:
        out.append({"entry": (a.get("labels") or {}).get("machine") or "", "severity": "warn",
                    "title": alert_title(a), "detail": str((a.get("annotations") or {}).get("summary") or "")})
    return out


# ── node_exporter on a machine ───────────────────────────────────────────

def install_exporter(ctx: Any, machine: str, action: str = "install") -> dict[str, Any]:
    from hisho import addons
    o = ctx.options
    args = (f"install {o['exporter_version']} {o['exporter_sha256']} {int(o.get('exporter_port') or 9100)}"
            if action == "install" else "remove")
    try:
        res = addons._run(addons.on_host(machine, f"bash -s -- {args}"), 240, EXPORTER_SCRIPT.read_text())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__} on {machine}"}
    if res.returncode:
        return {"ok": False, "error": (res.stderr.strip() or res.stdout.strip())[-300:] or f"exit {res.returncode}"}
    if action == "install":
        note_installed({machine: config_id(o)}, replace=True)
    return {"ok": True, "message": res.stdout.strip()[-200:] or "done"}


def config_id(o: dict[str, Any]) -> str:
    """What a machine's node_exporter should be: version, port and this install script (its flags).
    A machine holding another id is reinstalled (woken first if it is the parked reserve worker)."""
    import hashlib
    script = hashlib.sha256(EXPORTER_SCRIPT.read_bytes()).hexdigest()[:10]
    return f"{o.get('exporter_version')}-{int(o.get('exporter_port') or 9100)}-{script}"


def installed(path: Path | None = None) -> dict[str, str]:
    """Machine -> config_id of its node_exporter as installed from here ("seen" if only seen answering)."""
    try:
        return dict(json.loads((path or DATA / "exporters.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def note_installed(values: dict[str, str], path: Path | None = None, replace: bool = False) -> None:
    path = path or DATA / "exporters.json"
    if not path.parent.is_dir():
        return
    with _lock:
        known = installed(path)
        new = {**known, **{m: v if replace else known.get(m) or v for m, v in values.items()}}
        if new != known:
            tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(new, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(path)


def outdated(st: dict[str, str], known: dict[str, str], want: str) -> list[str]:
    """Machines that are on and answering but run another node_exporter version or flags."""
    return [m for m, s in st.items() if s == "up" and known.get(m) != want]


def to_wake(st: dict[str, str], gpu_worker: str | None, known: dict[str, str], want: str) -> str | None:
    """The parked reserve worker, if its node_exporter is missing or outdated: wake it like a job does,
    so it gets the current one and the idle sweep parks it again afterwards. Others that are off wait."""
    if gpu_worker and st.get(gpu_worker) == "off" and known.get(gpu_worker) != want:
        return gpu_worker
    return None


def wake(machine: str) -> dict[str, Any]:
    """Power the machine on through Hisho's one wake path (jobs use it too); the idle sweep parks it
    again once it's idle. Journaled by the power controller."""
    from hisho.power import wake_worker
    return wake_worker(Path(os.environ.get("HISHO_ROOT") or "~/Hisho").expanduser(), machine, requester="prometheus")


def _auto_install(ctx: Any, machines: list[str]) -> None:
    now = time.time()
    for m in machines:
        if now - _tried.get(m, 0) < RETRY:
            continue
        _tried[m] = now
        threading.Thread(target=install_exporter, args=(ctx, m), daemon=True,
                         name=f"node-exporter-{m}").start()


# ── the provider functions ───────────────────────────────────────────────

def summary(entries: dict[str, dict[str, Any]], ctx: Any) -> dict[str, Any]:
    machines = list(ctx.fleet.machines)
    if _local(ctx):
        write_targets(ctx.fleet, int(ctx.options.get("exporter_port") or 9100))
    try:
        d = read(ctx)
    except Exception as exc:  # noqa: BLE001
        return {"state": "down", "headline": "Prometheus isn't answering",
                "problems": [{"entry": "", "severity": "critical", "title": "Prometheus isn't answering",
                              "detail": f"{str(exc)[:160]}. No machine numbers or history are being kept. "
                                        "Restart it on the Prometheus card."}]}
    hold_all, held_machines = held()
    try:
        ages = heartbeat_ages(ctx)
    except Exception:  # noqa: BLE001 -- unknown means "not known on": never page about it
        ages = {}
    st = states(machines, health(d["targets"]), ages, hold_all, held_machines)
    auto = bool(ctx.options.get("auto_install", True))
    missing = [m for m, s in st.items() if s == "missing"]
    if _local(ctx):
        want = config_id(ctx.options)
        note_installed({m: "seen" for m, s in st.items() if s == "up"})
        known = installed()
        if auto and (missing or outdated(st, known, want)):
            _auto_install(ctx, missing + outdated(st, known, want))
        sleeper = to_wake(st, ctx.fleet.gpu_worker, known, want) if auto else None
        if sleeper and time.time() - _woke.get(sleeper, 0) > RETRY:
            _woke[sleeper] = time.time()
            threading.Thread(target=wake, args=(sleeper,), daemon=True, name=f"wake-{sleeper}").start()
    fire = firing(d["alerts"], hold_all, held_machines)
    problems = findings(st, fire, auto)
    up, off = sum(s == "up" for s in st.values()), sum(s in ("off", "held", "new") for s in st.values())
    headline = (f"{up} of {len(machines)} machines reporting" + (f" · {off} off" if off else "")
                + (f" · {len(fire)} alert{'s' * (len(fire) != 1)} firing" if fire else " · no alerts"))
    return {"state": "warn" if problems else "ok", "headline": headline, "problems": problems}


def _by_machine(rows: list[dict[str, Any]]) -> dict[str, float]:
    return {str(r["metric"].get("machine")): float(r["value"][1]) for r in rows if r.get("metric", {}).get("machine")}


def disks_of(m: str, rows: list[dict[str, Any]]) -> list[tuple[str, float]]:
    """(mountpoint, % used) once per device (btrfs subvolumes share one), shortest mountpoint first."""
    seen: dict[str, tuple[str, float]] = {}
    for r in rows:
        mt, val = r.get("metric") or {}, float(r["value"][1])
        if mt.get("machine") != m or val != val:   # NaN: nothing to measure
            continue
        dev, mp = str(mt.get("device") or mt.get("mountpoint")), str(mt.get("mountpoint"))
        if dev not in seen or len(mp) < len(seen[dev][0]):
            seen[dev] = (mp, val)
    return sorted(seen.values())


def machine_facts(m: str, metrics: dict[str, list[dict[str, Any]]]) -> list[str]:
    cpu, ram, temp = (_by_machine(metrics.get(k) or []).get(m) for k in ("cpu", "ram", "temp"))
    disks = {str(r["metric"].get("mountpoint")): float(r["value"][1])
             for r in metrics.get("disk") or [] if r["metric"].get("machine") == m}
    out = []
    if cpu is not None:
        out.append(f"CPU {cpu:.0f}% · RAM {ram or 0:.0f}%" + (f" · CPU {temp:.0f}°C" if temp is not None else " · no temperature sensor"))
    if "/" in disks:
        out.append(f"disk / {disks['/']:.0f}% used")
    return out


def details(entries: dict[str, dict[str, Any]], ctx: Any) -> list[dict[str, Any]]:
    o = ctx.options
    try:
        d = read(ctx, metrics=True)
    except Exception as exc:  # noqa: BLE001
        return [{"title": "Prometheus", "entry": "", "state": "down", "facts": [str(exc)[:200]], "items": []}]
    hold_all, held_machines = held()
    try:
        ages = heartbeat_ages(ctx)
    except Exception:  # noqa: BLE001
        ages = {}
    th = health(d["targets"])
    st = states(list(ctx.fleet.machines), th, ages, hold_all, held_machines)
    fire = firing(d["alerts"], hold_all, held_machines)
    items = [{"id": "open", "name": "Prometheus", "state": "up", "url": o.get("site_url"),
              "facts": [f"version {d['build'].get('version')}", f"keeps {o.get('retention')} of history"],
              "actions": []}]
    if o.get("grafana_url"):
        items.append({"id": "grafana", "name": "Grafana", "state": "up", "url": o["grafana_url"],
                      "facts": [f"Dashboards → {o['grafana_dashboard']} (pick a machine at the top)"
                                if o.get("grafana_dashboard") else "your dashboards"], "actions": []})
    items += grafana_dashboards(ctx)
    items += [{"id": f"alert-{i}", "name": alert_title(a), "state": "warn",
               "facts": [str((a.get("annotations") or {}).get("summary") or "")], "actions": []}
              for i, a in enumerate(fire)]
    out = [{"title": "Prometheus", "entry": "", "state": "warn" if fire else "ok",
            "facts": [f"{sum(s == 'up' for s in st.values())} of {len(st)} machines reporting · "
                      f"{len(fire)} alert{'s' * (len(fire) != 1)} firing"], "items": items}]
    metrics = d["metrics"]
    for m, s in st.items():
        facts = machine_facts(m, metrics) if s == "up" else [
            {"off": "off (no heartbeat), so nothing to read; normal", "held": "in maintenance",
             "new": "waiting for Prometheus to read it the first time",
             "missing": "on, but its node_exporter doesn't answer"}[s]]
        disks = disks_of(m, metrics.get("disk") or [])
        err = (th.get(m) or {}).get("lastError")
        exporter = {"id": "exporter", "name": "node_exporter", "state": {"up": "up", "missing": "down"}.get(s, "off"),
                    "facts": [f"{address(ctx.fleet, m)}:{o.get('exporter_port')}"] + ([err[:160]] if err and s == "missing" else []),
                    "actions": [{"id": "install", "label": "Reinstall" if s == "up" else "Install",
                                 "confirm": f"Install node_exporter {o.get('exporter_version')} on {m}? (user unit, tailnet only)"},
                                {"id": "remove", "label": "Remove", "danger": True,
                                 "confirm": f"Stop and remove node_exporter on {m}? Prometheus stops getting its numbers. "
                                            + ("Hisho puts it back within the hour while auto_install is on." if o.get("auto_install", True) else "")}]}
        disk_item = [{"id": "disks", "name": "Disks", "state": "up" if all(p < 90 for _, p in disks) else "warn",
                      "facts": [f"{mp}: {p:.0f}% used" for mp, p in disks], "actions": []}] if disks else []
        out.append({"title": m, "entry": m, "state": {"up": "ok", "missing": "warn"}.get(s, "off"),
                    "facts": facts, "items": disk_item + [exporter]})
    return out


def grafana_dashboards(ctx: Any) -> list[dict[str, Any]]:
    """Grafana's dashboards as items (needs grafana_api and a Viewer token's secret name)."""
    o = ctx.options
    if not (o.get("grafana_api") and o.get("grafana_token")):
        return []
    api = str(o["grafana_api"]).rstrip("/")
    try:
        req = urllib.request.Request(api + "/api/search?type=dash-db", headers={
            "Authorization": "Bearer " + ctx.secret(str(o["grafana_token"])), "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            found = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        return [{"id": "grafana-dashboards", "name": "Grafana dashboards", "state": "down",
                 "facts": [f"can't list them: {type(exc).__name__}: {str(exc)[:120]}"], "actions": []}]
    return [{"id": f"dash-{d.get('uid')}", "name": f"Grafana: {d.get('title')}", "state": "up",
             "url": api + str(d.get("url") or ""), "facts": [str(d.get("folderTitle") or "dashboard")], "actions": []}
            for d in found[:20]]


def test(entry: dict[str, Any], ctx: Any) -> dict[str, Any]:
    try:
        d = read(ctx)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
    up = sum(t.get("health") == "up" for t in health(d["targets"]).values())
    return {"ok": True, "message": f"Connected: {up} machine(s) reporting"}


def act(entries: dict[str, dict[str, Any]], ctx: Any, entry: str, item: str, action: str) -> dict[str, Any]:
    if entry not in ctx.fleet.machines:
        return {"ok": False, "error": f"{entry} is not a fleet machine"}
    if item == "exporter" and action in ("install", "remove"):
        _tried[entry] = time.time()
        return install_exporter(ctx, entry, action)
    return {"ok": False, "error": f"unknown action {action}"}


def adopted(entries: dict[str, dict[str, Any]], ctx: Any, machine: str) -> dict[str, Any]:
    """Called by `hisho adopt` once a new machine is in the fleet."""
    return install_exporter(ctx, machine)
