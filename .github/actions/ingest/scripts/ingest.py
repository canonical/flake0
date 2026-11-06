#!/usr/bin/env python3
"""flake0-ingest: load a flake0 collect bundle into OpenSearch 2.x.

Usage: ingest.py <bundle.tar.zst | bundle.tar.gz | extracted bundle dir>

Reads OPENSEARCH_URL (https only), OPENSEARCH_USER, OPENSEARCH_PASSWORD and
OPENSEARCH_CA (a CA file, optional) from the environment, so the password never
shows in argv. Loading a bundle twice overwrites it rather than duplicating it.
"""

import base64
import hashlib
import json
import os
import re
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from itertools import chain

TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates")
BATCH_BYTES = 5 * 1024 * 1024
# The global tags telegraf.conf puts on every metric, and where start.sh read them.
RUN_TAGS = {
    "run_id": "GITHUB_RUN_ID", "run_attempt": "GITHUB_RUN_ATTEMPT", "repo": "GITHUB_REPOSITORY",
    "workflow": "GITHUB_WORKFLOW", "job": "GITHUB_JOB", "sha": "GITHUB_SHA", "ref": "GITHUB_REF_NAME",
    "runner_name": "RUNNER_NAME", "runner_os": "RUNNER_OS", "image_os": "ImageOS",
}
METRICS = re.compile(r"metrics[^/]*\.json")  # Telegraf rotates to metrics.<time>-<n>.json
JUJU_STATUS = re.compile(r"juju-status-[^/]*\.ndjson")
CRI = re.compile(r"\S+ (?:stdout|stderr) [FP](?: |$)")

ISO = r"(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)(?:[.,](\d+))?(Z|[+-]\d\d:?\d\d)?"
TIMES = [
    re.compile(r"# flake0: .*? at " + ISO),  # our markers put the time last
    re.compile(r"\[?" + ISO),  # dmesg, journalctl, CRI, watchers, and most snap logs
    re.compile(r"[^\s:]+: " + ISO),  # juju debug-log, in UTC with no zone
    re.compile(r'\{"t":\{"\$date":"' + ISO),  # mongod
]
VALKEY = re.compile(r"\d+:[A-Z] (\d\d [A-Z][a-z]{2} \d{4} \d\d:\d\d:\d\d)\.(\d+) ")


class Failure(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def warn(message):
    print(f"flake0-ingest: {message}", file=sys.stderr)


def iso(dt, frac):
    """Formats an aware datetime as UTC ISO with milliseconds, the format of every flake0 file."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (frac or "0")[:3].ljust(3, "0") + "Z"


def parse_ts(line):
    """Returns the time a log line starts with, or None. A line without a zone is UTC."""
    try:
        for pattern in TIMES:
            if m := pattern.match(line):
                date, clock, frac, zone = m.groups()
                zone = "+00:00" if zone in (None, "Z") else zone[:3] + ":" + zone[-2:]
                return iso(datetime.fromisoformat(f"{date}T{clock}{zone}"), frac)
        if m := VALKEY.match(line):
            return iso(datetime.strptime(m[1], "%d %b %Y %H:%M:%S").replace(tzinfo=timezone.utc), m[2])
    except ValueError:
        pass
    return None


def from_ms(ms):
    seconds, millis = divmod(int(ms), 1000)
    return iso(datetime.fromtimestamp(seconds, timezone.utc), f"{millis:03d}")


def month(ts):
    return f"{ts[:4]}.{ts[5:7]}"


def doc_id(*parts):
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:40]


def regular_files(root):
    """Maps bundle-relative paths to paths, for regular files only: a symlink in a
    downloaded bundle could point at a local secret."""
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if stat.S_ISREG(os.lstat(path).st_mode):
                files[os.path.relpath(path, root)] = path
    return files


def read_json(files, rel):
    try:
        with open(files[rel], encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else None
    except (KeyError, ValueError):
        return None


def lines(files, rel):
    """Yields (line number, line). Bytes that are not UTF-8 become U+FFFD."""
    with open(files[rel], encoding="utf-8", errors="replace") as f:
        yield from enumerate((line.rstrip("\n") for line in f), 1)


def json_lines(files, rel):
    """Yields (line number, parsed line), and warns once about lines that are not JSON,
    such as the last line of a collector that was killed mid-write."""
    bad = 0
    for n, line in lines(files, rel):
        try:
            yield n, json.loads(line)
        except ValueError:
            bad += bool(line.strip())
    if bad:
        warn(f"skipped {bad} unreadable line{'s' * (bad > 1)} in {rel}")


def run_tags(files):
    env = {tag: os.environ.get(var) or "local" for tag, var in RUN_TAGS.items()}
    for rel in filter(METRICS.fullmatch, files):
        for _, batch in json_lines(files, rel):
            try:
                tags = batch["metrics"][0]["tags"]
                return {tag: str(tags.get(tag, env[tag])) for tag in RUN_TAGS}
            except (KeyError, IndexError, TypeError, AttributeError):
                continue
    return env


def metric_doc(m, bundle_id):
    ts = from_ms(m["timestamp"])
    # The shape Telegraf's outputs.opensearch writes, so live streaming can share the indices.
    return ts, {"@timestamp": ts, "measurement_name": m["name"], "tag": m["tags"],
                m["name"]: m["fields"], "bundle_id": bundle_id}


def metric_docs(files, bundle_id):
    for rel in filter(METRICS.fullmatch, files):
        bad = 0
        for n, batch in json_lines(files, rel):
            try:
                docs = [metric_doc(m, bundle_id) for m in batch["metrics"]]
            except (KeyError, TypeError, ValueError, OSError, OverflowError):
                bad += 1
                continue
            for i, (ts, doc) in enumerate(docs):
                yield f"ci-metrics-{month(ts)}", doc_id(bundle_id, rel, n, i), doc
        if bad:
            warn(f"skipped {bad} badly shaped line{'s' * (bad > 1)} in {rel}")


def log_source(rel):
    """Names the ci-logs-<source>-* index of a bundle file, one per log format."""
    if rel.startswith("juju-debug-"):
        return "juju"
    if rel.startswith("workload/k8s/"):
        return "k8s"
    if rel.startswith("workload/lxd/"):
        return "lxd" if rel.endswith("/journal.log") else "app"
    return {"kernel.log": "host", "snapd.log": "host", "job.log": "job", "pytest.log": "pytest"}.get(rel, "flake0")


def log_docs(files, bundle_id, tags, fallback_month):
    for rel in (f for f in files if f.endswith(".log")):
        ts = anchor = None
        offset = 0
        seen = {}
        cri = rel.startswith("workload/k8s/")
        index = f"ci-logs-{log_source(rel)}"
        for n, line in lines(files, rel):
            if not line.strip():
                continue
            if line.startswith("# flake0: "):
                seen = {}  # a follower reconnected, and may replay what came before
            if own := parse_ts(line):
                ts, anchor, offset = own, line, 0
            else:
                offset += 1
            message = line
            if cri and (m := CRI.match(line)):
                message = line[m.end():]
            doc = {"bundle_id": bundle_id, "tag": tags, "file": rel, "line": n, "message": message}
            if ts:
                doc["@timestamp"] = ts
            # Keyed on content and its count since the last marker, not the line number, so the
            # history a juju or workload follower replays after a reconnect lands on the same
            # documents, while a line that really repeats keeps its own.
            key = (anchor, offset, line)
            seen[key] = seen.get(key, -1) + 1
            yield f"{index}-{month(ts) if ts else fallback_month}", doc_id(bundle_id, rel, *key, seen[key]), doc


def meta_docs(files, bundle_id, tags, env_start, env_end):
    start, end = (env_start or {}).get("ts"), (env_end or {}).get("ts")
    run = {"type": "run", "bundle_id": bundle_id, "tag": tags, "end": end,
           "env_start": env_start, "env_end": env_end}
    if start:
        run["@timestamp"] = start
    yield "ci-meta", bundle_id, run

    def test(nodeid, attempt, started, finished, outcome, duration):
        doc = {"type": "test", "bundle_id": bundle_id, "tag": tags, "nodeid": nodeid, "attempt": attempt,
               "outcome": outcome, "@timestamp": started, "end": finished, "duration": duration}
        return "ci-meta", doc_id(bundle_id, nodeid, attempt), doc

    if "markers.ndjson" not in files:
        return
    attempts, running = {}, {}
    for _, m in json_lines(files, "markers.ndjson"):
        try:
            event, nodeid, ts = m["event"], m["nodeid"], m["ts"]
        except (KeyError, TypeError):
            continue
        if event == "start":
            attempts[nodeid] = attempts.get(nodeid, 0) + 1
            running.setdefault(nodeid, []).append((attempts[nodeid], ts))
        elif event == "finish" and running.get(nodeid):
            attempt, started = running[nodeid].pop(0)
            yield test(nodeid, attempt, started, ts, m.get("outcome"), m.get("duration"))
    # A test the job's timeout killed has a start and no finish.
    for nodeid, opened in running.items():
        for attempt, started in opened:
            yield test(nodeid, attempt, started, end, "unfinished", None)


def unit_doc(name, unit):
    workload = unit.get("workload-status", {})
    return {"app": name.split("/")[0], "unit": name, "workload_status": workload.get("current"),
            "agent_status": unit.get("juju-status", {}).get("current"), "message": workload.get("message")}


def units(status):
    """Flattens the units of `juju status --format=json`, subordinates included, into fixed
    field names. App and unit names are keys there, and would add mapping fields every run."""
    for app in status.get("applications", {}).values():
        for name, unit in app.get("units", {}).items():
            yield unit_doc(name, unit)
            for sub_name, sub in unit.get("subordinates", {}).items():
                yield unit_doc(sub_name, sub)


def juju_status_docs(files, bundle_id, tags):
    for rel in filter(JUJU_STATUS.fullmatch, files):
        bad = 0
        for n, line in json_lines(files, rel):
            try:
                status = line["status"]
                doc = {"type": "juju_status", "bundle_id": bundle_id, "tag": tags, "@timestamp": line["ts"],
                       "controller": status["model"]["controller"], "model": status["model"]["name"],
                       "units": list(units(status)), "status": status}
            except (KeyError, TypeError, AttributeError):
                bad += 1
                continue
            yield "ci-meta", doc_id(bundle_id, rel, n), doc
        if bad:
            warn(f"skipped {bad} badly shaped line{'s' * (bad > 1)} in {rel}")


def connect(env):
    """Returns request(method, path, body) -> (status, json) for the cluster in env."""
    url = env.get("OPENSEARCH_URL", "").rstrip("/")
    if not url.startswith("https://"):
        raise Failure("OPENSEARCH_URL must be an https:// URL")
    try:
        context = ssl.create_default_context(cafile=env.get("OPENSEARCH_CA") or None)
    except (OSError, ssl.SSLError) as e:
        raise Failure(f"cannot read OPENSEARCH_CA {env['OPENSEARCH_CA']}: {e}") from e
    headers = {}
    if env.get("OPENSEARCH_USER"):
        # Sent up front: urllib's auth handler waits for a 401, sending every batch twice.
        token = f"{env['OPENSEARCH_USER']}:{env.get('OPENSEARCH_PASSWORD', '')}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(token).decode()

    # No redirects: urllib would send the password along, even to an http:// Location.
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), NoRedirect)

    def request(method, path, body=None, ctype="application/json"):
        req = urllib.request.Request(url + path, body, {**headers, "Content-Type": ctype}, method=method)
        try:
            with opener.open(req, timeout=120) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.load(e)
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, OSError) as e:
            raise Failure(f"cannot reach {url}: {e}") from e

    return request


def merge_mappings(*mappings):
    merged = {}
    for m in mappings:
        for key, value in m.items():
            merged[key] = {**merged.get(key, {}), **value} if key == "properties" else value
    return merged


def install_templates(request):
    """Installs templates/*.json. A file without index_patterns is a component template,
    shared by the index templates that list it in composed_of, so it goes first."""
    templates = {}
    for name in sorted(os.listdir(TEMPLATES)):
        with open(os.path.join(TEMPLATES, name)) as f:
            templates[name[:-len(".json")]] = json.load(f)
    components = {name: t for name, t in templates.items() if "index_patterns" not in t}
    for name, template in sorted(templates.items(), key=lambda item: item[0] not in components):
        kind = "_component_template" if name in components else "_index_template"
        status, resp = request("PUT", f"/{kind}/{name}", json.dumps(template).encode())
        if status == 403:
            warn(f"no permission to install the {name} template, loading with the one in place")
        elif status >= 300:
            raise Failure(f"installing the {name} template failed: HTTP {status} {resp}")
        if name in components:
            continue
        # A template applies only when an index is created, so add new fields to existing ones
        # too. 404 means no index yet. A field that changed type needs a reindex.
        pattern = template["index_patterns"][0]
        mappings = merge_mappings(*(components[c]["template"]["mappings"] for c in template.get("composed_of", [])),
                                  template.get("template", {}).get("mappings", {}))
        status, resp = request("PUT", f"/{pattern}/_mapping", json.dumps(mappings).encode())
        if status not in (200, 404):
            warn(f"could not update the mapping of {pattern}, loading anyway: HTTP {status} {resp}")


def send(request, body):
    for wait in (1, 5, 15, None):
        status, resp = request("POST", "/_bulk", body, "application/x-ndjson")
        failed = [r for item in resp.get("items", []) for r in item.values() if r.get("status", 500) >= 300]
        throttled = status == 429 or any(r.get("status") == 429 for r in failed)
        if not throttled or wait is None:
            break
        time.sleep(wait)  # every document has its id, so resending the batch is safe
    if status >= 300:
        raise Failure(f"bulk upload failed: HTTP {status} {resp}")
    if failed:
        reasons = "; ".join(str(r.get("error", {}).get("reason", r.get("status"))) for r in failed[:3])
        raise Failure(f"OpenSearch rejected {len(failed)} documents, first: {reasons}")


def bulk(request, docs):
    batch, size, total = [], 0, 0
    for index, id_, doc in docs:
        entry = (json.dumps({"index": {"_index": index, "_id": id_}}) + "\n"
                 + json.dumps(doc, separators=(",", ":")) + "\n").encode()
        batch.append(entry)
        size += len(entry)
        if size >= BATCH_BYTES:
            send(request, b"".join(batch))
            total += len(batch)
            batch, size = [], 0
    if batch:
        send(request, b"".join(batch))
        total += len(batch)
    return total


def main(argv, request=None):
    if len(argv) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        request = request or connect(os.environ)
        with tempfile.TemporaryDirectory() as tmp:
            root = argv[0]
            if not os.path.isdir(root):
                tar = subprocess.run(["tar", "-xf", root, "-C", tmp], capture_output=True, text=True, check=False)
                if tar.returncode:
                    raise Failure(f"cannot extract {root}: {tar.stderr.strip()}")
                root = tmp
            files = regular_files(root)
            tags = run_tags(files)
            env_start, env_end = read_json(files, "env-start.json"), read_json(files, "env-end.json")
            start = (env_start or {}).get("ts")
            bundle_id = doc_id(tags["run_id"], tags["run_attempt"], tags["job"], tags["runner_name"], start)
            fallback_month = month(start or iso(datetime.now(timezone.utc), "0"))
            install_templates(request)
            total = bulk(request, chain(
                meta_docs(files, bundle_id, tags, env_start, env_end),
                juju_status_docs(files, bundle_id, tags),
                metric_docs(files, bundle_id),
                log_docs(files, bundle_id, tags, fallback_month),
            ))
    except (Failure, OSError) as e:
        warn(str(e))
        return 1
    print(f"flake0-ingest: sent {total} documents, bundle_id {bundle_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
