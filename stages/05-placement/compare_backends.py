#!/usr/bin/env python3
"""Check the two packaging choices with the same work, not equal race-dependent placement."""
import argparse
import csv
import json
from pathlib import Path
import subprocess
import tempfile


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--business",required=True)
    p.add_argument("--library",required=True)
    p.add_argument("--output",type=Path)
    a=p.parse_args()
    owned=tempfile.TemporaryDirectory(prefix="placement-compare-") if a.output is None else None
    root=Path(owned.name) if owned else a.output
    root.mkdir(parents=True,exist_ok=True)
    results=[]
    for policy in ("home","borrow","sticky","fanout","least-load"):
        signatures=[]
        for label,binary in (("business",a.business),("folly",a.library)):
            out=root/(policy+"-"+label)
            cmd=[str(Path(binary).resolve()),"--output",str(out),"--pools","2","--threads","2",
                 "--requests","8","--duration","1","--qps","100","--fanout-min","4","--fanout-max","4",
                 "--chain-length","2","--cpu-us","150","--join-us","30","--rpc-prob","0.3","--rpc-delay-us","500",
                 "--policy",policy,"--seed","871","--sample-ms","10","--drain-seconds","10"]
            completed=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
            if completed.returncode:raise RuntimeError(completed.stdout+completed.stderr)
            summary=json.loads((out/"summary.json").read_text())
            metadata=json.loads((out/"metadata.json").read_text())
            expected_backend="business" if label=="business" else "folly_experimental"
            if metadata.get("backend")!=expected_backend:
                raise AssertionError(f"wrong backend binary: expected {expected_backend}, got {metadata.get('backend')}")
            if not summary["conservation_ok"] or summary["completed"]!=8 or summary["failed"] or summary["censored"]:
                raise AssertionError(summary)
            with (out/"tasks.csv").open() as f:tasks=list(csv.DictReader(f))
            signatures.append(sorted((t["request_id"],t["node_id"],t["stage"],t["work_target_us"]) for t in tasks))
            if policy=="home" and any(t["chosen_group"]!=t["home_group"] for t in tasks):
                raise AssertionError("home invariant")
            results.append({"policy":policy,"backend":label,"metadata_backend":metadata.get("backend"),
                            "completed":summary["completed"],"cpu_stages":len(tasks),"command":cmd})
        if signatures[0]!=signatures[1]:raise AssertionError("different seeded workload")
    (root/"comparison.json").write_text(json.dumps({"passed":True,"runs":results,
        "meaning":"same workload and API invariants; dynamic routing/timing need not match"},indent=2)+"\n")
    print("PASS: business/Folly packaging, 5 policies, 10 runs, equal work and completion invariants")
    if owned:owned.cleanup()


if __name__=="__main__":main()
