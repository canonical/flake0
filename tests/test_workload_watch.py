"""Tests for .github/actions/collect/scripts/workload-watch.sh and its part of stop-watcher.sh.

They run the watcher against fake LXD monitor processes, tests/fakes/lxc, a fake
/var/log/containers and a fake /proc, with short intervals. consumer-sim.yml tests it
against real LXD and Canonical K8s.
"""

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
MARKER = r"# flake0: connect {} at \d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{{3}}Z"


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=5)


class Watch:
    def __init__(self, tmp_path):
        self.collect = tmp_path / "collect"
        self.collect.mkdir()
        self.lxd = tmp_path / "lxd"
        self.lxc = tmp_path / "lxc"
        self.lxc.mkdir()
        self.links = tmp_path / "containers"
        self.pods = tmp_path / "pods"
        self.proc_dir = tmp_path / "proc"
        self.monitors = []
        self.proc = None

    # --- LXD -----------------------------------------------------------------------

    def container(self, name, journal="Oct 02 10:00:00 boot\n"):
        """Starts a fake LXD monitor for container name. Returns the init pid."""
        argv0 = f"[lxc monitor] {self.lxd}/containers {name}"
        monitor = subprocess.Popen(
            ["bash", "-c", f'exec -a "{argv0}" bash -c "sleep 300 & wait"'],
            start_new_session=True,
        )
        self.monitors.append(monitor)
        d = self.lxc / name
        d.mkdir(exist_ok=True)
        (d / "journal").write_text(journal)
        assert wait_for(lambda: self.children(monitor.pid))
        return self.children(monitor.pid)[0]

    @staticmethod
    def children(pid):
        out = subprocess.run(
            ["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False
        ).stdout
        return [int(p) for p in out.split()]

    def stop_container(self, name):
        for monitor in self.monitors:
            if f"containers {name}" in " ".join(self.cmdline(monitor.pid)):
                kill_group(monitor)

    @staticmethod
    def cmdline(pid):
        try:
            return pathlib.Path(f"/proc/{pid}/cmdline").read_text().split("\0")
        except OSError:
            return []

    def snap_log(self, pid, snap, directory, name, text):
        d = (
            self.proc_dir
            / str(pid)
            / "root/var/snap"
            / snap
            / "common/var/log"
            / directory
        )
        d.mkdir(parents=True, exist_ok=True)
        with open(d / name, "a") as f:
            f.write(text)

    def lxc_calls(self):
        p = self.lxc / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    # --- K8s -----------------------------------------------------------------------

    def pod(self, link, text):
        """A kubelet log file and its /var/log/containers symlink. Returns the file."""
        self.links.mkdir(exist_ok=True)
        target = self.pods / link.removesuffix(".log") / "0.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        (self.links / link).symlink_to(target)
        return target

    # --- watcher -------------------------------------------------------------------

    def start(self, **knobs):
        env = {
            "PATH": "/usr/bin:/bin",
            "FAKE_LXC_DIR": str(self.lxc),
            "FLAKE0_WATCH_INTERVAL": "0.2",
            "FLAKE0_WATCH_LXC": str(FAKES / "lxc"),
            "FLAKE0_WATCH_LXD_DIR": str(self.lxd),
            "FLAKE0_WATCH_CONTAINERS_DIR": str(self.links),
            "FLAKE0_WATCH_PROC": str(self.proc_dir),
            **{k: str(v) for k, v in knobs.items()},
        }
        self.proc = subprocess.Popen(
            [str(SCRIPTS / "workload-watch.sh"), str(self.collect)],
            env=env,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert wait_for(lambda: (self.collect / "workload-watcher.pid").exists())
        return self

    def stop(self):
        for monitor in self.monitors:
            kill_group(monitor)
        if self.proc is not None:
            kill_group(self.proc)

    def names(self):
        return sorted(
            str(p.relative_to(self.collect))
            for p in self.collect.rglob("*")
            if p.is_file()
        )

    def read(self, name):
        p = self.collect / name
        return p.read_text() if p.exists() else ""

    def tails(self, path):
        """Pids of running tail processes that follow path."""
        out = subprocess.run(
            ["pgrep", "-f", f"tail .*{path}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return [int(p) for p in out.stdout.split()]


@pytest.fixture
def watch(tmp_path):
    w = Watch(tmp_path)
    yield w
    w.stop()


def sleep_ticks(n=3):
    time.sleep(0.2 * n + 0.3)


# --- quiet without workloads --------------------------------------------------------


def test_nothing_to_follow_writes_only_the_pidfile(watch):
    watch.start()
    sleep_ticks()
    assert watch.names() == ["workload-watcher.pid"]


def test_never_prints(watch):
    watch.pod("p_ns_c-1.log", "line\n")
    watch.container("c1")
    watch.start()
    assert wait_for(lambda: "boot" in watch.read("workload/lxd/c1/journal.log"))
    watch.stop()
    assert watch.proc.stdout.read() == b"" and watch.proc.stderr.read() == b""


# --- K8s ------------------------------------------------------------------------------


def test_pod_appearing_later_is_followed(watch):
    watch.start()
    sleep_ticks()
    target = watch.pod("valkey-0_testing_valkey-abc.log", "first\n")
    out = "workload/k8s/valkey-0_testing_valkey-abc.log"
    assert wait_for(lambda: "first" in watch.read(out))
    with open(target, "a") as f:
        f.write("second\n")
    assert wait_for(lambda: "second" in watch.read(out))
    first = watch.read(out).splitlines()[0]
    assert re.fullmatch(MARKER.format("k8s:valkey-0_testing_valkey-abc.log"), first)


def test_restarted_pod_container_gets_a_second_file(watch):
    watch.pod("valkey-0_testing_valkey-abc.log", "before\n")
    watch.start()
    assert wait_for(
        lambda: "before" in watch.read("workload/k8s/valkey-0_testing_valkey-abc.log")
    )
    watch.pod("valkey-0_testing_valkey-def.log", "after\n")
    assert wait_for(
        lambda: "after" in watch.read("workload/k8s/valkey-0_testing_valkey-def.log")
    )
    assert "after" not in watch.read("workload/k8s/valkey-0_testing_valkey-abc.log")


# --- LXD journal ------------------------------------------------------------------------


def test_container_journal_is_followed(watch):
    watch.start()
    sleep_ticks()
    watch.container("juju-abc-0", journal="Oct 02 10:00:00 snapd started\n")
    out = "workload/lxd/juju-abc-0/journal.log"
    assert wait_for(lambda: "snapd started" in watch.read(out))
    assert re.fullmatch(
        MARKER.format(r"lxd:juju-abc-0:\d+:journal"), watch.read(out).splitlines()[0]
    )
    assert watch.lxc_calls() == [
        "exec juju-abc-0 -- journalctl --utc --no-tail -f -o short-iso-precise"
    ]


def test_container_in_a_project_is_exec_with_the_project(watch):
    watch.container("charmcraft_build-1", journal="built\n")
    watch.start()
    assert wait_for(
        lambda: "built" in watch.read("workload/lxd/charmcraft_build-1/journal.log")
    )
    assert watch.lxc_calls()[0].startswith(
        "exec --project charmcraft build-1 -- journalctl"
    )


def test_dropped_journal_reconnects_with_a_marker(watch):
    watch.container("c1")
    watch.start()
    out = "workload/lxd/c1/journal.log"
    assert wait_for(lambda: "boot" in watch.read(out))
    subprocess.run(["pkill", "-f", f"{FAKES}/lxc exec c1 "], check=True)
    assert wait_for(lambda: watch.read(out).count("boot") == 2)
    assert watch.read(out).count("# flake0: connect") == 2


# --- LXD snap log files ---------------------------------------------------------------------


def test_snap_log_appearing_later_is_followed(watch):
    pid = watch.container("c1")
    watch.start()
    assert wait_for(lambda: "boot" in watch.read("workload/lxd/c1/journal.log"))
    watch.snap_log(pid, "valkey-charmed", "valkey", "valkey.log", "ready\n")
    watch.snap_log(pid, "valkey-charmed", "valkey", "valkey.log.1.gz", "rotated\n")
    out = "workload/lxd/c1/valkey-charmed_valkey_valkey.log"
    assert wait_for(lambda: "ready" in watch.read(out))
    watch.snap_log(pid, "valkey-charmed", "valkey", "valkey.log", "later\n")
    assert wait_for(lambda: "later" in watch.read(out))
    path = "/var/snap/valkey-charmed/common/var/log/valkey/valkey.log"
    assert re.fullmatch(
        MARKER.format(rf"lxd:c1:{pid}:{path}"), watch.read(out).splitlines()[0]
    )
    assert [n for n in watch.names() if "gz" in n] == []


def test_restarted_container_gets_new_followers_and_old_tail_exits(watch):
    old = watch.container("c1")
    watch.snap_log(old, "opensearch", "opensearch", "opensearch.log", "old run\n")
    watch.start()
    out = "workload/lxd/c1/opensearch_opensearch_opensearch.log"
    assert wait_for(lambda: "old run" in watch.read(out))
    assert watch.tails(f"{watch.proc_dir}/{old}/")

    watch.stop_container("c1")
    assert wait_for(lambda: not watch.tails(f"{watch.proc_dir}/{old}/"))
    new = watch.container("c1")
    watch.snap_log(new, "opensearch", "opensearch", "opensearch.log", "new run\n")
    assert wait_for(lambda: "new run" in watch.read(out))
    assert watch.read(out).count("# flake0: connect") == 2
    assert wait_for(
        lambda: (
            watch.read("workload/lxd/c1/journal.log").count("# flake0: connect") == 2
        )
    )


# --- stop-watcher.sh ---------------------------------------------------------------------


def test_stop_kills_the_whole_tree(watch):
    target = watch.pod("p_ns_c-1.log", "line\n")
    watch.container("c1")
    watch.start()
    assert wait_for(lambda: "boot" in watch.read("workload/lxd/c1/journal.log"))
    assert wait_for(lambda: watch.tails(target))
    result = subprocess.run(
        [str(SCRIPTS / "stop-watcher.sh"), str(watch.collect)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert watch.proc.wait(timeout=5) is not None
    assert watch.tails(target) == []
    assert (
        subprocess.run(["pgrep", "-f", f"{FAKES}/lxc exec c1 "], check=False).returncode
        == 1
    )
    assert not (watch.collect / "workload-watcher.pid").exists()
