#!/usr/bin/env python3
"""Read topology evidence without assuming CPU numbering or SMT capacity."""
import argparse
import glob
import json
import os
import pathlib
import platform
import subprocess


def read(path):
    try:
        return pathlib.Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return None


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
    cg_path = None
    if cgroup:
        for line in cgroup.splitlines():
            if line.startswith("0::"):
                cg_path = "/sys/fs/cgroup" + line[3:]
    ancestors = []
    if cg_path:
        path = pathlib.Path(cg_path)
        root = pathlib.Path("/sys/fs/cgroup")
        while path == root or root in path.parents:
            ancestors.append({"path": str(path), **{key: read(path / key) for key in
                ("cpu.max", "cpu.stat", "cpuset.cpus.effective", "cpuset.mems.effective", "memory.max")}})
            if path == root:
                break
            path = path.parent
    try:
        lscpu = subprocess.run(["lscpu", "-J"], capture_output=True, text=True, check=True).stdout
        lscpu = json.loads(lscpu)
    except (OSError, ValueError, subprocess.SubprocessError):
        lscpu = None
    return {"platform": platform.platform(), "allowed_cpus": cpus, "cpus": cpu_info,
            "numa_nodes": {pathlib.Path(d).name: read(f"{d}/cpulist")
                for d in sorted(glob.glob("/sys/devices/system/node/node[0-9]*"))},
            "cgroup": cgroup, "cgroup_ancestors": ancestors, "lscpu": lscpu,
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
