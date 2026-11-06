"""Tests for .github/actions/collect/scripts/redact.sh and redact.sed.

redact.sh runs in stop.sh, before the bundle is built, to mask secrets in the
collected Juju and workload logs. These tests run it in place over fake log files.
"""

import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[1]
REDACT = REPO / ".github/actions/collect/scripts/redact.sh"


def run(tmp_path, files, values=None):
    env = {"PATH": "/usr/bin:/bin"}
    if values is not None:
        vf = tmp_path / "values"
        vf.write_text("\n".join(values) + "\n")
        env["FLAKE0_REDACT_VALUES"] = str(vf)
    paths = []
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        paths.append(str(p))
    subprocess.run([str(REDACT), *paths], env=env, cwd=tmp_path, check=True)
    return {name: (tmp_path / name).read_text() for name in files}


# --- exact values (the reliable layer) ----------------------------------------------


def test_exact_value_is_masked(tmp_path):
    # "tok" is not a pattern key, so only the exact-value layer can catch this
    out = run(tmp_path, {"log": "connecting with tok=SeKReT-123 done\n"}, values=["SeKReT-123"])
    assert "SeKReT-123" not in out["log"]
    assert out["log"] == "connecting with tok=***REDACTED*** done\n"


def test_value_with_regex_metacharacters_is_escaped(tmp_path):
    secret = "a.b/c*d$e"  # chars special to both regex and the s/// delimiter
    out = run(tmp_path, {"log": f"x {secret} y\n"}, values=[secret])
    assert secret not in out["log"]
    assert out["log"] == "x ***REDACTED*** y\n"


# --- built-in patterns (best effort) ------------------------------------------------


def test_patterns_mask_common_secrets(tmp_path):
    jwt = "eyJhbGciOiJIUzI1Ni_.eyJzdWIiOiIxMjM0_.sIG9mIHRoZSBKV1Q"
    text = (
        "password=hunter2\n"
        "Authorization: Bearer abc.def-ghi\n"
        f"id_token={jwt}\n"
        "key AKIAIOSFODNN7EXAMPLE used\n"
    )
    out = run(tmp_path, {"log": text})["log"]
    assert "hunter2" not in out
    assert "abc.def-ghi" not in out
    assert jwt not in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert out.count("***REDACTED") == 4


def test_pem_private_key_block_is_removed(tmp_path):
    text = (
        "before\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIBOgIBAAJBAKj34GkxFhD\n"
        "90oN secret body line\n"
        "-----END RSA PRIVATE KEY-----\n"
        "after\n"
    )
    out = run(tmp_path, {"log": text})["log"]
    assert "MIIBOgIBAAJBAKj34GkxFhD" not in out
    assert "90oN" not in out
    assert "before" in out and "after" in out
    assert "***REDACTED-PRIVATE-KEY-BLOCK***" in out


# --- safety -------------------------------------------------------------------------


def test_benign_text_is_untouched(tmp_path):
    text = "unit valkey/0 ready to accept connections\nstatus active\n"
    out = run(tmp_path, {"log": text})["log"]
    assert out == text


def test_connect_markers_survive(tmp_path):
    text = "# flake0: connect lxd:c1:1:journal at 2026-10-02T00:00:00.000Z\nhello\n"
    out = run(tmp_path, {"log": text})["log"]
    assert out.splitlines()[0].startswith("# flake0: connect lxd:c1:1:journal at ")


def test_runs_recursively_over_a_directory(tmp_path):
    out = run(
        tmp_path,
        {"workload/lxd/c1/journal.log": "password=hunter2\n", "workload/k8s/p.log": "ok\n"},
    )
    assert "hunter2" not in out["workload/lxd/c1/journal.log"]
    assert out["workload/k8s/p.log"] == "ok\n"


def test_no_inputs_is_a_noop(tmp_path):
    # no redact.sed and no values: data must pass through unchanged
    p = tmp_path / "log"
    p.write_text("password=hunter2\n")
    env = {"PATH": "/usr/bin:/bin", "FLAKE0_REDACT_SED": str(tmp_path / "missing.sed")}
    subprocess.run([str(REDACT), str(p)], env=env, cwd=tmp_path, check=True)
    assert p.read_text() == "password=hunter2\n"


def test_no_path_arguments_touches_nothing(tmp_path):
    # Guard against `find` with no path walking the current directory. Run from an
    # isolated cwd so a regression here cannot escape the temp dir.
    canary = tmp_path / "canary"
    canary.write_text("password=hunter2\n")
    r = subprocess.run([str(REDACT)], env={"PATH": "/usr/bin:/bin"}, cwd=tmp_path, check=False)
    assert r.returncode == 0
    assert canary.read_text() == "password=hunter2\n"
