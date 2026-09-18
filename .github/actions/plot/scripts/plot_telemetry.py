#!/usr/bin/env python3
"""Render a runner-health dashboard from a flake0 collect bundle's metrics.json.

    plot_telemetry.py <outdir> <metrics.json> [<metrics.json> ...]

Zero configuration: title, subtitle, time range, tracked devices/interfaces and
processes are all derived from the bundle itself. Writes <outdir>/health.png.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime as _datetime
from datetime import timedelta, timezone
from functools import partial

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

COMMON_TAG_KEYS = ("repo", "workflow", "job", "run_id", "sha", "ref", "runner_name")

# telegraf.conf's [[inputs.diskio]] devices allowlist (sd*/vd*/xvd*/nvme*/dm-*)
# matches partitions and device-mapper volumes too; both double-count their
# parent whole disk.
_DISK_PARTITION_RE = re.compile(r"^(sd|vd|xvd)[a-z]+\d+$")
_DISK_NVME_PARTITION_RE = re.compile(r"^nvme\d+n\d+p\d+$")
_DISK_DM_RE = re.compile(r"^dm-.*$")

# telegraf.conf's [[inputs.net]] interfaces allowlist admits only eth*/en* and
# four bridge families (docker0/lxdbr*/br-*/virbr*) -- nothing else -- so a
# positive match on eth*/en* is exactly equivalent to excluding the bridges.
_NET_PRIMARY_RE = re.compile(r"^(eth|en)")

CYCLE = ["#4c78a8", "#54a24b", "#f58518", "#b279a2", "#72b7b2", "#ff9da6", "#9d755d", "#bab0ac"]


class Loaded:
    def __init__(self):
        self.by_name = defaultdict(list)
        self.common_tags = {}
        self.ts_min = None
        self.ts_max = None


def load_metrics(paths):
    """Read one or more metrics*.json files into a Loaded bundle.

    Tolerant of malformed lines and partial files (a SIGKILLed collector can
    leave a truncated final line): such lines are skipped and counted, never
    raised.
    """
    loaded = Loaded()
    skipped = 0
    for path in paths:
        try:
            fh = open(path, encoding="utf-8")  # noqa: SIM115 -- only open() itself should be caught below
        except OSError as exc:
            print(f"plot: warning: could not open {path}: {exc}", file=sys.stderr)
            continue
        with fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    batch = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                metrics = batch.get("metrics") if isinstance(batch, dict) else None
                if not isinstance(metrics, list):
                    skipped += 1
                    continue
                for m in metrics:
                    if not isinstance(m, dict):
                        skipped += 1
                        continue
                    name = m.get("name")
                    ts = m.get("timestamp")
                    if not isinstance(name, str) or not isinstance(ts, (int, float)):
                        skipped += 1
                        continue
                    loaded.by_name[name].append(m)
                    if loaded.ts_min is None or ts < loaded.ts_min:
                        loaded.ts_min = ts
                    if loaded.ts_max is None or ts > loaded.ts_max:
                        loaded.ts_max = ts
                    if not loaded.common_tags:
                        tags = m.get("tags") or {}
                        loaded.common_tags = {k: tags[k] for k in COMMON_TAG_KEYS if k in tags}
    if skipped:
        print(f"plot: warning: skipped {skipped} malformed metric line(s)", file=sys.stderr)
    return loaded


def ts_to_dt(ts_ms):
    return _datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def group_by(records, keyfunc):
    """Bucket records by an arbitrary key function.

    Records for which keyfunc raises KeyError (a required tag/field missing)
    are silently dropped -- the same field-presence tolerance every other
    accessor here uses.
    """
    groups = defaultdict(list)
    for r in records:
        try:
            key = keyfunc(r)
        except KeyError:
            continue
        groups[key].append(r)
    return groups


def xy(records, field, scale=1.0):
    """(timestamps, values) for records that actually carry `field`.

    Telegraf emits several measurements as multiple documents per interval
    with disjoint field sets (e.g. `system` splits load1/load5/n_cpus from
    uptime from uptime_format; `pressure` splits total from avg10/60/300;
    `swap` splits in/out from free/total/used/used_percent; `nstat` splits
    its counters across three documents). Every field access in this module
    goes through this function (or `rate` below) instead of bare indexing,
    or it would KeyError on most records of an affected measurement.
    """
    pts = [(m["timestamp"], m["fields"][field]) for m in records if field in m.get("fields", {})]
    pts.sort(key=lambda p: p[0])
    xs = [ts_to_dt(t) for t, _ in pts]
    ys = [v * scale for _, v in pts]
    return xs, ys


def xy_multi(records, fields, scale=1.0):
    """(timestamps, [values-per-field]) for records carrying every field in `fields`.

    Used where several fields must stay aligned point-for-point (a stacked
    area plot needs equal-length series).
    """
    pts = []
    for m in records:
        f = m.get("fields", {})
        if all(field in f for field in fields):
            pts.append((m["timestamp"], tuple(f[field] * scale for field in fields)))
    pts.sort(key=lambda p: p[0])
    xs = [ts_to_dt(t) for t, _ in pts]
    if not pts:
        return xs, tuple([] for _ in fields)
    return xs, tuple(zip(*(vals for _, vals in pts)))


def rate(records, field, scale=1.0):
    """(timestamps, values) of the per-second delta of a monotonic counter field.

    Composes on `xy` (same field-presence filter, same return shape) rather
    than re-implementing the filter/sort; a negative delta (a counter reset)
    is clamped to zero.
    """
    xs_all, ys_all = xy(records, field)
    xs, ys = [], []
    for (t1, v1), (t2, v2) in zip(zip(xs_all, ys_all), zip(xs_all[1:], ys_all[1:])):
        dt = (t2 - t1).total_seconds()
        if dt <= 0:
            continue
        delta = v2 - v1
        if delta < 0:
            delta = 0.0
        xs.append(t2)
        ys.append(delta / dt * scale)
    return xs, ys


def safe_max(seq, default=None):
    seq = list(seq)
    return max(seq) if seq else default


def safe_min(seq, default=None):
    seq = list(seq)
    return min(seq) if seq else default


def is_whole_disk(name):
    if _DISK_DM_RE.match(name) or _DISK_PARTITION_RE.match(name):
        return False
    return not _DISK_NVME_PARTITION_RE.match(name)


def is_primary_interface(name):
    return bool(_NET_PRIMARY_RE.match(name))


def lifeline_spans(records_by_key):
    """[(label, start, width)] for a {(process_name, pid): records} mapping.

    Bar width is floored at one procstat interval (30s): a process sampled
    exactly once would otherwise be an invisible, zero-width bar.
    """
    spans = []
    for (name, pid), records in records_by_key.items():
        timestamps = sorted(m["timestamp"] for m in records)
        if not timestamps:
            continue
        start = ts_to_dt(timestamps[0])
        end = ts_to_dt(timestamps[-1])
        width = max(end - start, timedelta(seconds=30))
        spans.append((f"{name}:{pid}", start, width))
    spans.sort(key=lambda s: s[1])
    return spans


def build_title(loaded):
    tags = loaded.common_tags
    title = f"{tags.get('repo', '?')} — {tags.get('workflow', '?')} / {tags.get('job', '?')}"
    sha = tags.get("sha", "")[:8]
    subtitle = (
        f"run {tags.get('run_id', '?')} · {sha} · "
        f"{tags.get('ref', '?')} · {tags.get('runner_name', '?')}"
    )
    return title, subtitle


def get_n_cpus(loaded):
    for m in loaded.by_name.get("system", []):
        if "n_cpus" in m.get("fields", {}):
            return int(m["fields"]["n_cpus"])
    return None


# --------------------------------------------------------------- panel styling


def finish(ax, ylabel, title):
    ax.set_ylabel(ylabel, fontsize=9)
    ax.set_title(title, fontsize=10, loc="left", fontweight="bold")
    ax.grid(alpha=0.25, lw=0.5)
    ax.tick_params(labelsize=8)
    handles, _ = ax.get_legend_handles_labels()
    if handles:
        ax.legend(fontsize=7.5, loc="upper left", ncol=2, framealpha=0.85)
    ax.set_ylim(bottom=0)


def finish_twin(ax, ylabel):
    ax.set_ylabel(ylabel, fontsize=9)
    ax.tick_params(labelsize=8)
    handles, _ = ax.get_legend_handles_labels()
    if handles:
        ax.legend(fontsize=7.5, loc="upper right")


# --------------------------------------------------------------------- panels


def panel_cpu(ax, records, n_cpus):
    x, (user, system, iowait, steal) = xy_multi(
        records, ["usage_user", "usage_system", "usage_iowait", "usage_steal"]
    )
    if not x:
        return
    ax.stackplot(
        x,
        user,
        system,
        iowait,
        steal,
        labels=["user", "system", "iowait", "steal"],
        colors=["#4c78a8", "#f58518", "#e45756", "#b279a2"],
        alpha=0.9,
    )
    ax.set_ylim(0, 100)
    _, active = xy(records, "usage_active")
    peak_active = safe_max(active, default=0.0)
    peak_steal = safe_max(steal, default=0.0)
    ylabel = f"% of {n_cpus} vCPU" if n_cpus else "% CPU"
    title = f"CPU utilisation — peak {peak_active:.0f}%, steal max {peak_steal:.1f}%"
    finish(ax, ylabel, title)


def panel_pressure(ax, records):
    groups = group_by(records, lambda m: (m["tags"]["resource"], m["tags"]["type"]))
    wanted = [
        (("cpu", "some"), "#4c78a8"),
        (("io", "some"), "#e45756"),
        (("io", "full"), "#8b0000"),
        (("memory", "some"), "#54a24b"),
    ]
    maxima = {}
    for key, color in wanted:
        recs = groups.get(key)
        if not recs:
            continue
        x, y = xy(recs, "avg10")
        if not x:
            continue
        ax.plot(x, y, label=f"{key[0]}/{key[1]}", color=color, lw=1.1)
        maxima[key] = safe_max(y, default=0.0)
    title = (
        f"PSI pressure stall — cpu/some max {maxima.get(('cpu', 'some'), 0):.1f}%, "
        f"io/full max {maxima.get(('io', 'full'), 0):.1f}%, "
        f"memory/some max {maxima.get(('memory', 'some'), 0):.1f}%"
    )
    finish(ax, "% of last 10s stalled", title)


def panel_memory(ax, mem, swap):
    x, used = xy(mem, "used_percent")
    if x:
        ax.plot(x, used, color="#4c78a8", lw=1.2, label="memory used")
    x2, swap_used = xy(swap, "used_percent")
    if x2:
        ax.plot(x2, swap_used, color="#b279a2", lw=1.2, label="swap used")
    ax.set_ylim(0, 100)

    x3, avail = xy(mem, "available", 1 / 2**30)
    axb = ax.twinx()
    if x3:
        axb.plot(x3, avail, color="#54a24b", lw=1.0, ls="--", label="available GB")
    finish_twin(axb, "GB available")

    peak_used = safe_max(used, default=0.0)
    min_avail = safe_min(avail, default=0.0)
    title = f"Memory — peak {peak_used:.1f}% used, min {min_avail:.1f} GB available"
    finish(ax, "% used", title)


def panel_disk_usage(ax, records):
    x_pct, used_pct = xy(records, "used_percent")
    x_inodes, inodes_pct = xy(records, "inodes_used_percent")
    x_used, used_gb = xy(records, "used", 1 / 2**30)
    _, free_gb = xy(records, "free", 1 / 2**30)
    _, total_gb = xy(records, "total", 1 / 2**30)

    ax.plot(x_pct, used_pct, color="#e45756", lw=1.4, label="/ used %")
    if x_inodes:
        ax.plot(x_inodes, inodes_pct, color="#f58518", lw=1.2, label="/ inodes used %")
    top = min(100.0, safe_max(used_pct, default=0.0) * 1.25) or 100.0
    ax.set_ylim(0, top)

    total = total_gb[-1] if total_gb else 0.0
    if total > 0 and x_used:
        axd = ax.twinx()
        axd.plot(x_used, used_gb, color="0.4", lw=1.0, ls="--", label="GB used")
        axd.set_ylim(0, top / 100 * total)
        finish_twin(axd, f"GB used of {total:.0f}")

    start_gb = used_gb[0] if used_gb else 0.0
    end_gb = used_gb[-1] if used_gb else 0.0
    free_end = free_gb[-1] if free_gb else 0.0
    title = (
        f"Root filesystem — {start_gb:.1f} -> {end_gb:.1f} GB of {total:.0f} GB, "
        f"{free_end:.1f} GB free at the end"
    )
    finish(ax, "% used", title)


def panel_diskio(ax, by_device):
    axi = ax.twinx()
    for i, (name, records) in enumerate(sorted(by_device.items())):
        color = CYCLE[i % len(CYCLE)]
        x, read = rate(records, "read_bytes", 1 / 2**20)
        if x:
            ax.plot(x, read, color=color, lw=1.0, ls="-", label=f"{name} read MB/s")
        x2, write = rate(records, "write_bytes", 1 / 2**20)
        if x2:
            ax.plot(x2, write, color=color, lw=1.0, ls="--", label=f"{name} write MB/s")
        x3, busy = rate(records, "io_time", 0.1)
        if x3:
            axi.plot(x3, busy, color=color, lw=0.8, ls=":", label=f"{name} busy %")
    axi.set_ylim(0, 105)
    finish_twin(axi, "busy %")
    finish(ax, "MB/s", "Block I/O")


def panel_network(ax, by_interface, nstat):
    for i, (name, records) in enumerate(sorted(by_interface.items())):
        color = CYCLE[i % len(CYCLE)]
        x, rx = rate(records, "bytes_recv", 8 / 1e6)
        if x:
            ax.plot(x, rx, color=color, lw=1.0, ls="-", label=f"{name} rx Mbit/s")
        x2, tx = rate(records, "bytes_sent", 8 / 1e6)
        if x2:
            ax.plot(x2, tx, color=color, lw=1.0, ls="--", label=f"{name} tx Mbit/s")
    finish(ax, "Mbit/s", "Network")

    axn = ax.twinx()
    x3, retrans = rate(nstat, "TcpRetransSegs")
    if x3:
        axn.plot(x3, retrans, color="#e45756", lw=1.0, ls=":", label="TCP retrans/s")
    finish_twin(axn, "retrans/s")


def panel_load(ax, system, processes, n_cpus):
    x, load1 = xy(system, "load1")
    if x:
        ax.plot(x, load1, color="#4c78a8", lw=1.2, label="load1")
    x2, load5 = xy(system, "load5")
    if x2:
        ax.plot(x2, load5, color="#72b7b2", lw=1.0, label="load5")
    if n_cpus:
        ax.axhline(n_cpus, color="#e45756", ls="--", lw=0.9, label=f"n_cpus = {n_cpus}")

    x3, total = xy(processes, "total")
    x4, blocked = xy(processes, "blocked")
    axp = ax.twinx()
    if x3:
        axp.plot(x3, total, color="0.4", lw=1.0, ls=":", label="total procs")
    if x4:
        axp.plot(x4, blocked, color="#8b0000", lw=1.2, label="blocked (D)")
    finish_twin(axp, "process count")

    peak_load1 = safe_max(load1, default=0.0)
    peak_blocked = safe_max(blocked, default=0.0)
    n_label = f"{n_cpus} vCPU" if n_cpus else "unknown vCPU count"
    title = f"Load average vs {n_label} — peak load1 {peak_load1:.1f}; blocked peak {peak_blocked:.0f}"
    finish(ax, "load average", title)


def panel_process_memory(ax, by_pid):
    ordered = sorted(by_pid.items(), key=lambda kv: min(m["timestamp"] for m in kv[1]))
    for i, ((name, pid), records) in enumerate(ordered):
        x, rss = xy(records, "memory_rss", 1 / 2**20)
        if not x:
            continue
        is_collector = name == "telegraf"
        color = "0.45" if is_collector else CYCLE[i % len(CYCLE)]
        label = "telegraf (the collector)" if is_collector else f"{name}:{pid}"
        ax.plot(x, rss, lw=1.0 if is_collector else 1.2, color=color, ls="--" if is_collector else "-", label=label)
    finish(ax, "RSS (MB)", "Per-process memory")


def panel_lifelines(ax, by_pid):
    spans = lifeline_spans(by_pid)
    for i, (label, start, width) in enumerate(spans):
        ax.barh(i, width, left=start, height=0.6, color=CYCLE[i % len(CYCLE)], alpha=0.85)
        ax.text(start, i, f" {label}", va="center", fontsize=6.5, color="0.2", clip_on=True)
    ax.set_yticks([])
    ax.set_ylim(-1, max(len(spans), 1))
    ax.set_ylabel("tracked processes", fontsize=9)

    counts = defaultdict(int)
    for records in by_pid.values():
        for m in records:
            counts[m["timestamp"]] += 1
    axc = ax.twinx()
    xs = sorted(counts)
    if xs:
        axc.step(
            [ts_to_dt(t) for t in xs],
            [counts[t] for t in xs],
            where="post",
            color="#8b0000",
            lw=1.4,
            label="live count",
        )
    finish_twin(axc, "live count")

    ax.set_title(
        "Process lifelines (procstat, excluding telegraf/sudo)",
        fontsize=10,
        loc="left",
        fontweight="bold",
    )
    ax.grid(alpha=0.25, lw=0.5, axis="x")
    ax.tick_params(labelsize=8)


# --------------------------------------------------------------- assembly


def build_panels(loaded):
    panels = []
    n_cpus = get_n_cpus(loaded)

    cpu_total = [m for m in loaded.by_name.get("cpu", []) if m.get("tags", {}).get("cpu") == "cpu-total"]
    if cpu_total:
        panels.append(partial(panel_cpu, records=cpu_total, n_cpus=n_cpus))

    pressure = loaded.by_name.get("pressure", [])
    if pressure:
        panels.append(partial(panel_pressure, records=pressure))

    mem = loaded.by_name.get("mem", [])
    swap = loaded.by_name.get("swap", [])
    if mem or swap:
        panels.append(partial(panel_memory, mem=mem, swap=swap))

    disk_root = [m for m in loaded.by_name.get("disk", []) if m.get("tags", {}).get("path") == "/"]
    if disk_root:
        panels.append(partial(panel_disk_usage, records=disk_root))

    diskio_by_dev = {
        name: recs
        for name, recs in group_by(loaded.by_name.get("diskio", []), lambda m: m["tags"]["name"]).items()
        if is_whole_disk(name)
    }
    if diskio_by_dev:
        panels.append(partial(panel_diskio, by_device=diskio_by_dev))

    net_by_if = {
        name: recs
        for name, recs in group_by(loaded.by_name.get("net", []), lambda m: m["tags"]["interface"]).items()
        if is_primary_interface(name)
    }
    if net_by_if:
        panels.append(partial(panel_network, by_interface=net_by_if, nstat=loaded.by_name.get("nstat", [])))

    system = loaded.by_name.get("system", [])
    processes = loaded.by_name.get("processes", [])
    if system or processes:
        panels.append(partial(panel_load, system=system, processes=processes, n_cpus=n_cpus))

    procstat_by_key = group_by(
        loaded.by_name.get("procstat", []),
        lambda m: (m["tags"]["process_name"], m["fields"]["pid"]),
    )
    memory_procs = {k: v for k, v in procstat_by_key.items() if k[0] != "sudo"}
    if memory_procs:
        panels.append(partial(panel_process_memory, by_pid=memory_procs))

    lifeline_procs = {k: v for k, v in procstat_by_key.items() if k[0] not in ("sudo", "telegraf")}
    if lifeline_procs:
        panels.append(partial(panel_lifelines, by_pid=lifeline_procs))

    return panels


def render_dashboard(panels, title, subtitle, ts_min, ts_max, outfile):
    n = len(panels)
    if n == 0:
        print("plot: warning: no panels to draw (bundle produced no usable metrics)", file=sys.stderr)
        return False

    fig, axes_grid = plt.subplots(n, 1, figsize=(14, max(2.4 * n, 3)), sharex=True, squeeze=False)
    axes = [row[0] for row in axes_grid]

    for ax, panel in zip(axes, panels):
        panel(ax)

    x_start = ts_to_dt(ts_min)
    x_end = ts_to_dt(ts_max)
    if x_start == x_end:
        x_start -= timedelta(seconds=1)
        x_end += timedelta(seconds=1)
    axes[0].set_xlim(x_start, x_end)

    for ax in axes:
        locator = mdates.AutoDateLocator()
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    fig.suptitle(title, fontsize=13, fontweight="bold")
    fig.text(0.5, 0.965, subtitle, ha="center", fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(outfile, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return True


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="plot_telemetry.py",
        description="Render a runner-health dashboard from a flake0 collect bundle's metrics.json",
    )
    parser.add_argument("outdir", help="directory to write health.png into")
    parser.add_argument("metrics", nargs="+", help="one or more metrics*.json files")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    os.makedirs(args.outdir, exist_ok=True)

    loaded = load_metrics(args.metrics)
    if loaded.ts_min is None:
        print("plot: no metrics loaded from the given file(s)", file=sys.stderr)
        return 1

    panels = build_panels(loaded)
    title, subtitle = build_title(loaded)
    outfile = os.path.join(args.outdir, "health.png")
    if not render_dashboard(panels, title, subtitle, loaded.ts_min, loaded.ts_max, outfile):
        return 1
    print(f"plot: wrote {outfile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
