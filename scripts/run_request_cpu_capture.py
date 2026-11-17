#!/usr/bin/env python3
"""Plan or execute one bounded sched_switch capture; never change tracefs/sysctl.

Default is plan-only, with no writes and no workload. --run records every
sched_switch on the specified CPU union, then invokes the offline request
profiler. This is a diagnostic run, not a new performance comparison matrix.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
BOUNDARY_HELPER = REPO / 'scripts/request_cpu_capture_workload.py'
DEFAULT_CPUS = '0-15;16-31;32-47;192-207;208-223;224-239'
TIMEOUT_SECONDS = 120


def partitions(value):
    groups, seen = [], set()
    for group in value.split(';'):
        cpus = set()
        for token in group.split(','):
            match = re.fullmatch(r'(\d+)(?:-(\d+))?', token.strip())
            if not match:
                raise ValueError('CPU partitions require decimal lists separated by semicolons')
            lo, hi = int(match[1]), int(match[2] or match[1])
            if hi < lo or hi >= 1024:
                raise ValueError('CPU range is reversed or exceeds demo CPU_SETSIZE=1024')
            values = set(range(lo, hi + 1))
            if values & cpus:
                raise ValueError('duplicate CPU within a partition')
            cpus.update(values)
        if seen & cpus:
            raise ValueError('CPU partitions overlap')
        seen.update(cpus)
        groups.append(sorted(cpus))
    if not groups or len(groups) > 64:
        raise ValueError('expected between 1 and 64 CPU partitions')
    return groups


def cpu_list(cpus):
    values = sorted(cpus)
    parts, begin, last = [], values[0], values[0]
    for cpu in values[1:] + [None]:
        if cpu is not None and cpu == last + 1:
            last = cpu
            continue
        parts.append(str(begin) if begin == last else '%d-%d' % (begin, last))
        begin = last = cpu
    return ','.join(parts)


def resolve(path):
    path = Path(path).expanduser()
    return (path if path.is_absolute() else REPO / path).resolve()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def make_commands(args, perf='perf', taskset='taskset'):
    groups = partitions(args.cpus)
    union = cpu_list({cpu for group in groups for cpu in group})
    output, binary = resolve(args.output), resolve(args.binary)
    flags = {
        'output': str(output / 'demo'), 'cpus': ';'.join(cpu_list(g) for g in groups),
        'affinity': 'cluster', 'threads': args.threads, 'capacity-scale': 1,
        'requests': args.requests, 'qps': args.qps, 'arrival': 'regular',
        'burst-size': 8, 'burst-spacing-us': 100, 'widths': '80,4', 'depths': '2,2',
        'iterations': args.iterations, 'accesses': args.accesses, 'model': 'branch-rw',
        'branch-kb': 64, 'shared-kb': 0, 'jitter-percent': 25,
        'long-every': 8, 'long-factor': 4, 'work-scale-scope': 'task',
        'estimate-mode': 'constant', 'home-policy': 'rr', 'policy': args.policy,
        'continuation': 'parent', 'balance-at': 'all', 'batch-chunk': 1,
        'busy-high': 1, 'return-low': 0.5, 'min-gain': 0, 'cross-numa': 0,
        'cross-numa-penalty': 0, 'max-hops': 4, 'max-outstanding': 0,
        'cpu-sampling': 0, 'trace': 1, 'page-sample': 0, 'max-inflight': 16,
        'memory-mb': 512, 'seed': args.seed, 'sync-block-percent': 0,
        'sync-block-us': 0, 'async-percent': 0, 'async-delay-us': 0,
        'async-align-us': 0, 'async-capacity': 4096,
    }
    demo = [taskset, '-c', union, str(binary)]
    for name, value in flags.items():
        demo.extend(['--' + name, str(value)])
    workload = [sys.executable, str(BOUNDARY_HELPER), '--cpus', union,
                '--boundary-output', str(output / 'boundaries.json'), '--'] + demo
    record = [taskset, '-c', union, perf, 'record', '-a', '-C', union, '--clockid', 'mono',
              '-c', '1', '-e', 'sched:sched_switch', '-m', '1024',
              '-o', str(output / 'perf.data'), '--'] + workload
    script = [perf, 'script', '-i', str(output / 'perf.data'), '--ns',
              '--show-lost-events', '-F', 'trace:cpu,time,event,trace']
    profile = [sys.executable, str(REPO / 'experiments/affinity/request_sched_profile.py'),
               '--run', str(output / 'demo'), '--trace', str(output / 'sched.txt'),
               '--capture', str(output / 'capture.json'), '--request', str(args.request_id),
               '--bin-ms', '1', '--output', str(output / 'profile')]
    return {'record': record, 'script': script, 'profile': profile}, groups


def stop_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # Also terminate a child which outlived the group leader after TERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_to_files(command, stdout_path, stderr_path, timeout=TIMEOUT_SECONDS):
    with Path(stdout_path).open('wb') as out, Path(stderr_path).open('wb') as err:
        process = subprocess.Popen(command, cwd=REPO, stdout=out, stderr=err,
                                   start_new_session=True)
        try:
            return process.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            stop_group(process)
            return 124, True
        except BaseException:
            stop_group(process)
            raise


LOSS = re.compile(r'PERF_RECORD_LOST|\bLOST\s+(?:\d+\s+)?EVENTS?\b|'
                  r'\blost\s+[1-9]\d*\s+(?:events?|samples?)\b|'
                  r'\b(?:events?|samples?)\s+lost\s*[:=]\s*[1-9]\d*', re.I)


def lost_events(paths):
    for path in paths:
        with Path(path).open(errors='replace') as stream:
            if any(LOSS.search(line) for line in stream):
                return True
    return False


def terminate_requested(signum, frame):
    raise InterruptedError('received signal %d; terminating capture process group' % signum)


def write_capture(path, metadata):
    path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + '\n')


def preflight(args, groups):
    perf, taskset = shutil.which('perf'), shutil.which('taskset')
    if not perf or not taskset:
        raise ValueError('perf and taskset must already be installed; no workload launched')
    binary = resolve(args.binary)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError('demo binary is missing or not executable: ' + str(binary))
    output = resolve(args.output)
    if output.exists():
        raise ValueError('refusing existing output directory: ' + str(output))
    cpus = sorted(cpu for group in groups for cpu in group)
    probe = subprocess.run([taskset, '-c', cpu_list(cpus), sys.executable, '-c',
                            'import json,os;print(json.dumps(sorted(os.sched_getaffinity(0))))'],
                           capture_output=True, text=True, timeout=10)
    if probe.returncode or json.loads(probe.stdout) != cpus:
        raise ValueError('CPU preflight failed: effective mask differs from requested CPU set')
    version = subprocess.run([perf, '--version'], capture_output=True, text=True, timeout=10)
    if version.returncode:
        raise ValueError('perf --version failed; no workload launched')
    return perf, taskset, version.stdout.strip()


def execute(args, commands, groups):
    perf, taskset, version = preflight(args, groups)
    commands, groups = make_commands(args, perf, taskset)
    output = resolve(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()  # Fail if another run claimed the destination after preflight.
    metadata = {
        'schema': 'request-sched-capture-v1', 'clock': 'CLOCK_MONOTONIC',
        'cpus': sorted(cpu for group in groups for cpu in group), 'partitions': groups,
        'capture_completed': False, 'record_returncode': None, 'script_returncode': None,
        'profile_returncode': None, 'lost_events_detected': False,
        'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'perf_version': version, 'commands': commands, 'timeout_seconds_per_command': TIMEOUT_SECONDS,
        'events': ['sched:sched_switch'], 'sample_period': 1, 'requested_bin_ms': 1,
        'request_selected_for_profile': args.request_id, 'request_count': args.requests,
        'record_stdout_path': str(output / 'record.stdout.txt'),
        'record_stderr_path': str(output / 'record.stderr.txt'),
        'script_stderr_path': str(output / 'script.stderr.txt'),
        'profile_stdout_path': str(output / 'profile.stdout.txt'),
        'profile_stderr_path': str(output / 'profile.stderr.txt'),
        'trace_path': str(output / 'sched.txt'),
        'boundary_path': str(output / 'boundaries.json'),
        'boundary_method': 'Helper visits every selected CPU and sleeps 1ms before demo startup and after demo exit; outside all requests.',
        'source_sha256': {'binary': sha256(resolve(args.binary)), 'capture_script': sha256(__file__),
                          'boundary_helper': sha256(BOUNDARY_HELPER)},
        'scope': 'All sched_switch records on selected CPUs; no PID/event filter. '
                 'Request attribution requires task TIDs and complete matching intervals. '
                 'Scheduler residency includes time interrupting a running task; no IRQ subtraction.',
        'error': None,
    }
    capture = output / 'capture.json'
    write_capture(capture, metadata)
    previous_term = signal.signal(signal.SIGTERM, terminate_requested)
    capture_frozen = False
    execution = None
    try:
        for phase in ('record', 'script'):
            out = output / ('record.stdout.txt' if phase == 'record' else 'sched.txt')
            err = output / (phase + '.stderr.txt')
            code, timed_out = run_to_files(commands[phase], out, err)
            metadata[phase + '_returncode'] = code
            metadata[phase + '_timed_out'] = timed_out
            write_capture(capture, metadata)
            if code:
                raise RuntimeError('%s failed with exit=%d; no later phase launched; see %s' % (phase, code, err))
        metadata['lost_events_detected'] = lost_events([
            output / 'sched.txt', output / 'record.stderr.txt', output / 'script.stderr.txt'])
        if metadata['lost_events_detected']:
            raise RuntimeError('lost scheduler events detected; capture retained, profiling refused')
        if not (output / 'sched.txt').stat().st_size:
            raise RuntimeError('no scheduler text produced; capture retained, profiling refused')
        boundaries = json.loads((output / 'boundaries.json').read_text())
        if (boundaries.get('schema') != 'request-sched-boundaries-v1'
                or boundaries.get('clock') != 'CLOCK_MONOTONIC'
                or boundaries.get('cpus') != metadata['cpus']
                or not all(isinstance(boundaries.get(phase), dict)
                           and boundaries[phase].get('completed') is True for phase in ('pre', 'post'))
                or boundaries.get('demo_returncode') != 0 or boundaries.get('error') is not None):
            raise RuntimeError('CPU boundaries incomplete; capture retained, profiling refused')
        metadata['source_sha256']['boundaries'] = sha256(output / 'boundaries.json')
        metadata['source_sha256']['sched_text'] = sha256(output / 'sched.txt')
        metadata['source_sha256']['perf_data'] = sha256(output / 'perf.data')
        metadata['capture_completed'] = True
        metadata['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        write_capture(capture, metadata)
        capture_frozen = True  # The profiler records this file's hash; never rewrite it afterwards.
        execution = {'schema': 'request-sched-capture-execution-v1',
                     'capture_sha256': sha256(capture), 'profile_returncode': None,
                     'profile_timed_out': False, 'error': None}
        write_capture(output / 'execution.json', execution)
        code, timed_out = run_to_files(commands['profile'], output / 'profile.stdout.txt',
                                      output / 'profile.stderr.txt')
        execution['profile_returncode'], execution['profile_timed_out'] = code, timed_out
        write_capture(output / 'execution.json', execution)
        # The report is compact and printable; raw scheduler events remain local.
        print((output / 'profile.stdout.txt').read_text(errors='replace'), end='')
        if code:
            raise RuntimeError('offline profile failed with exit=%d; see %s' %
                               (code, output / 'profile.stderr.txt'))
        print('ONE CAPTURE COMPLETE: ' + str(output))
        print('This run diagnoses workload shape; it does not establish production p99 or tracing overhead.')
        return 0
    except BaseException as exc:
        error = type(exc).__name__ + ': ' + str(exc)
        if capture_frozen:
            execution['error'] = error
            write_capture(output / 'execution.json', execution)
        else:
            metadata['error'] = error
            write_capture(capture, metadata)
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_term)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='execute one capture; default only prints the plan')
    parser.add_argument('--output', required=True, help='new directory; relative to repository root')
    parser.add_argument('--binary', default='build-affinity/affinity_demo')
    parser.add_argument('--cpus', default=DEFAULT_CPUS, help='nonoverlapping semicolon-separated cluster CPU lists')
    parser.add_argument('--policy', choices=('home', 'queue', 'outstanding'), default='home')
    parser.add_argument('--iterations', type=int, default=3000000)
    parser.add_argument('--accesses', type=int, default=100000)
    parser.add_argument('--threads', type=int, default=16)
    parser.add_argument('--requests', type=int, default=24)
    parser.add_argument('--request-id', type=int, default=0, help='explicit request to profile; interior IDs avoid startup boundaries')
    parser.add_argument('--qps', type=float, default=12)
    parser.add_argument('--seed', type=int, default=421)
    args = parser.parse_args(argv)
    if (not 1 <= args.requests <= 10000 or not 1 <= args.threads <= 4096 or
            not math.isfinite(args.qps) or not .001 <= args.qps <= 1000000 or
            not 1 <= args.iterations <= 250000000000 or not 0 <= args.accesses <= 250000000000 or
            not 0 <= args.seed < 2**64):
        parser.error('configuration outside bounded demo limits')
    if not 0 <= args.request_id < args.requests:
        parser.error('request-id must be nonnegative and less than requests')
    if (args.requests - 1) / args.qps > TIMEOUT_SECONDS - 10:
        parser.error('planned arrivals exceed 110 seconds; shorten this diagnostic capture')
    return args


def main(argv=None):
    try:
        args = parse_args(argv)
        commands, groups = make_commands(args)
        print('REQUEST CPU DIAGNOSTIC: ' + ('EXECUTE ONE ROUND' if args.run else 'PLAN ONLY'))
        print('CLOCK_MONOTONIC; every sched_switch; offline 1ms integration; no global tracefs changes.')
        print('Output: ' + str(resolve(args.output)))
        for phase, command in commands.items():
            print(phase.upper() + ': ' + shlex.join(command))
        if not args.run:
            print('PLAN ONLY: no directories written, dependencies probed, or workloads launched.')
            return 0
        return execute(args, commands, groups)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('STOP: ' + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('STOP: interrupted; child process group stopped and partial output retained.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
