"""Tests for .github/actions/collect/scripts/redact.sh and redact.sed.

redact.sh runs in stop.sh, before the bundle is built, to mask secrets in the
collected Juju and workload logs. These tests run it in place over fake log files.
"""

import json
import pathlib
import subprocess

import pytest

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


@pytest.mark.parametrize("secret", ["ab+cd", "pa(ss", "x|y", "a{2}", "why?"])
def test_value_with_extended_regex_metacharacters_is_escaped(tmp_path, secret):
    # redact.sh runs sed -E, so these are special too. A bad one used to void the whole program.
    out = run(tmp_path, {"log": f"got {secret} and password=hunter2 and lone x\n"}, values=[secret])
    assert out["log"] == "got ***REDACTED*** and password=***REDACTED*** and lone x\n"


def test_pattern_cannot_split_a_listed_value(tmp_path):
    out = run(tmp_path, {"log": "password=hun,ter2\n"}, values=["hun,ter2"])
    assert out["log"] == "password=***REDACTED***\n"


def test_value_inside_a_longer_value_masks_the_longer_one(tmp_path):
    out = run(tmp_path, {"log": "connect with admin-S3cr3t-long now\n"}, values=["admin", "admin-S3cr3t-long"])
    assert out["log"] == "connect with ***REDACTED*** now\n"


def test_value_with_spaces_keeps_them(tmp_path):
    out = run(tmp_path, {"log": "got  two  spaces end\n"}, values=[" two  spaces"])
    assert out["log"] == "got ***REDACTED*** end\n"


def test_crlf_values_file_still_matches(tmp_path):
    values = tmp_path / "values"
    values.write_bytes(b"crlfval\r\n")
    log = tmp_path / "log"
    log.write_text("x crlfval y\n")
    env = {"PATH": "/usr/bin:/bin", "FLAKE0_REDACT_VALUES": str(values)}
    subprocess.run([str(REDACT), str(log)], env=env, check=True)
    assert log.read_text() == "x ***REDACTED*** y\n"


def test_relative_values_path_with_an_equals_sign_is_read(tmp_path):
    # awk would take my=values for a variable assignment.
    (tmp_path / "my=values").write_text("hunter2xyz\n")
    log = tmp_path / "log"
    log.write_text("a hunter2xyz b\n")
    env = {"PATH": "/usr/bin:/bin", "FLAKE0_REDACT_VALUES": "my=values"}
    subprocess.run([str(REDACT), str(log)], env=env, cwd=tmp_path, check=True, timeout=10, stdin=subprocess.DEVNULL)
    assert log.read_text() == "a ***REDACTED*** b\n"


def test_long_value_list_still_redacts(tmp_path):
    # The list used to go on the sed command line, and past 128 KB sed never ran.
    values = [f"value-number-{i:06}-padding-padding" for i in range(3000)]
    out = run(tmp_path, {"log": "password=hunter2 value-number-000042-padding-padding\n"}, values=values)
    assert out["log"] == "password=***REDACTED*** ***REDACTED***\n"


# --- built-in patterns (best effort) ------------------------------------------------


@pytest.mark.parametrize(
    "line, masked",
    [
        ('{"user": "op", "password": "hunter2"}', '{"user": "op", "password": "***REDACTED***"}'),
        ("{'user': 'op', 'password': 'hunter2'}", "{'user': 'op', 'password': '***REDACTED***'}"),
        ('password="hunter2" next', 'password="***REDACTED***" next'),
        ('"token":"hunter2",', '"token":"***REDACTED***",'),
    ],
)
def test_quoted_keys_and_values_are_masked(tmp_path, line, masked):
    assert run(tmp_path, {"log": f"{line}\n"})["log"] == f"{masked}\n"


@pytest.mark.parametrize(
    "line, masked",
    [
        ('{\\"password\\":\\"hunter2\\"}', '{\\"password\\":\\"***REDACTED***\\"}'),
        ('"secret-key": "hunter2"', '"secret-key": "***REDACTED***"'),
        ("SECRET_KEY=hunter2", "SECRET_KEY=***REDACTED***"),
        ("t[password=\\\\xe9]", "t[password=***REDACTED***"),
        ("uri=postgres://relation-1:hunter2@10.0.0.1:5432/db", "uri=postgres://relation-1:***REDACTED***@10.0.0.1:5432/db"),
    ],
)
def test_nested_and_s3_forms_are_masked(tmp_path, line, masked):
    assert run(tmp_path, {"log": f"{line}\n"})["log"] == f"{masked}\n"


def test_scheme_after_a_key_is_masked(tmp_path):
    assert "hunter2" not in run(tmp_path, {"log": "token: Bearer hunter2\n"})["log"]


def test_basic_in_a_test_name_keeps_the_verdict(tmp_path):
    line = "tests/test_basic.py::test_basic FAILED\n"
    assert run(tmp_path, {"log": line})["log"] == line


def test_url_with_a_port_survives(tmp_path):
    line = "GET http://10.0.0.1:8080/path@v2 200\n"
    assert run(tmp_path, {"log": line})["log"] == line


def test_value_with_a_backslash_is_masked_whole(tmp_path):
    assert run(tmp_path, {"log": "password=ab\\cd\n"})["log"] == "password=***REDACTED***\n"


def test_class_scoped_test_id_survives(tmp_path):
    line = "tests/test_a.py::TestPassword::test_rotate PASSED\n"
    assert run(tmp_path, {"log": line})["log"] == line


def test_escaped_character_in_a_marker_line_stays_valid_json(tmp_path):
    line = json.dumps({"nodeid": "t[token:\u00e9]"}) + "\n"
    json.loads(run(tmp_path, {"log": line})["log"])


@pytest.mark.parametrize("param", ['Bearer "abc"', 'Bearer abc"', 'a basic "quoted" id'])
def test_quoted_scheme_in_a_marker_line_stays_valid_json(tmp_path, param):
    line = json.dumps({"nodeid": f"t[{param}]"}) + "\n"
    json.loads(run(tmp_path, {"log": line})["log"])


def test_url_rule_stops_at_a_closing_quote(tmp_path):
    line = '{"url":"http://10.0.0.1:8080","mail":"a@b"}\n'
    assert run(tmp_path, {"log": line})["log"] == line


def test_one_line_pem_keeps_the_rest_of_the_file(tmp_path):
    pem = '{"key": "-----BEGIN PRIVATE KEY-----\\nMII\\n-----END PRIVATE KEY-----\\n"}'
    out = run(tmp_path, {"log": f"before\n{pem}\nafter\n"})["log"]
    assert out.splitlines() == ["before", '{"key": "***REDACTED-PRIVATE-KEY-BLOCK***\\n"}', "after"]


def test_quoted_parameter_in_a_marker_line_stays_valid_json(tmp_path):
    line = json.dumps({"event": "start", "nodeid": 'a.py::test_p[password="hunter2"]'}) + "\n"
    out = run(tmp_path, {"log": line})["log"]
    assert "hunter2" not in json.loads(out)["nodeid"]


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
