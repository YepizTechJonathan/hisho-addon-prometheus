"""Prometheus add-on: targets come from Fleet.yaml, "off" is grey not red, maintenance holds alerts."""
from __future__ import annotations

import json
from importlib import util
from pathlib import Path
from types import SimpleNamespace

from hisho import addons

spec = util.spec_from_file_location("prometheus_provider", Path(__file__).resolve().parents[1] / "provider.py")
pm = util.module_from_spec(spec)
spec.loader.exec_module(pm)

FLEET = SimpleNamespace(machines=("souko", "kihon", "hashira-1", "hashira-2", "newbox"),
                        ssh={"souko": "user@100.64.0.22", "kihon": "user@100.64.0.20"},
                        core_host="kihon", core_url="http://kihon:8787", gpu_worker="hashira-2")


def _t(machine, health):
    return {"scrapePool": "node", "labels": {"machine": machine, "job": "node"}, "health": health,
            "lastError": "" if health == "up" else "connection refused"}


def test_the_manifest_is_valid(addon):
    assert addons.validate_manifest(addon.manifest) == []
    assert addon.kind == "integration" and addon.services()[0]["unit"] == "hisho-prometheus.service"


def test_targets_are_every_fleet_machine_by_tailnet_address_or_name():
    t = pm.targets(FLEET, 9100)
    assert [x["labels"]["machine"] for x in t] == list(FLEET.machines)
    assert t[0]["targets"] == ["100.64.0.22:9100"]
    assert t[-1]["targets"] == ["newbox:9100"]   # a machine with no ssh entry yet is reached by name


def test_write_targets_only_when_installed_and_only_when_changed(tmp_path):
    assert pm.write_targets(FLEET, 9100, tmp_path / "absent") is False
    assert pm.write_targets(FLEET, 9100, tmp_path) is True
    assert pm.write_targets(FLEET, 9100, tmp_path) is False
    assert json.loads((tmp_path / "fleet.json").read_text())[1]["labels"] == {"machine": "kihon"}


def test_off_machines_are_grey_and_only_on_but_silent_ones_need_anything():
    th = pm.health([_t("souko", "down"), _t("kihon", "up"), _t("hashira-1", "down"), _t("hashira-2", "down")])
    ages = {"souko": 9000, "kihon": 10, "hashira-1": 20}       # Souko away; Ren parked (no heartbeat)
    st = pm.states(list(FLEET.machines), th, ages, False, set())
    assert st == {"souko": "off", "kihon": "up", "hashira-1": "missing", "hashira-2": "off", "newbox": "off"}
    assert pm.states(["newbox"], th, {"newbox": 5}, False, set()) == {"newbox": "new"}   # not scraped yet
    problems = pm.findings(st, [], True)
    assert [p["entry"] for p in problems] == ["hashira-1"]
    assert problems[0]["severity"] == "warn"


def test_maintenance_holds_a_machine_and_its_alerts():
    th = pm.health([_t("hashira-1", "down")])
    st = pm.states(["hashira-1"], th, {"hashira-1": 5}, False, {"hashira-1"})
    assert st == {"hashira-1": "held"} and pm.findings(st, [], True) == []
    alerts = [{"state": "firing", "labels": {"alertname": "DiskAlmostFull", "machine": "hashira-1", "mountpoint": "/"},
               "annotations": {"summary": "hashira-1: / is 95% full"}},
              {"state": "firing", "labels": {"alertname": "HighTemperature", "machine": "souko"}},
              {"state": "pending", "labels": {"alertname": "LowMemory", "machine": "kihon"}}]
    fire = pm.firing(alerts, False, {"hashira-1"})
    assert [pm.alert_title(a) for a in fire] == ["souko: CPU running hot"]
    assert pm.firing(alerts, True, set()) == []
    assert pm.alert_title(alerts[0]) == "hashira-1: / almost full"


def _ctx(tmp_path, monkeypatch, targets, alerts, ages):
    monkeypatch.setattr(pm, "_local", lambda ctx: True)
    monkeypatch.setattr(pm, "DATA", tmp_path)
    monkeypatch.setattr(pm, "held", lambda: (False, set()))
    monkeypatch.setattr(pm, "heartbeat_ages", lambda ctx: ages)
    monkeypatch.setattr(pm, "read", lambda ctx, metrics=False: {
        "targets": targets, "alerts": alerts, "build": {"version": "3.13.4"},
        "metrics": {"cpu": [{"metric": {"machine": "kihon"}, "value": [0, "12.4"]}],
                    "ram": [{"metric": {"machine": "kihon"}, "value": [0, "40"]}],
                    "temp": [],
                    "disk": [{"metric": {"machine": "kihon", "mountpoint": "/"}, "value": [0, "51"]}]}})
    started = []
    monkeypatch.setattr(pm, "install_exporter", lambda ctx, m, action="install": started.append(m) or {"ok": True})
    monkeypatch.setattr(pm, "wake", lambda m: started.append(f"wake {m}"))
    pm._tried.clear()
    pm._woke.clear()
    opts = {"port": 9090, "exporter_port": 9100, "exporter_version": "1.12.1", "exporter_sha256": "x",
            "retention": "30d", "site_url": "https://kihon.example.ts.net:9443", "auto_install": True}
    return SimpleNamespace(fleet=FLEET, options=opts, addon=None), started


def test_summary_counts_off_machines_and_reinstalls_where_missing(tmp_path, monkeypatch):
    (tmp_path / "targets").mkdir()
    ctx, started = _ctx(tmp_path, monkeypatch, [_t("kihon", "up"), _t("hashira-1", "down")], [],
                        {"kihon": 5, "hashira-1": 5})
    out = pm.summary({}, ctx)
    assert out["headline"] == "1 of 5 machines reporting · 3 off · no alerts"
    assert out["state"] == "warn" and out["problems"][0]["entry"] == "hashira-1"
    import time
    time.sleep(0.2)
    # missing on Ten, unknown config on Kihon (only seen answering), parked Ren woken like a job does
    assert sorted(started) == ["hashira-1", "kihon", "wake hashira-2"]
    assert (tmp_path / "targets" / "fleet.json").exists()
    assert pm.installed(tmp_path / "exporters.json") == {"kihon": "seen"}
    pm.summary({}, ctx)
    time.sleep(0.1)
    assert len(started) == 3   # at most once an hour


def test_prometheus_down_is_critical(monkeypatch):
    monkeypatch.setattr(pm, "_local", lambda ctx: False)
    monkeypatch.setattr(pm, "read", lambda ctx, metrics=False: (_ for _ in ()).throw(OSError("refused")))
    out = pm.summary({}, SimpleNamespace(fleet=FLEET, options={}, addon=None))
    assert out["state"] == "down" and out["problems"][0]["severity"] == "critical"


def test_details_show_numbers_for_up_machines_and_off_ones_plainly(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch, [_t("kihon", "up")], [], {"kihon": 5})
    secs = {s["title"]: s for s in pm.details({}, ctx)}
    assert secs["kihon"]["facts"] == ["CPU 12% · RAM 40% · no temperature sensor", "disk / 51% used"]
    assert secs["souko"]["state"] == "off" and "normal" in secs["souko"]["facts"][0]
    assert secs["Prometheus"]["items"][0]["url"] == "https://kihon.example.ts.net:9443"
    ex = secs["souko"]["items"][-1]
    assert ex["id"] == "exporter" and [a["id"] for a in ex["actions"]] == ["install", "remove"]


def test_act_only_for_fleet_machines(monkeypatch):
    ctx = SimpleNamespace(fleet=FLEET, options={})
    assert pm.act({}, ctx, "elsewhere", "exporter", "install")["ok"] is False
    monkeypatch.setattr(pm, "install_exporter", lambda ctx, m, action="install": {"ok": True, "message": f"{action} {m}"})
    assert pm.act({}, ctx, "souko", "exporter", "remove")["message"] == "remove souko"


def test_only_the_parked_reserve_worker_without_an_exporter_is_woken():
    st = {"souko": "off", "hashira-2": "off"}
    assert pm.to_wake(st, "hashira-2", {}, "v2") == "hashira-2"
    assert pm.to_wake(st, "hashira-2", {"hashira-2": "v1"}, "v2") == "hashira-2"   # outdated: wake it too
    assert pm.to_wake(st, "hashira-2", {"hashira-2": "v2"}, "v2") is None   # current: let it sleep
    assert pm.to_wake({"hashira-2": "held"}, "hashira-2", {}, "v2") is None     # maintenance / turned off
    assert pm.to_wake(st, None, {}, "v2") is None


def test_outdated_and_config_id_follow_the_install_script():
    st = {"kihon": "up", "souko": "up", "hashira-1": "off"}
    assert pm.outdated(st, {"kihon": "v2", "souko": "v1", "hashira-1": "v1"}, "v2") == ["souko"]
    cid = pm.config_id({"exporter_version": "1.12.1", "exporter_port": 9100})
    assert cid.startswith("1.12.1-9100-") and len(cid) == len("1.12.1-9100-") + 10


def test_disks_once_per_device_and_no_nan():
    rows = [{"metric": {"machine": "souko", "mountpoint": mp, "device": dev}, "value": [0, v]}
            for mp, dev, v in [("/home", "/dev/mapper/root", "2.4"), ("/", "/dev/mapper/root", "2.4"),
                               ("/boot", "/dev/nvme2n1p1", "29"), ("/run/user/1000/doc", "portal", "NaN")]]
    assert pm.disks_of("souko", rows) == [("/", 2.4), ("/boot", 29.0)]


def test_grafana_dashboards_only_when_set_and_say_why_when_not_reachable(monkeypatch):
    ctx = SimpleNamespace(options={}, secret=lambda n: "t")
    assert pm.grafana_dashboards(ctx) == []
    ctx.options = {"grafana_api": "http://grafana:3000", "grafana_token": "GRAFANA_HA_VIEWER_TOKEN"}

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'[{"uid": "n1", "title": "Node Exporter Full", "url": "/d/n1/node", "folderTitle": "General"}]'
    monkeypatch.setattr(pm.urllib.request, "urlopen", lambda req, timeout: R())
    assert pm.grafana_dashboards(ctx) == [{"id": "dash-n1", "name": "Grafana: Node Exporter Full", "state": "up",
                                          "url": "http://grafana:3000/d/n1/node", "facts": ["General"], "actions": []}]
    monkeypatch.setattr(pm.urllib.request, "urlopen", lambda req, timeout: (_ for _ in ()).throw(OSError("refused")))
    assert pm.grafana_dashboards(ctx)[0]["state"] == "down"


def test_note_installed_from_many_threads_keeps_every_machine(tmp_path):
    import threading
    path = tmp_path / "exporters.json"
    ts = [threading.Thread(target=pm.note_installed, args=({f"m{i}": "v2"},), kwargs={"path": path, "replace": True})
          for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(pm.installed(path)) == 20


def test_disks_plugged_in_under_media_never_count_as_almost_full():
    from pathlib import Path
    import yaml
    rules = yaml.safe_load((Path(__file__).parent.parent / "rules.yml").read_text())
    expr = next(r["expr"] for g in rules["groups"] for r in g["rules"] if r["alert"] == "DiskAlmostFull")
    assert expr.count('mountpoint!~"/run/media/.*|/media/.*"') == 2
