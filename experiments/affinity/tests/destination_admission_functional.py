#!/usr/bin/env python3
"""Small real-executor admission checks; artificial CPU groups, no timing assertions."""
import csv
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def run(binary):
    cpus = sorted(os.sched_getaffinity(0))[:2]
    if len(cpus) < 2:
        raise RuntimeError('functional check requires two allowed logical CPUs')
    print(f'Artificial CPU groups {cpus}: 1 worker/pool. Functional checks only.')
    report, flow, phase = module('report'), module('flow_report'), module('request_phase_overlap')
    base = ['--cpus', ';'.join(map(str, cpus)), '--threads', '1', '--capacity-scale', '1',
            '--affinity', 'cluster', '--requests', '4', '--qps', '100', '--max-inflight', '16',
            '--widths', '10,3', '--depths', '2,3', '--iterations', '10000', '--accesses', '100',
            '--branch-kb', '4', '--shared-kb', '0', '--model', 'branch-rw', '--memory-mb', '64',
            '--jitter-percent', '0', '--long-every', '0', '--seed', '101', '--policy', 'outstanding',
            '--busy-high', '0', '--return-low', '0', '--min-gain', '0', '--max-hops', '4',
            '--cross-numa', '0', '--continuation', 'parent', '--balance-at', 'all']
    cases = [('off', '0', 1, 1), ('blocked', '0.01', 1, 1),
             ('one', '1', 1, 1), ('chunk', '1', 3, 1), ('trace0', '0.01', 1, 0)]
    with tempfile.TemporaryDirectory(prefix='destination-admission-') as temporary:
        results = []
        for label, limit, chunk, trace in cases:
            path = Path(temporary) / label
            command = [str(binary), *base, '--target-limit', limit, '--batch-chunk', str(chunk),
                       '--trace', str(trace), '--output', str(path)]
            process = subprocess.run(command, text=True, capture_output=True, timeout=20)
            if process.returncode:
                raise AssertionError(f'{label}: {process.stderr}')
            summary = json.loads((path / 'summary.json').read_text())
            assert summary['completed'] == 4 and summary['failed_or_rejected'] == 0
            assert summary['routing']['target_limit'] == float(limit)
            admission = summary['target_admission']
            assert admission['enabled'] == (float(limit) > 0)
            assert admission['limit'] == float(limit)
            assert admission['policy'] == ('disabled' if limit == '0' else 'outstanding-after')
            counters = admission['stats']
            assert all(type(v) is int and v >= 0 for v in counters.values())
            assert sum(admission['decision_reasons'].values()) == counters['selections']
            assert counters['evaluated_selections'] <= counters['selections']
            if limit == '0':
                assert all(v == 0 for v in counters.values()), 'disabled path maintained active admission counters'
            else:
                assert counters['selections'] > 0, 'trace-independent statistics absent'
                assert counters['candidate_checks'] > 0
            loaded = report.load_run(path, 0, None)
            assert loaded['valid_for_comparison'] and loaded['metrics']['n'] == 4
            if trace:
                roles = flow.task_roles(summary, loaded['tasks'])
                assert sum(row['n'] for row in roles.values()) == summary['observed_tasks']
                assert phase.analyze(loaded)['cohort_requests'] == 4
                with (path / 'tasks.csv').open() as handle:
                    tasks = list(csv.DictReader(handle))
                assert all(int(row['selection_batch_tasks']) in range(1, chunk + 1) for row in tasks)
                if label == 'blocked':
                    assert summary['migrations'] == 0, 'no target can fit even one task at limit .01'
                    assert counters['no_eligible_target'] > 0
                for row in tasks:
                    if int(row['admission_evaluated']):
                        assert 0 <= float(row['admission_before']) <= float(row['admission_after'])
                        assert float(row['admission_after']) <= float(limit) + 1e-9
                    else:
                        assert row['admission_before'] == row['admission_after'] == '', 'unevaluated pressure must be absent'
            results.append(summary)
            print(f'PASS {label}: completed=4/4, target_limit={limit}, chunk={chunk}, trace={trace}')
        for field in ('fixed_work_signature', 'work_signature', 'planned_total_iterations',
                      'planned_total_accesses', 'checksum', 'arrival_hash'):
            assert len({str(s[field]) for s in results}) == 1, f'changed business contract: {field}'
        for value in ('-1', 'nan', 'inf'):
            path = Path(temporary) / ('invalid-' + value)
            process = subprocess.run([str(binary), *base, '--target-limit', value,
                                      '--output', str(path)], capture_output=True, text=True, timeout=10)
            assert process.returncode != 0 and not (path / 'summary.json').exists()
        print('PASS invalid thresholds; fixed work/checksums; trace0 statistics; existing report/flow/phase analyzers')


if __name__ == '__main__':
    run(Path(sys.argv[1]).resolve())
