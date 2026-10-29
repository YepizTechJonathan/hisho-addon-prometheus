"""Prometheus add-on tests. Run with Hisho's venv at the repository's top:
~/code/hisho/.venv/bin/python -m pytest -q"""
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from hisho import addon_store, addons, fleet_power, maintenance  # noqa: F401
from hisho.addon_testing import AUTH, TOKEN, Recorder

HERE = Path(__file__).resolve().parents[1]


def ctx_for(addon):
    return addons.Context(addon, resolve=lambda name: f"value-of-{name}")


def test_manifest_is_valid_and_readme_matches(addon):
    assert addons.validate_manifest(addon.manifest) == []
    assert addon_store.readme_problems(HERE) == []


def test_install_finishes_without_a_site_url_and_hands_the_address_to_hisho():
    """A fresh machine has no site_url: the install stopped on its very last line (set -u, 2026-10-29)."""
    import re
    install = (Path(__file__).resolve().parents[1] / "bin" / "install.sh").read_text()
    assert re.findall(r"\$\{HISHO_OPT_SITE_URL[^}]*\}", install) == ["${HISHO_OPT_SITE_URL:-}"]
    assert 'echo "HISHO_SET site_url=${site}"' in install
