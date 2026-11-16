#!/usr/bin/env python3
"""Capture CPU idle transitions in a private tracefs instance.

This is an observational sidecar, never an input to load balancing. It measures
scheduled non-idle occupancy, not productive task work or hardware utilization.
No global tracer, trace options, mounts or sysctls are changed. Timestamp text
may have microsecond precision; that is distinct from the reporting bin width.
"""
import argparse
import json
import os
from pathlib import Path
import re
import select
import sys
import threading
import time
import uuid

SCHEMA_VERSION = 1
LOSS_FIELDS = ('overrun', 'commit overrun', 'dropped events')
IDLE_FILTER = 'prev_pid == 0 || next_pid == 0'


def parse_cpu_list(value):
    cpus = set()
    for part in value.split(','):
        if not re.fullmatch(r'\d+(?:-\d+)?', part):
            raise ValueError('CPU list must contain numbers or increasing ranges')
        bounds = [int(v) for v in part.split('-')]
        first, last = bounds[0], bounds[-1]
        if first > last or last > 1048575:
            raise ValueError('CPU range is reversed or unreasonably large')
        added = set(range(first, last + 1))
        if cpus & added:
            raise ValueError('duplicate CPU')
        cpus.update(added)
    if not cpus:
        raise ValueError('empty CPU list')
    return sorted(cpus)


def cpu_mask(cpus):
    """Kernel hexadecimal cpumask: most significant 32-bit word first."""
    cpus = sorted(set(cpus))
    if not cpus or any(not isinstance(c, int) or c < 0 or c > 1048575 for c in cpus):
        raise ValueError('nonempty nonnegative integer CPUs required')
    words = [0] * (cpus[-1] // 32 + 1)
    for cpu in cpus:
        words[cpu // 32] |= 1 << (cpu % 32)
    return ','.join(f'{word:08x}' for word in reversed(words))


def _atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def _find_tracefs():
    for path in (Path('/sys/kernel/tracing'), Path('/sys/kernel/debug/tracing')):
        if (path / 'instances').is_dir() and (path / 'events/sched/sched_switch').is_dir():
            return path
    raise RuntimeError('tracefs with sched:sched_switch is unavailable; mount/permission changes are not automatic')


class IdleTraceCapture:
    """Call start(), launch the workload, start_reader(), then always stop().

    start() deliberately starts no threads, allowing a caller to launch a
    subprocess with preexec_fn first. start failures write a failed status and
    raise RuntimeError after cleanup. stop() is idempotent and returns status.
    The protected instance/pipe methods permit temporary fake tracefs testing.
    """
    def __init__(self, groups, output_dir, tracefs_root=None, buffer_kb=256):
        self.groups = [list(group) for group in groups]
        self.cpus = sorted(set(cpu for group in self.groups for cpu in group))
        if not self.groups or any(not group for group in self.groups):
            raise ValueError('nonempty CPU groups required')
        cpu_mask(self.cpus)
        if len(self.cpus) != sum(map(len, self.groups)):
            raise ValueError('CPU groups must be disjoint')
        if not isinstance(buffer_kb, int) or not 4 <= buffer_kb <= 1048576:
            raise ValueError('buffer_kb must be an integer from 4 through 1048576')
        self.output_dir = Path(output_dir)
        self.tracefs_root = Path(tracefs_root) if tracefs_root is not None else None
        self.buffer_kb = buffer_kb
        self.instance = None
        self._owned = False
        self._started = False
        self._stopped = False
        self._reader_thread = None
        self._stop_reader = threading.Event()
        self._pipe_fd = None
        self._trace_file = None
        self._loss_tail = b''
        self._process_cpu_start_ns = None
        self.status = {
            'schema_version': SCHEMA_VERSION, 'schema': SCHEMA_VERSION,
            'status': 'not_started', 'source': 'tracefs:sched:sched_switch:idle_transitions',
            'start_monotonic_ns': None, 'end_monotonic_ns': None,
            'cpu_ids': self.cpus, 'groups': self.groups, 'clock': 'mono',
            'timestamp_precision': None, 'buffer_kb_per_cpu': buffer_kb,
            'filter': IDLE_FILTER, 'trace_path': 'cpu_trace.txt',
            'instance': None, 'stats': {}, 'errors': [], 'bytes_written': 0,
            'lost_event_marker': False, 'cleanup_complete': False,
            'reader_started_monotonic_ns': None,
            'online_cpu_ids_start': None, 'online_cpu_ids_end': None,
            'observer_process_cpu_seconds': None, 'reader_thread_cpu_seconds': None,
            'observer_cpu_scope': 'whole observer process during capture, including runner and proc/stat work; excludes kernel tracing overhead and workload subprocess',
        }

    def _save(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(self.output_dir / 'cpu_trace_status.json', self.status)

    def _error(self, message):
        self.status['errors'].append(str(message))
        self.status['status'] = 'failed'

    def _online_cpus(self):
        return parse_cpu_list(Path('/sys/devices/system/cpu/online').read_text().strip())

    def _make_instance(self):
        self.instance.mkdir()

    def _remove_instance(self):
        # tracefs removes its virtual children with rmdir. Never recursively
        # delete a path or touch an instance we did not successfully create.
        self.instance.rmdir()

    def _write(self, relative, value):
        (self.instance / relative).write_text(str(value) + '\n')

    def _open_trace_pipe(self):
        return os.open(self.instance / 'trace_pipe', os.O_RDONLY | os.O_NONBLOCK)

    def start(self):
        if self._started or self._stopped:
            raise RuntimeError('capture cannot be restarted')
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.status['status'] = 'initializing'
        self._process_cpu_start_ns = time.process_time_ns()
        try:
            self.status['online_cpu_ids_start'] = self._online_cpus()
            missing = set(self.cpus) - set(self.status['online_cpu_ids_start'])
            if missing:
                raise RuntimeError(f'selected CPUs are offline or absent: {sorted(missing)}')
            root = self.tracefs_root or _find_tracefs()
            if not (root / 'instances').is_dir():
                raise RuntimeError(f'tracefs instances directory unavailable: {root}')
            self.instance = root / 'instances' / f'executor-idle-{os.getpid()}-{uuid.uuid4().hex[:12]}'
            self.status['instance'] = str(self.instance)
            self._make_instance()
            self._owned = True
            self._write('tracing_on', 0)
            self._write('current_tracer', 'nop')
            available = (self.instance / 'trace_clock').read_text().replace('[', '').replace(']', '').split()
            if 'mono' not in available:
                raise RuntimeError('tracefs mono clock unavailable; cannot align with workload CLOCK_MONOTONIC')
            self._write('trace_clock', 'mono')
            selected = (self.instance / 'trace_clock').read_text().strip()
            if '[mono]' not in selected and selected != 'mono':
                raise RuntimeError(f'tracefs mono clock was not selected: {selected}')
            self._write('buffer_size_kb', self.buffer_kb)
            self._write('tracing_cpumask', cpu_mask(self.cpus))
            nsecs = self.instance / 'options/nsecs'
            self.status['timestamp_precision'] = 'nanoseconds' if nsecs.exists() and nsecs.read_text().strip() == '1' else 'microseconds'
            event = self.instance / 'events/sched/sched_switch'
            if not event.is_dir():
                raise RuntimeError('sched:sched_switch is unavailable in the private instance')
            self._write('events/sched/sched_switch/filter', IDLE_FILTER)
            configured = (event / 'filter').read_text()
            if 'prev_pid' not in configured or 'next_pid' not in configured or '^' in configured:
                raise RuntimeError(f'idle-transition filter rejected: {configured}')
            # Opening before enabling ensures errors cannot silently leave tracing
            # active; no reader thread exists until start_reader() is called.
            self._trace_file = (self.output_dir / 'cpu_trace.txt').open('wb')
            self._pipe_fd = self._open_trace_pipe()
            self._write('events/sched/sched_switch/enable', 1)
            self._write('tracing_on', 1)
            self.status['start_monotonic_ns'] = time.monotonic_ns()
            self._started = True
            self.status['status'] = 'capturing'
            self._save()
            return self.status
        except Exception as exc:
            self._error(f'start: {type(exc).__name__}: {exc}')
            self.stop()
            raise RuntimeError(self.status['errors'][0]) from exc

    def start_reader(self):
        if not self._started or self._stopped or self._reader_thread is not None:
            raise RuntimeError('start_reader requires an active capture and can run only once')
        try:
            self.status['reader_started_monotonic_ns'] = time.monotonic_ns()
            self._reader_thread = threading.Thread(target=self._read, name='executor-idle-trace', daemon=True)
            self._reader_thread.start()
        except Exception as exc:
            self._reader_thread = None
            self._error(f'start_reader: {type(exc).__name__}: {exc}')
            self.stop()
            raise RuntimeError(self.status['errors'][-1]) from exc

    def _consume(self, block):
        self._trace_file.write(block)
        self.status['bytes_written'] += len(block)
        combined = self._loss_tail + block
        if re.search(rb'LOST\s+\d+\s+EVENTS', combined, re.IGNORECASE):
            self.status['lost_event_marker'] = True
        self._loss_tail = combined[-128:]

    def _read(self):
        thread_cpu_start_ns = time.thread_time_ns()
        try:
            while True:
                try:
                    block = os.read(self._pipe_fd, 256 * 1024)
                except BlockingIOError:
                    block = b''
                if block:
                    self._consume(block)
                    continue
                if self._stop_reader.is_set():
                    break
                # trace_pipe supports polling. A temporary regular-file fixture
                # is always readable at EOF, so the event also bounds busy loops.
                ready, _, _ = select.select([self._pipe_fd], [], [], .05)
                if ready:
                    self._stop_reader.wait(.001)
        except Exception as exc:
            self._error(f'reader: {type(exc).__name__}: {exc}')
        finally:
            self.status['reader_thread_cpu_seconds'] = (time.thread_time_ns() - thread_cpu_start_ns) / 1e9

    def _read_stats(self):
        for cpu in self.cpus:
            try:
                raw = (self.instance / f'per_cpu/cpu{cpu}/stats').read_text()
                stats = {'raw': raw}
                for line in raw.splitlines():
                    key, separator, value = line.partition(':')
                    if separator:
                        value = value.strip()
                        stats[key.strip()] = int(value) if re.fullmatch(r'\d+', value) else value
                self.status['stats'][str(cpu)] = stats
                if not isinstance(stats.get('entries'), int):
                    self._error(f'CPU {cpu}: missing or invalid remaining entry count')
                elif stats['entries'] != 0:
                    self._error(f'CPU {cpu}: entries={stats["entries"]}; trace buffer was not completely drained')
                for key in LOSS_FIELDS:
                    if key not in stats or not isinstance(stats[key], int):
                        self._error(f'CPU {cpu}: missing or invalid loss counter {key}')
                    elif stats[key] != 0:
                        self._error(f'CPU {cpu}: {key}={stats[key]}')
            except Exception as exc:
                self._error(f'CPU {cpu} stats: {type(exc).__name__}: {exc}')

    def stop(self):
        if self._stopped:
            return self.status
        self._stopped = True
        if self._owned:
            if self._started:
                self.status['end_monotonic_ns'] = time.monotonic_ns()
            try:
                self._write('tracing_on', 0)
            except Exception as exc:
                self._error(f'disable tracing: {type(exc).__name__}: {exc}')
            try:
                self._write('events/sched/sched_switch/enable', 0)
            except Exception as exc:
                self._error(f'disable event: {type(exc).__name__}: {exc}')
        self._stop_reader.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=5)
            if self._reader_thread.is_alive():
                self._error('reader did not drain within 5 seconds; shared resources and private instance retained')
                # Closing either handle while the reader is using it can race
                # with an unrelated reused fd or corrupt the output. Fail this
                # run and retain the disabled instance for explicit inspection.
                self.status['cleanup_complete'] = False
                self.status['cleanup_deferred_reason'] = 'reader still alive after timeout; tracing/event disable attempted; instance path retained'
                if self._process_cpu_start_ns is not None:
                    self.status['observer_process_cpu_seconds'] = (time.process_time_ns() - self._process_cpu_start_ns) / 1e9
                self._save()
                return self.status
        elif self._pipe_fd is not None:
            # Even startup failure or check without a reader preserves evidence.
            self._read()
        if self._pipe_fd is not None:
            try:
                os.close(self._pipe_fd)
            except OSError as exc:
                self._error(f'close trace_pipe: {exc}')
            self._pipe_fd = None
        if self._trace_file is not None:
            try:
                self._trace_file.close()
            except OSError as exc:
                self._error(f'close trace output: {exc}')
            self._trace_file = None
        if self._owned:
            self._read_stats()
            try:
                self._remove_instance()
                self._owned = False
            except Exception as exc:
                self._error(f'cleanup: {type(exc).__name__}: {exc}')
        if self._started:
            try:
                online_end = self._online_cpus()
                self.status['online_cpu_ids_end'] = online_end
                if set(self.cpus) - set(online_end):
                    self._error('selected CPU went offline during capture')
                if online_end != self.status['online_cpu_ids_start']:
                    self._error('CPU online set changed during capture; hotplug is unsupported')
            except Exception as exc:
                self._error(f'CPU online check: {type(exc).__name__}: {exc}')
        self.status['cleanup_complete'] = not self._owned
        if self.status['lost_event_marker']:
            self._error('trace text contains a LOST EVENTS marker')
        if not self._started and not self.status['errors']:
            self._error('capture was never started')
        if self.status['errors']:
            self.status['status'] = 'failed'
        else:
            self.status['status'] = 'ok'
        if self._process_cpu_start_ns is not None:
            self.status['observer_process_cpu_seconds'] = (time.process_time_ns() - self._process_cpu_start_ns) / 1e9
        self._save()
        return self.status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    check = sub.add_parser('check', help='short private-instance permission/capability check; no synthetic workload')
    check.add_argument('--cpus', required=True)
    check.add_argument('--output', default='results/cpu-trace-check')
    check.add_argument('--tracefs-root')
    check.add_argument('--buffer-kb', type=int, default=256)
    check.add_argument('--seconds', type=float, default=.2)
    args = parser.parse_args(argv)
    if not .01 <= args.seconds <= 5:
        parser.error('--seconds must be between 0.01 and 5')
    try:
        capture = IdleTraceCapture([parse_cpu_list(args.cpus)], args.output, args.tracefs_root, args.buffer_kb)
        try:
            capture.start()
            capture.start_reader()
            time.sleep(args.seconds)
        finally:
            status = capture.stop()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status['status'] == 'ok' else 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'CPU trace check failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
