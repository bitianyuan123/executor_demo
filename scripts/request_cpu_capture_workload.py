#!/usr/bin/env python3
"""Create per-CPU trace boundaries outside the demo's request lifetime.

Runs inside perf's recording scope and inherits its process group. Each boundary
visits every selected CPU and sleeps there, providing real switch-in/switch-out
events even when that CPU would otherwise remain idle for the whole capture.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def parse_cpus(value):
    cpus = set()
    for token in value.split(','):
        bounds = token.split('-')
        if not 1 <= len(bounds) <= 2 or not all(v.isdecimal() for v in bounds):
            raise ValueError('invalid CPU list')
        first, last = int(bounds[0]), int(bounds[-1])
        if first > last or last >= 1024:
            raise ValueError('CPU range outside supported bounds')
        cpus.update(range(first, last + 1))
    if not cpus:
        raise ValueError('empty CPU list')
    return sorted(cpus)


def boundary(cpus):
    original = os.sched_getaffinity(0)
    if set(cpus) != original:
        raise ValueError('boundary helper CPU affinity differs from the capture union')
    result = {'started_monotonic_ns': time.monotonic_ns(), 'completed': False,
              'sleep_ns_per_cpu': 1_000_000, 'visits': []}
    try:
        for cpu in cpus:
            os.sched_setaffinity(0, {cpu})
            before = time.monotonic_ns()
            time.sleep(.001)
            result['visits'].append({'cpu': cpu, 'before_sleep_ns': before,
                                     'after_sleep_ns': time.monotonic_ns()})
        result['completed'] = True
        return result
    finally:
        os.sched_setaffinity(0, original)
        result['finished_monotonic_ns'] = time.monotonic_ns()


def execute(cpus, command, output):
    output = Path(output)
    if output.exists():
        raise ValueError('boundary output already exists')
    if not command:
        raise ValueError('missing demo command')
    record = {'schema': 'request-sched-boundaries-v1', 'clock': 'CLOCK_MONOTONIC',
              'cpus': cpus, 'demo_command': command, 'pre': None, 'post': None,
              'demo_returncode': None, 'error': None,
              'scope': 'Real sched_switch boundary activity before demo startup and after demo exit; '
                       'outside all request lifetimes; no synthetic scheduler intervals.'}

    def save():
        output.write_text(json.dumps(record, indent=2) + '\n')

    save()
    try:
        record['pre'] = boundary(cpus)
        save()
        try:
            record['demo_started_monotonic_ns'] = time.monotonic_ns()
            # Inherit the outer capture process group for timeout/interrupt cleanup.
            record['demo_returncode'] = subprocess.run(command).returncode
        finally:
            record['demo_finished_monotonic_ns'] = time.monotonic_ns()
            record['post'] = boundary(cpus)
            save()
        code = record['demo_returncode']
        return code if code >= 0 else 128 - code
    except BaseException as exc:
        record['error'] = type(exc).__name__ + ': ' + str(exc)
        save()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cpus', required=True)
    parser.add_argument('--boundary-output', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    try:
        return execute(parse_cpus(args.cpus), command, args.boundary_output)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('BOUNDARY STOP: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
