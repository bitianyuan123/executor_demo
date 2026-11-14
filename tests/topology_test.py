#!/usr/bin/env python3
"""Fixture tests for cgroup v1/v2 mount and ancestor discovery."""
import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("topology", ROOT / "scripts" / "topology.py")
TOPOLOGY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOPOLOGY)


def mount(root, point, controllers, kind="cgroup"):
    return f"42 23 0:30 {root} {point} rw,nosuid,nodev,noexec,relatime - {kind} cgroup rw,{controllers}"


class CgroupTopologyTest(unittest.TestCase):
    def test_v1_controllers_have_independent_memberships_and_ancestor_limits(self):
        membership = "3:cpu,cpuacct:/service/job\n7:cpuset:/\n13:memory:/service\n11:pids:/service"
        mounts = "\n".join(mount("/", f"/cg/{controller}", controller)
                           for controller in ("cpu,cpuacct", "cpuset", "memory", "pids"))
        files = {"/cg/cpu,cpuacct/service/job/cpu.cfs_quota_us": "-1",
                 "/cg/cpu,cpuacct/service/cpu.cfs_quota_us": "200000",
                 "/cg/cpu,cpuacct/service/cpu.cfs_period_us": "100000",
                 "/cg/cpuset/cpuset.cpus": "0-383",
                 "/cg/memory/service/memory.limit_in_bytes": "4294967296",
                 "/cg/pids/service/pids.max": "1000"}
        result = TOPOLOGY.capture_cgroups(membership, mounts, lambda path: files.get(str(path)))
        self.assertEqual(result["cgroup_version"], "v1")
        self.assertEqual(result["cgroup_ancestors"], [])
        cpu, cpuset, memory, pids = result["cgroup_hierarchies"]
        self.assertEqual([row["path"] for row in cpu["ancestors"]],
                         ["/cg/cpu,cpuacct/service/job", "/cg/cpu,cpuacct/service", "/cg/cpu,cpuacct"])
        self.assertEqual(cpu["ancestors"][0]["cpu.cfs_quota_us"], "-1")
        self.assertEqual(cpu["ancestors"][1]["cpu.cfs_quota_us"], "200000")
        self.assertEqual(cpuset["ancestors"][0]["cpuset.cpus"], "0-383")
        self.assertEqual(memory["ancestors"][0]["memory.limit_in_bytes"], "4294967296")
        self.assertEqual(pids["ancestors"][0]["pids.max"], "1000")

    def test_mount_root_is_decoded_and_bounds_traversal(self):
        seen = []

        def reader(path):
            seen.append(str(path))
            return None

        result = TOPOLOGY.capture_cgroups("3:cpu:/tenant one/job",
            mount(r"/tenant\040one", r"/visible\040cpu", "cpu"), reader)
        item = result["cgroup_hierarchies"][0]
        self.assertEqual(item["resolved_path"], "/visible cpu/job")
        self.assertEqual(item["mount_root"], "/tenant one")
        self.assertTrue(item["hidden_ancestors"])
        self.assertEqual([row["path"] for row in item["ancestors"]], ["/visible cpu/job", "/visible cpu"])
        self.assertTrue(all(path.startswith("/visible cpu/") for path in seen))

    def test_namespace_mismatch_is_unknown_without_guessing(self):
        result = TOPOLOGY.capture_cgroups("3:cpu:/", mount("/tenant", "/cg", "cpu"),
                                         lambda path: self.fail(f"unexpected read: {path}"))
        item = result["cgroup_hierarchies"][0]
        self.assertEqual(item["status"], "membership-outside-visible-mount")
        self.assertEqual(item["ancestors"], [])
        self.assertIsNone(item["hidden_ancestors"])

    def test_path_traversal_is_not_resolved(self):
        result = TOPOLOGY.capture_cgroups("3:cpu:/a/../../secret", mount("/", "/cg", "cpu"),
                                         lambda path: self.fail(f"unexpected read: {path}"))
        self.assertEqual(result["cgroup_hierarchies"][0]["status"], "membership-outside-visible-mount")

    def test_bind_mount_choice_exposes_outer_ancestors(self):
        mounts = "\n".join([mount("/tenant", "/nested", "cpu"), mount("/", "/full", "cpu")])
        result = TOPOLOGY.capture_cgroups("3:cpu:/tenant/job", mounts, lambda path: None)
        item = result["cgroup_hierarchies"][0]
        self.assertEqual(item["mount_point"], "/full")
        self.assertFalse(item["hidden_ancestors"])
        self.assertEqual(item["ancestors"][-1]["path"], "/full")

    def test_v2_keeps_legacy_fields_and_missing_values_unknown(self):
        files = {"/unified/job/cpu.max": "max 100000", "/unified/cpu.max": "400000 100000"}
        result = TOPOLOGY.capture_cgroups("0::/job", mount("/", "/unified", "", kind="cgroup2"),
                                         lambda path: files.get(str(path)))
        self.assertEqual(result["cgroup_version"], "v2")
        item = result["cgroup_hierarchies"][0]
        self.assertEqual(result["cgroup_ancestors"], item["ancestors"])
        self.assertEqual(item["ancestors"][1]["cpu.max"], "400000 100000")
        self.assertIsNone(item["ancestors"][0]["memory.max"])
        self.assertIn("memory.max", item["ancestors"][0]["unreadable_or_missing"])
        self.assertNotIn("cpu.max", item["ancestors"][0]["unreadable_or_missing"])

    def test_hybrid_and_unmounted_controllers(self):
        result = TOPOLOGY.capture_cgroups("0::/job\n3:cpu:/job\n11:pids:/job",
                                         mount("/", "/unified", "", kind="cgroup2"), lambda path: None)
        self.assertEqual(result["cgroup_version"], "hybrid")
        self.assertEqual([item["status"] for item in result["cgroup_hierarchies"]],
                         ["resolved", "mount-not-found", "mount-not-found"])

    def test_missing_or_malformed_proc_data(self):
        result = TOPOLOGY.capture_cgroups(None, None, lambda path: self.fail(f"unexpected read: {path}"))
        self.assertEqual(result["cgroup_version"], "unknown")
        self.assertEqual(result["cgroup_hierarchies"], [])
        self.assertEqual(TOPOLOGY.parse_cgroup_mounts("malformed\nnot a mount - cgroup"), [])
        self.assertEqual(TOPOLOGY.parse_cgroup_memberships("oops\na:b:c\n3:cpu"), [])


if __name__ == "__main__":
    unittest.main()
