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
MATRIX = REPORT.with_name('run_matrix.py')
spec = importlib.util.spec_from_file_location('affinity_report', REPORT)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def check_matrix_plans():
    """Inspect commands/metadata only; fake CPU partitions never reach a binary."""
    matrix_spec = importlib.util.spec_from_file_location('affinity_matrix', MATRIX)
    matrix = importlib.util.module_from_spec(matrix_spec)
    matrix_spec.loader.exec_module(matrix)
    historical = ('real-baseline', 'legacy-four-pool-replay')
    generic = ('routing', 'signals', 'arrival', 'blocking', 'follow', 'granularity',
               'deployment', 'threads', 'overhead')
    check(matrix.cases(historical[0]) == matrix.cases(historical[1]),
          'historical replay aliases have different workloads')
    check({case['threads'] for _, case in matrix.cases('threads')} ==
          {'8', '16', '64', '128', '256'}, 'thread sweep removed a case or omitted 128')
    check({case['policy'] for _, case in matrix.cases('signals')} ==
          {'home', 'queue', 'active', 'outstanding', 'cpu', 'work'}, 'signal comparison incomplete')
    for suite in (*generic, 'cluster-baseline'):
        check(all('pool-count' not in change for _, change in matrix.cases(suite)),
              f'{suite} injects a pool count instead of using caller partitions')

    with tempfile.TemporaryDirectory(prefix='affinity-matrix-plans-') as temporary:
        root = Path(temporary)
        serial = 0

        def plan(suite, base, success=True):
            nonlocal serial
            serial += 1
            output = root / f'{serial}-{suite}'
            command = [sys.executable, str(MATRIX), '--suite', suite, '--output', str(output),
                       '--seeds', '101', '--base-args']
            command.extend(part for pair in base.items() for part in pair)
            process = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, timeout=10)
            if not success:
                check(process.returncode != 0, f'{suite}: invalid target plan accepted')
                check(not (output / 'plan.json').exists(), 'invalid target left a runnable plan')
                return None
            check(process.returncode == 0, f'{suite}: plan failed: {process.stderr}')
            metadata = json.loads((output / 'metadata.json').read_text())
            entries = json.loads((output / 'plan.json').read_text())
            options = {entry['case']: dict(zip(entry['command'][1::2], entry['command'][2::2]))
                       for entry in entries}
            role = ('target_cluster_baseline' if suite == 'cluster-baseline'
                    else 'historical_capture_replay' if suite in historical
                    else 'controlled_comparison')
            check(metadata['suite'] == suite and metadata['experiment_role'] == role,
                  f'{suite}: experiment role metadata does not describe the plan')
            check(metadata['research_target'] == 'one_pool_per_cluster',
                  f'{suite}: research target metadata missing')
            return options, process.stderr

        base = {'--cpus': '0;1;2;3;4;5', '--affinity': 'cluster',
                '--threads': '17', '--qps': '23'}
        for suite in generic:
            options, _ = plan(suite, base)
            for case in options.values():
                check('--pool-count' not in case and case['--cpus'] == base['--cpus'],
                      f'{suite}: generic plan changed resource partitions or injected four pools')
                check(case['--qps'] == '23', f'{suite}: generic plan replaced caller QPS')
                if suite != 'threads':
                    check(case['--threads'] == '17', f'{suite}: generic plan replaced caller thread count')

        for partitions, threads, qps, explicit_zero in (
                ('0;1', '17', '23', False), ('0;1;2;3;4;5', '17', '23', True),
                ('0;1;2', '128', '16', False)):
            target = {**base, '--cpus': partitions, '--threads': threads, '--qps': qps}
            if explicit_zero:
                target['--pool-count'] = '0'
            options, diagnostics = plan('cluster-baseline', target)
            check(not diagnostics, f'target baseline unexpectedly warned: {diagnostics}')
            check(set(options) == {'regular', 'poisson', 'burst4', 'burst8'},
                  'target baseline arrival coverage changed')
            for case in options.values():
                check(case['--policy'] == 'home' and case['--affinity'] == 'cluster',
                      'target baseline lost cluster home placement')
                check(case['--cpus'] == partitions and case.get('--pool-count', '0') == '0',
                      'target baseline no longer derives pool count from partitions')
                check(case['--threads'] == threads and case['--qps'] == qps,
                      'target baseline replaced explicitly chosen threads/QPS')

        for affinity in (None, 'none', 'numa'):
            target = dict(base)
            if affinity is None:
                del target['--affinity']
            else:
                target['--affinity'] = affinity
            plan('cluster-baseline', target, success=False)
        for count in ('1', '4', '6', '-1', 'invalid'):
            plan('cluster-baseline', {**base, '--pool-count': count}, success=False)
        for partitions in (None, '', ' ', ';0', '0;', '0;;1', '0; ;1'):
            target = dict(base)
            if partitions is None:
                del target['--cpus']
            else:
                target['--cpus'] = partitions
            plan('cluster-baseline', target, success=False)

        alias_plans = []
        for suite in historical:
            options, diagnostics = plan(suite, base)
            for case in options.values():
                check((case['--pool-count'], case['--threads'], case['--affinity'], case['--qps']) ==
                      ('4', '128', 'none', '16'), f'{suite}: historical deployment changed')
                check(case['--cpus'] == base['--cpus'], f'{suite}: historical replay changed CPU union')
                del case['--output']
            if suite == 'real-baseline':
                check('deprecated' in diagnostics and 'historical capture replay' in diagnostics
                      and 'cluster-baseline' in diagnostics, 'old ambiguous alias lacks deprecation guidance')
            else:
                check('deprecated' not in diagnostics, 'explicit historical alias was deprecated')
            alias_plans.append(options)
        check(alias_plans[0] == alias_plans[1], 'historical aliases produce different commands')
    print('affinity matrix: target cluster baseline, historical aliases, generic resource contracts and metadata passed')


def main(binary):
    check_matrix_plans()
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
                    ('active', {'policy': 'active'}), ('cpu', {'policy': 'cpu'}),
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

        # A batch arrives at b/qps intervals, independently of callback completion.
        burst = execute('burst-home', {'arrival': 'burst', 'burst-size': '4', 'burst-spacing-us': '0'})
        burst_other = execute('burst-outstanding', {'arrival': 'burst', 'burst-size': '4',
                                                  'burst-spacing-us': '0', 'policy': 'outstanding'})
        microburst = execute('microburst', {'arrival': 'burst', 'burst-size': '4', 'burst-spacing-us': '100'})
        for run, spacing in ((burst, 0), (burst_other, 0), (microburst, 100000)):
            actual = [row['scheduled_ns'] - run['summary']['origin_ns'] for row in run['requests']]
            expected = [(request // 4) * 80000000 + (request % 4) * spacing for request in range(8)]
            check(actual == expected, 'burst request schedule is not conserved')
            check(run['summary']['fixed_work_signature'] == baseline['summary']['fixed_work_signature'], 'arrival changed fixed work contract')
            check(task_work(run) == task_work(baseline), 'arrival changed actual work/checksums')
            check(run['summary']['arrival_hash'] != baseline['summary']['arrival_hash'], 'burst was mislabeled as regular arrival')
        for field in signatures:
            check(burst['summary'][field] == burst_other['summary'][field], f'burst policy changed {field}')
        check(burst['pairing_key'] != baseline['pairing_key'], 'different arrival schedules became timing pairs')

        # Retain callbacks while they sleep: CPU work/checksums stay fixed while
        # callback wall time includes the blocking interval. This is not an RPC.
        blocked_options = {'requests': '4', 'qps': '1000', 'widths': '4', 'depths': '1',
                           'iterations': '10', 'accesses': '0', 'jitter-percent': '0',
                           'long-every': '0', 'sync-block-percent': '100', 'sync-block-us': '10000'}
        blocked = [execute(f'blocked-{policy}', {**blocked_options, 'policy': policy})
                   for policy in ('home', 'active', 'cpu')]
        for run in blocked:
            check(run['summary']['planned_blocked_operators'] == 16, 'planned blocked operators changed')
            check(run['summary']['planned_sync_block_us'] == 160000, 'planned synchronous blocking changed')
            check(run['metrics']['sync_block_wall_ms'] >= 160, 'sleep time was not retained in callback wall time')
            check(run['metrics']['operator_cpu_wall_ratio'] < .5, 'blocking counterexample did not distinguish CPU from callback activity')
            check(task_work(run) == task_work(blocked[0]), 'blocking policy changed computational work')
            check(run['callback_occupancy'] is not None, 'callback occupancy not reconstructed')
            all_pools = run['callback_occupancy'][-1]
            check(all_pools['peak'] >= 2 and all_pools['mean'] > 0, 'blocking callback occupancy missing')
        for run in (results[5], blocked[2]):
            check(run['summary']['cpu_sampling'], 'cpu policy did not enable sampling')
            check(run['summary']['cpu_sampler']['scans'] >= 1, 'CPU sampler did not run')
            check(run['summary']['score_fallbacks'] <= run['summary']['score_evaluations'], 'fallback denominator includes unscored tasks')

        four_pools = execute('four-pools-none', {'affinity': 'none', 'pool-count': '4'})
        check(len(four_pools['summary']['groups']) == 4, 'pool-count did not decouple pools from partitions')
        check(all(group['cpus'] == cpus for group in four_pools['summary']['groups']), 'none changed the CPU union')
        check(task_work(four_pools) == task_work(baseline), 'pool count changed computational work')

        # A parent occupies the sole admission slot while submitting its fork.
        # Rejection must terminate the request and pool rather than lose a join.
        rejected = execute('reject', {'requests': '2', 'qps': '100', 'max-outstanding': '1'}, success=False)
        rejection = report.load_run(rejected)
        check(rejection['summary']['failed_or_rejected'] > 0, 'rejection was not recorded')
        check(not rejection['valid_for_comparison'], 'rejected run eligible for timing comparison')
        execute('invalid', {'depths': '0,2'}, success=False)
        execute('invalid-memory', {'branch-kb': '1048576'}, success=False)
        execute('invalid-estimate', {'estimate-mode': 'request-scale', 'work-scale-scope': 'task'}, success=False)
        execute('invalid-burst', {'arrival': 'burst', 'burst-size': '4', 'burst-spacing-us': '30000'}, success=False)
        execute('invalid-block', {'sync-block-percent': '50', 'sync-block-us': '0'}, success=False)
        execute('invalid-pool-count', {'affinity': 'cluster', 'pool-count': '4'}, success=False)
        execute('invalid-cpu-disabled', {'policy': 'cpu', 'cpu-sampling': '0'}, success=False)

        # The analysis retains a request whose finish lies beyond the arrival window.
        window = report.load_run(baseline['path'], 0, .000000001)
        check(len(window['completed_cohort']) == 1, 'arrival window excluded late completion')
        check(window['completed_cohort'][0]['finished_ns'] > window['completed_cohort'][0]['scheduled_ns'] + 1, 'test did not cover late completion')
        output = subprocess.run([sys.executable, str(REPORT), str(baseline['path']),
                                 str(results[2]['path'])], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=10)
        check(output.returncode == 0 and 'delta mean/p95/p99' in output.stdout, f'paired report failed: {output.stderr}')
        cpu_report = subprocess.run([sys.executable, str(REPORT), str(blocked[0]['path']),
                                     str(blocked[2]['path'])], text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=10)
        check(cpu_report.returncode == 0 and 'fallbacks/evaluated' in cpu_report.stdout
              and 'synchronous callback blocking' in cpu_report.stdout
              and 'CPU sampling differs from home' in cpu_report.stdout,
              f'CPU/blocking report missing evidence: {cpu_report.stderr}')

        # Simultaneous per-pool peaks need not coincide: global peak is rebuilt,
        # never obtained by adding individual maxima.
        occupancy = report.callback_occupancy([
            {'chosen': 0, 'start_ns': 0, 'finish_ns': 5},
            {'chosen': 1, 'start_ns': 5, 'finish_ns': 10}], 2, 0, 10)
        check(occupancy[-1]['peak'] == 1 and occupancy[-1]['mean'] == 1,
              'global callback occupancy incorrectly sums disjoint peaks')

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
        print('affinity functional: 22 complete runs, burst schedules, blocking counterexample, rejection/invalid input, work/checksum conservation and report checks passed')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: functional.py AFFINITY_DEMO_BINARY | --matrix-only')
    if sys.argv[1] == '--matrix-only':
        check_matrix_plans()
    else:
        main(sys.argv[1])
