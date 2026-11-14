#!/usr/bin/env python3
"""Test output validity/option handling, not machine-specific timing assertions."""
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile

binary=str(Path(sys.argv[1]).resolve())
help_result=subprocess.run([binary,'--help'],capture_output=True,text=True,timeout=5)
assert help_result.returncode==0 and 'metric_cost_bench --output' in help_result.stdout
with tempfile.TemporaryDirectory() as directory:
    out=Path(directory)/'probe'
    command=[binary,'--output',str(out),'--threads','2','--samples','2','--batch','2','--operations','1000','--repeats','1']
    result=subprocess.run(command,capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert json.loads((out/'metadata.json').read_text())['completed']
    with (out/'reads.csv').open() as f:reads=list(csv.DictReader(f))
    with (out/'updates.csv').open() as f:updates=list(csv.DictReader(f))
    assert len(reads)==16 and len(updates)==9
    assert {r['state'] for r in reads}=={'idle','cpu_busy_with_backlog'}
    assert all(int(r['final_count'])==int(r['producers'])*1000 for r in updates)
    repeated=subprocess.run(command,capture_output=True,text=True,timeout=5)
    assert repeated.returncode!=0 and 'already exists' in repeated.stderr
print('PASS: metric probe CLI, read schema, update conservation, overwrite protection')
