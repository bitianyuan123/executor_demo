#!/usr/bin/env python3
"""Read topology evidence without assuming CPU numbering or SMT capacity."""
import argparse
import glob
import json
import os
import pathlib
import platform
import re
import subprocess


def read(path):
    try:
        return pathlib.Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return None


def parse_cgroup_memberships(value):
    """Keep controller memberships separate: v1 controllers can use different paths."""
    memberships = []
    for line in (value or "").splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3 or not fields[0].isdigit():
            continue
        memberships.append({"version": 2 if fields[0] == "0" and not fields[1] else 1,
                            "controllers": fields[1].split(",") if fields[1] else [],
                            "membership_path": fields[2]})
    return memberships


def parse_cgroup_mounts(value):
    """Decode mountinfo escapes after tokenizing; spaces may occur in cgroup roots."""
    def unescape(text):
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), text)

    mounts = []
    for line in (value or "").splitlines():
        left, separator, right = line.partition(" - ")
        before, after = left.split(), right.split()
        if not separator or len(before) < 6 or len(after) < 3:
            continue
        if after[0] not in ("cgroup", "cgroup2"):
            continue
        mounts.append({"version": 2 if after[0] == "cgroup2" else 1,
                       "mount_root": unescape(before[3]),
                       "mount_point": unescape(before[4]),
                       "controllers": sorted(set(after[2].split(",")) | set(after[1].split(",")))})
    return mounts


def _cgroup_location(membership, mount):
    """Resolve only paths demonstrably inside the mount's exposed hierarchy."""
    paths = [pathlib.PurePosixPath(value) for value in
             (membership["membership_path"], mount["mount_root"], mount["mount_point"])]
    if any(not path.is_absolute() or ".." in path.parts for path in paths):
        return None
    member, root, point = paths
    try:
        relative = member.relative_to(root)
    except ValueError:
        # Namespace or inaccessible ancestor mismatch: do not guess a filesystem path.
        return None
    return pathlib.Path(point, relative)


def capture_cgroups(cgroup_text, mountinfo_text, reader=read):
    memberships = parse_cgroup_memberships(cgroup_text)
    mounts = parse_cgroup_mounts(mountinfo_text)
    versions = {item["version"] for item in memberships}
    version = "hybrid" if len(versions) > 1 else (f"v{next(iter(versions))}" if versions else "unknown")
    relevant = {"cpu", "cpuacct", "cpuset", "memory", "pids"}
    v1_fields = {
        "cpu": ("cpu.cfs_quota_us", "cpu.cfs_period_us", "cpu.cfs_burst_us", "cpu.stat", "cpu.shares"),
        "cpuacct": ("cpuacct.usage", "cpuacct.stat"),
        "cpuset": ("cpuset.cpus", "cpuset.mems", "cpuset.effective_cpus", "cpuset.effective_mems"),
        "memory": ("memory.limit_in_bytes", "memory.usage_in_bytes", "memory.use_hierarchy", "memory.failcnt"),
        "pids": ("pids.max", "pids.current", "pids.events"),
    }
    v2_fields = ("cpu.max", "cpu.stat", "cpu.weight", "cpuset.cpus", "cpuset.mems",
                 "cpuset.cpus.effective", "cpuset.mems.effective", "memory.max",
                 "memory.current", "pids.max", "pids.current", "pids.events")
    hierarchies = []
    legacy_ancestors = []
    for membership in memberships:
        controllers = set(membership["controllers"])
        if membership["version"] == 1 and not controllers & relevant:
            continue
        candidates = [mount for mount in mounts if mount["version"] == membership["version"]
                      and (membership["version"] == 2 or controllers <= set(mount["controllers"]))]
        accessible = [(mount, _cgroup_location(membership, mount)) for mount in candidates]
        accessible = [(mount, path) for mount, path in accessible if path is not None]
        record = {**membership, "ancestors": [], "mount_point": None, "mount_root": None,
                  "resolved_path": None, "hidden_ancestors": None}
        if not accessible:
            record["status"] = "membership-outside-visible-mount" if candidates else "mount-not-found"
            hierarchies.append(record)
            continue
        # A hierarchy can have bind mounts. Prefer the one exposing the most ancestors.
        mount, path = min(accessible, key=lambda entry: (len(pathlib.PurePosixPath(entry[0]["mount_root"]).parts),
                                                       entry[0]["mount_point"]))
        record.update({key: mount[key] for key in ("mount_point", "mount_root")})
        record.update(resolved_path=str(path), hidden_ancestors=mount["mount_root"] != "/", status="resolved")
        fields = v2_fields if membership["version"] == 2 else tuple(
            field for controller in sorted(controllers & relevant) for field in v1_fields[controller])
        boundary = pathlib.Path(mount["mount_point"])
        while path == boundary or boundary in path.parents:
            values = {key: reader(path / key) for key in fields}
            record["ancestors"].append({"path": str(path), **values,
                                        "unreadable_or_missing": [key for key, value in values.items() if value is None]})
            if path == boundary:
                break
            path = path.parent
        if membership["version"] == 2:
            # Preserve the original field used by existing result readers.
            legacy_ancestors = record["ancestors"]
        hierarchies.append(record)
    return {"cgroup_version": version, "cgroup_ancestors": legacy_ancestors,
            "cgroup_hierarchies": hierarchies,
            "cgroup_limit_note": "Missing/unreadable values are unknown, not unlimited. Ancestors above a mount root are not observable; namespace mappings are not guessed."}


def capture():
    cpus = sorted(os.sched_getaffinity(0))
    cpu_info = []
    for cpu in cpus:
        base = f"/sys/devices/system/cpu/cpu{cpu}"
        topology = {key: read(f"{base}/topology/{key}") for key in (
            "physical_package_id", "die_id", "cluster_id", "cluster_cpus_list",
            "core_id", "core_cpus_list", "thread_siblings_list")}
        caches = [{key: read(f"{directory}/{key}") for key in
                   ("level", "type", "id", "shared_cpu_list", "size")}
                  for directory in sorted(glob.glob(f"{base}/cache/index*"))]
        cpu_info.append({"cpu": cpu, "topology": topology, "caches": caches})
    cgroup = read("/proc/self/cgroup")
    cgroups = capture_cgroups(cgroup, read("/proc/self/mountinfo"))
    try:
        lscpu = subprocess.run(["lscpu", "-J"], capture_output=True, text=True, check=True).stdout
        lscpu = json.loads(lscpu)
    except (OSError, ValueError, subprocess.SubprocessError):
        lscpu = None
    return {"platform": platform.platform(), "allowed_cpus": cpus, "cpus": cpu_info,
            "numa_nodes": {pathlib.Path(d).name: read(f"{d}/cpulist")
                for d in sorted(glob.glob("/sys/devices/system/node/node[0-9]*"))},
            "cgroup": cgroup, **cgroups, "lscpu": lscpu,
            "sched_schedstats": read("/proc/sys/kernel/sched_schedstats"),
            "perf_event_paranoid": read("/proc/sys/kernel/perf_event_paranoid"),
            "warning": "Topology is OS/VM reported; cluster is not necessarily LLC; SMT siblings do not double capacity."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    args = parser.parse_args()
    data = json.dumps(capture(), indent=2)
    if args.output:
        output = pathlib.Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(data + "\n", encoding="utf-8")
    else:
        print(data)
