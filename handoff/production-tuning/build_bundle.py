#!/usr/bin/env python3
"""Build a small, deterministic, allowlisted offline handoff (stdlib only)."""

import argparse
import gzip
import hashlib
import io
from pathlib import Path
import tarfile


FILES = [
    "README.md", "GUIDE.md", "EVIDENCE.md", "WORKSHEET.md", "build_bundle.py",
    "prompts/01_DISCOVER.md", "prompts/02_DESIGN.md", "prompts/03_IMPLEMENT.md",
    "prompts/04_VALIDATE.md", "prompts/05_ITERATE.md",
    "references/README.md", "references/LoadRouting.h",
    "evidence/3005.log", "evidence/policy_summary.csv", "evidence/decomposition.csv",
    "evidence/comparisons.csv", "evidence/buckets.csv", "evidence/initial_spans.csv",
    "evidence/cpu_procstat.csv", "evidence/cpu_trace_scopes.csv",
    "evidence/cpu_trace_aligned_windows.csv", "evidence/cpu_trace_observer.csv",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    output = args.output.resolve()
    if output == root or root in output.parents:
        parser.error("output must be outside the handoff directory")
    sidecar = Path(str(output) + ".sha256")
    if output.exists() or sidecar.exists():
        parser.error("output or checksum already exists; choose a new path")
    payload = {}
    for name in sorted(FILES):
        path = root / name
        if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
            parser.error(f"missing or unsafe input: {name}")
        payload[name] = path.read_bytes()
    manifest = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        for name, data in payload.items()
    ).encode()
    (root / "SHA256SUMS").write_bytes(manifest)
    payload["SHA256SUMS"] = manifest
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, data in sorted(payload.items()):
                info = tarfile.TarInfo("production-tuning/" + name)
                info.size = len(data)
                info.mode = 0o644
                info.mtime = 0
                archive.addfile(info, io.BytesIO(data))
    data = buffer.getvalue()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        stream.write(data)
    sidecar.write_text(f"{hashlib.sha256(data).hexdigest()}  {output.name}\n")
    print(f"Created {output} ({len(data)} bytes, {len(payload)} files)")
    print(f"Archive checksum: {sidecar}")
    print("Local SHA256SUMS refreshed; no network access or application changes.")


if __name__ == "__main__":
    main()
