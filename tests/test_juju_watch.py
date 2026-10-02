"""Tests for .github/actions/collect/scripts/juju-watch.sh and stop-watcher.sh.

They run the watcher against tests/fakes/juju and tests/fakes/sudo, with short
intervals. consumer-sim.yml tests it against real Juju.
"""

import json
import os
import pathlib
import re
import signal
import subprocess
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / ".github/actions/collect/scripts"
FAKES = REPO / "tests/fakes"


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def debug_line(entity, message):
    return f"{entity}: 2026-09-25 10:00:00.000 INFO juju.worker {message}\n"


class Watch:
    def __init__(self, tmp_path, collect_name="collect"):
        self.collect = tmp_path / collect_name
        self.collect.mkdir()
        self.fake = tmp_path / "fake"
        self.fake.mkdir()
        self.user_store = tmp_path / "user-store"
        self.root_store = tmp_path / "root-store"
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.proc = None

    def controller(self, store, ctl, models, lines=None):
        """A store with one controller. models are (name, is_controller) pairs."""
        path = self.user_store if store == "user" else self.root_store
        path.mkdir(exist_ok=True)
        (path / "controllers.yaml").write_text("controllers: {}\n")
        d = self.fake / store
        d.mkdir(parents=True, exist_ok=True)
        (d / "controllers.json").write_text(json.dumps({"controllers": {ctl: {}}}))
        self.set_models(store, ctl, models)
        for model, _ in models:
            self.set_debug(store, ctl, model, lines or [debug_line("controller-0", model)])
            self.set_status(store, ctl, model, json.dumps({"model": {"name": model}}) + "\n")

    def set_models(self, store, ctl, models):
        d = self.fake / store / ctl
        d.mkdir(parents=True, exist_ok=True)
        listing = [{"short-name": m, "is-controller": c} for m, c in models]
        (d / "models.json").write_text(json.dumps({"models": listing}))

    def model_dir(self, store, ctl, model):
        d = self.fake / store / ctl / model
        d.mkdir(parents=True, exist_ok=True)
        return d

    def set_debug(self, store, ctl, model, lines):
        (self.model_dir(store, ctl, model) / "debug.log").write_text("".join(lines))

    def set_status(self, store, ctl, model, body):
        (self.model_dir(store, ctl, model) / "status.json").write_text(body)

    def start(self, with_juju=True, sudo=True, **knobs):
        env = {
            "PATH": os.pathsep.join(([str(FAKES)] if with_juju else []) + ["/usr/bin", "/bin"]),
            "HOME": str(self.home),
            "JUJU_DATA": str(self.user_store),
            "FAKE_JUJU_DIR": str(self.fake),
            "FLAKE0_WATCH_INTERVAL": "0.2",
            "FLAKE0_WATCH_RETRY": "0.1",
            "FLAKE0_WATCH_SNAP_JUJU": "/nonexistent/juju",
            "FLAKE0_WATCH_ROOT_STORE": str(self.root_store),
            "FLAKE0_WATCH_SUDO": str(FAKES / "sudo") if sudo else "false",
            **{k: str(v) for k, v in knobs.items()},
        }
        self.proc = subprocess.Popen(
            [str(SCRIPTS / "juju-watch.sh"), str(self.collect)],
            env=env,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert wait_for(lambda: (self.collect / "watcher.pid").exists())
        return self

    def stop(self):
        if self.proc is None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait(timeout=5)
        for pid in self.fake_juju_pids():
            os.kill(pid, signal.SIGKILL)

    def names(self):
        return sorted(p.name for p in self.collect.iterdir())

    def read(self, name):
        p = self.collect / name
        return p.read_text() if p.exists() else ""

    def calls(self):
        p = self.fake / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def wait_ticks(self, n=2):
        start = len(self.calls())
        assert wait_for(lambda: sum("controllers" in c for c in self.calls()[start:]) > n)

    def fake_juju_pids(self):
        """Running fake juju processes of this test."""
        out = subprocess.run(
            ["pgrep", "-f", str(FAKES / "juju")], capture_output=True, text=True, check=False
        ).stdout
        mine = f"FAKE_JUJU_DIR={self.fake}".encode()
        pids = []
        for pid in map(int, out.split()):
            try:
                if mine in pathlib.Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
                    pids.append(pid)
            except OSError:
                pass
        return pids

    def kill_streams(self):
        for pid in self.fake_juju_pids():
            os.kill(pid, signal.SIGTERM)


@pytest.fixture
def watch(tmp_path):
    w = Watch(tmp_path)
    yield w
    w.stop()


LOG = "juju-debug-user-lxd-testing.log"


# --- quiet without Juju -----------------------------------------------------


def test_no_juju_writes_only_the_pidfile(watch):
    watch.start(with_juju=False)
    time.sleep(1)
    assert watch.names() == ["watcher.pid"]


def test_no_store_runs_no_juju(watch):
    watch.start()
    time.sleep(1)
    assert watch.names() == ["watcher.pid"]
    assert watch.calls() == []


def test_never_prints(watch):
    watch.controller("user", "lxd", [("testing", False)])
    watch.start()
    time.sleep(1)
    watch.stop()
    assert watch.proc.stdout.read() == b"" and watch.proc.stderr.read() == b""


# --- discovery ----------------------------------------------------------------


def test_store_appearing_later_is_followed(watch):
    watch.start()
    time.sleep(0.5)
    watch.controller("user", "lxd", [("testing", False)])
    assert wait_for(lambda: "controller-0: " in watch.read(LOG))
    assert "user debug-log --replay --tail --date --utc --ms -m lxd:testing" in watch.calls()


def test_root_store_goes_through_sudo(watch):
    watch.controller("root", "lxd", [("testing", False)])
    watch.start()
    assert wait_for(lambda: "controller-0: " in watch.read("juju-debug-root-lxd-testing.log"))
    assert all(c.startswith("root ") for c in watch.calls())


def test_root_store_is_skipped_without_sudo(watch):
    watch.controller("root", "lxd", [("testing", False)])
    watch.start(sudo=False)
    time.sleep(1)
    assert watch.names() == ["watcher.pid"]


def test_every_model_is_followed_and_workload_models_get_status(watch):
    watch.controller("user", "lxd", [("controller", True), ("testing", False)])
    watch.start()
    assert wait_for(lambda: "controller-0: " in watch.read("juju-debug-user-lxd-controller.log"))
    assert wait_for(lambda: watch.read("juju-status-user-lxd-testing.ndjson").count("\n") >= 2)
    assert "juju-status-user-lxd-controller.ndjson" not in watch.names()


def test_model_added_later_is_followed(watch):
    watch.controller("user", "lxd", [("testing", False)])
    watch.start()
    watch.set_debug("user", "lxd", "extra", [debug_line("model-1", "extra")])
    watch.set_models("user", "lxd", [("testing", False), ("extra", False)])
    assert wait_for(lambda: "model-1: " in watch.read("juju-debug-user-lxd-extra.log"))


def test_names_are_made_safe_for_file_names(watch):
    watch.controller("user", "a/b", [("c d", False)])
    watch.start()
    assert wait_for(lambda: "juju-debug-user-a_b-c_d.log" in watch.names())


def test_collect_dir_with_spaces(tmp_path):
    w = Watch(tmp_path, collect_name="collect dir")
    try:
        w.controller("user", "lxd", [("testing", False)])
        w.start()
        assert wait_for(lambda: "controller-0: " in w.read(LOG))
    finally:
        w.stop()


# --- model log ------------------------------------------------------------------


def test_follower_stays_connected(watch):
    watch.controller("user", "lxd", [("testing", False)])
    watch.start()
    assert wait_for(lambda: "controller-0: " in watch.read(LOG))
    watch.wait_ticks(3)
    first = watch.read(LOG).splitlines()[0]
    assert re.fullmatch(
        r"# flake0: connect user:lxd:testing at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", first
    )
    assert "reconnect" not in watch.read(LOG)


def test_dropped_stream_reconnects_with_a_marker(watch):
    watch.controller("user", "lxd", [("testing", False)])
    watch.start()
    assert wait_for(lambda: "controller-0: " in watch.read(LOG))
    watch.kill_streams()
    assert wait_for(lambda: watch.read(LOG).count("# flake0: connect") == 2)
    assert "# flake0: reconnect 1 at " in watch.read(LOG)


def test_destroyed_model_ends_its_follower(watch):
    watch.controller("user", "lxd", [("testing", False)])
    watch.start()
    assert wait_for(lambda: "controller-0: " in watch.read(LOG))
    watch.set_models("user", "lxd", [])
    watch.kill_streams()
    assert wait_for(lambda: "# flake0: ended at " in watch.read(LOG))
    watch.wait_ticks()
    assert watch.read(LOG).count("# flake0: connect") == 1


def test_cap_stops_replaying(watch):
    lines = [debug_line("unit-a-0", f"line {i}") for i in range(20)]
    watch.controller("user", "lxd", [("testing", False)], lines=lines)
    watch.start(FLAKE0_WATCH_CAP=200)
    assert wait_for(lambda: "line 19" in watch.read(LOG))
    watch.kill_streams()
    assert wait_for(lambda: "# flake0: capped at " in watch.read(LOG))
    assert wait_for(lambda: "user debug-log --tail --date --utc --ms -m lxd:testing" in watch.calls())


# --- status -----------------------------------------------------------------------


def test_status_lines_are_timestamped_json(watch):
    watch.controller("user", "lxd", [("testing", False)])
    watch.set_status("user", "lxd", "testing", '{\n"model": {"name": "testing"}\n}\n')
    watch.start()
    f = "juju-status-user-lxd-testing.ndjson"
    assert wait_for(lambda: watch.read(f).count("\n") >= 2)
    for line in watch.read(f).splitlines():
        doc = json.loads(line)
        assert doc["ts"].endswith("Z") and doc["status"]["model"]["name"] == "testing"


def test_bad_status_is_logged_once_and_not_recorded(watch):
    watch.controller("user", "lxd", [("testing", False)])
    watch.set_status("user", "lxd", "testing", "WARNING model is being migrated\n")
    watch.start()
    watch.wait_ticks(4)
    assert "juju-status-user-lxd-testing.ndjson" not in watch.names()
    assert watch.read("watcher.log").count("status failed for user:lxd:testing") == 1


# --- stop-watcher.sh ------------------------------------------------------------------


def stop_watcher(collect):
    return subprocess.run(
        [str(SCRIPTS / "stop-watcher.sh"), str(collect)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_stop_kills_the_whole_tree(watch):
    watch.controller("user", "lxd", [("testing", False), ("extra", False)])
    watch.start()
    assert wait_for(lambda: len(watch.fake_juju_pids()) == 2)
    result = stop_watcher(watch.collect)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert watch.proc.wait(timeout=5) is not None
    assert watch.fake_juju_pids() == []
    assert not (watch.collect / "watcher.pid").exists()


def test_stop_twice_or_without_a_watcher_is_a_no_op(watch, tmp_path):
    watch.start(with_juju=False)
    assert stop_watcher(watch.collect).returncode == 0
    again = stop_watcher(watch.collect)
    assert (again.returncode, again.stdout) == (0, "")
    assert watch.names() == []


def test_stop_ignores_a_reused_pid(tmp_path):
    bystander = subprocess.Popen(["sleep", "30"])
    try:
        (tmp_path / "watcher.pid").write_text(f"{bystander.pid}\n")
        assert stop_watcher(tmp_path).returncode == 0
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()
