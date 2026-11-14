#!/usr/bin/env python3
"""Small deterministic end-to-end checks; not a performance benchmark."""

import csv
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


REPORT = Path(__file__).resolve().parents[1] / 'report.py'
spec = importlib.util.spec_from_file_location('affinity_report', REPORT)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main(binary):
    cpus = sorted(os.sched_getaffinity(0))[:2]
    if len(cpus) < 2:
        print('affinity functional: skipped, requires two allowed logical CPUs')
        return
    binary = str(Path(binary).resolve())
    with tempfile.TemporaryDirectory(prefix='affinity-functional-') as temporary:
        root = Path(temporary)
        common = {'cpus': f'{cpus[0]};{cpus[1]}', 'threads': '2', 'requests': '8',
                  'qps': '50', 'max-inflight': '8', 'widths': '3,2', 'depths': '2,2',
                  'model': 'branch-rw', 'branch-kb': '4', 'iterations': '20000',
                  'accesses': '200', 'estimate-us': '500', 'jitter-percent': '25',
                  'long-every': '5', 'long-factor': '3', 'seed': '1234', 'memory-mb': '8'}

        def execute(name, updates=None, success=True):
            options = dict(common)
            options.update(updates or {})
            options['output'] = str(root / name)
            command = [binary]
            for key, value in options.items():
                command.extend((f'--{key}', str(value)))
            process = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, timeout=20)
            if success and process.returncode:
                raise AssertionError(f'{name}: {process.stderr[-3000:]}')
            if not success:
                check(process.returncode != 0, f'{name}: expected rejection/error')
                return root / name
            return report.load_run(root / name)

        variants = [('home', {}), ('queue', {'policy': 'queue'}),
                    ('outstanding', {'policy': 'outstanding'}), ('work', {'policy': 'work'}),
                    ('return-home', {'policy': 'outstanding', 'continuation': 'home'}),
                    ('chunk-two', {'policy': 'outstanding', 'batch-chunk': '2'}),
                    ('no-trace', {'trace': '0'}), ('unbound', {'affinity': 'none'}),
                    ('numa', {'affinity': 'numa'})]
        results = [execute(name, updates) for name, updates in variants]
        baseline = results[0]
        signatures = ('checksum', 'arrival_hash', 'work_signature', 'completed', 'requests', 'nodes_per_request')
        for run in results:
            for field in signatures:
                check(run['summary'][field] == baseline['summary'][field], f'{run["path"]}: changed {field}')
            check(run['summary']['failed_or_rejected'] == 0, 'unexpected rejection')
            check([row['checksum'] for row in run['requests']] == [row['checksum'] for row in baseline['requests']], 'request checksum differs')
            if not run['summary']['trace']:
                check(not (run['path'] / 'tasks.csv').exists(), 'trace=0 unexpectedly writes tasks')
                continue
            check(len(run['tasks']) == 8 * 13, 'task count not conserved')
            for row in run['tasks']:
                if row['kind'] != 'operator':
                    check(row['iterations'] == row['read_words'] == row['write_words'] == 0, 'non-operator work counters')
                    continue
                random = report.mix(1234 ^ report.mix(row['request']) ^ report.mix(row['tag']))
                percent = 75 + random % 51
                factor = 3 if random % 5 == 0 else 1
                check(row['iterations'] == 20000 * percent // 100 * factor, 'compute work changed')
                check(row['read_words'] == row['write_words'] == 200 * percent // 100 * factor, 'data work changed')
                if run['summary']['policy'] == 'home':
                    check(row['chosen'] == row['home'], 'home policy moved a task')
            work = lambda item: sorted((row['request'], row['tag'], row['iterations'], row['read_words'], row['write_words'], row['checksum']) for row in item['tasks'])
            check(work(run) == work(baseline), 'policy/affinity changed actual data work')

        # Open arrival streams retain the same schedule independent of placement.
        poisson = execute('poisson-home', {'arrival': 'poisson', 'qps': '100'})
        poisson_other = execute('poisson-outstanding', {'arrival': 'poisson', 'qps': '100', 'policy': 'outstanding'})
        for field in signatures:
            check(poisson['summary'][field] == poisson_other['summary'][field], f'Poisson changed {field}')

        # All operators in one request share the same heterogeneous work scale;
        # changing placement or its request-level estimate must not alter work.
        heterogeneous_options = {'work-scale-scope': 'request', 'estimate-mode': 'request-scale',
                                 'long-every': '2', 'long-factor': '3', 'jitter-percent': '20'}
        heterogeneous = execute('heterogeneous-home', heterogeneous_options)
        heterogeneous_work = execute('heterogeneous-work', {**heterogeneous_options, 'policy': 'work'})
        for field in signatures:
            check(heterogeneous['summary'][field] == heterogeneous_work['summary'][field], f'heterogeneous request changed {field}')
        for run in (heterogeneous, heterogeneous_work):
            check(run['summary']['work_contract']['work_scale_scope'] == 'request', 'request work scope missing')
            check(run['summary']['routing']['estimate_mode'] == 'request-scale', 'request estimate mode missing')
            for row in run['tasks']:
                if row['kind'] != 'operator':
                    continue
                random = report.mix(1234 ^ report.mix(row['request']) ^ report.mix(1))
                percent = 80 + random % 41
                factor = 3 if random % 2 == 0 else 1
                check(row['iterations'] == 20000 * percent // 100 * factor, 'request-scaled compute work changed')
                check(row['read_words'] == row['write_words'] == 200 * percent // 100 * factor, 'request-scaled memory work changed')
        task_work = lambda item: sorted((row['request'], row['tag'], row['iterations'], row['read_words'], row['write_words'], row['checksum']) for row in item['tasks'])
        check(task_work(heterogeneous) == task_work(heterogeneous_work), 'request-scaled policy changed actual work')

        # A parent occupies the sole admission slot while submitting its fork.
        # Rejection must terminate the request and pool rather than lose a join.
        rejected = execute('reject', {'requests': '2', 'qps': '100', 'max-outstanding': '1'}, success=False)
        rejection = report.load_run(rejected)
        check(rejection['summary']['failed_or_rejected'] > 0, 'rejection was not recorded')
        check(not rejection['valid_for_comparison'], 'rejected run eligible for timing comparison')
        execute('invalid', {'depths': '0,2'}, success=False)
        execute('invalid-memory', {'branch-kb': '1048576'}, success=False)
        execute('invalid-estimate', {'estimate-mode': 'request-scale', 'work-scale-scope': 'task'}, success=False)

        # The analysis retains a request whose finish lies beyond the arrival window.
        window = report.load_run(baseline['path'], 0, .000000001)
        check(len(window['completed_cohort']) == 1, 'arrival window excluded late completion')
        check(window['completed_cohort'][0]['finished_ns'] > window['completed_cohort'][0]['scheduled_ns'] + 1, 'test did not cover late completion')
        output = subprocess.run([sys.executable, str(REPORT), str(baseline['path']),
                                 str(results[2]['path'])], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=10)
        check(output.returncode == 0 and 'delta mean/p95/p99' in output.stdout, f'paired report failed: {output.stderr}')

        corrupted = root / 'corrupted'
        shutil.copytree(baseline['path'], corrupted)
        with (corrupted / 'tasks.csv').open(newline='') as stream:
            rows = list(csv.reader(stream))
        with (corrupted / 'tasks.csv').open('w', newline='') as stream:
            csv.writer(stream).writerows(rows[:-1])
        try:
            report.load_run(corrupted)
        except ValueError:
            pass
        else:
            raise AssertionError('truncated trace silently accepted')
        print('affinity functional: 13 complete runs, rejection/invalid input, work/checksum conservation, windows and report checks passed')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: functional.py AFFINITY_DEMO_BINARY')
    main(sys.argv[1])
