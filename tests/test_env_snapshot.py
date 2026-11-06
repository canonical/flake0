"""Tests for .github/actions/collect/scripts/env-snapshot.sh."""

import json
import os
import pathlib
import re
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / ".github/actions/collect/scripts/env-snapshot.sh"
SNAPD = "/run/snapd.socket"


def snapshot(out, socket):
    subprocess.run([SCRIPT, out], env={**os.environ, "FLAKE0_SNAPD_SOCKET": str(socket)}, check=True, timeout=30)
    return json.loads(out.read_text())


def test_no_snapd_gives_nulls(tmp_path):
    env = snapshot(tmp_path / "env.json", tmp_path / "none")
    assert env["snaps"] is None
    assert env["changes"] is None
    assert env["kernel"] == os.uname().release
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", env["ts"])


@pytest.mark.skipif(not os.path.exists(SNAPD), reason="needs snapd")
def test_real_snapd(tmp_path):
    env = snapshot(tmp_path / "env.json", SNAPD)
    assert any("name" in snap and "revision" in snap for snap in env["snaps"])
    assert isinstance(env["changes"], list)
