#!/usr/bin/env python3
"""Validate explicit DAG edges and decompose the observed completion-determining path."""
import argparse
import csv
import json
import statistics
from pathlib import Path


def read_csv(path):
    with path.open(newline='') as stream:
        return list(csv.DictReader(stream))


def percentile(values, fraction):
    import math
    return sorted(values)[max(0, math.ceil(fraction * len(values)) - 1)] if values else None


def expected_graph(config, request):
    """Reconstruct edges independently, so a missing CSV edge cannot validate itself."""
    if config['graph'] == 'chains':
        widths, depths = [int(request['fanout'])], [int(config['chain_length'])]
    elif config['graph'] == 'waves':
        widths = [int(value) for value in config['wave_widths'].split(',')]
        depths = ([int(value) for value in config['wave_depths'].split(',')]
                  if config['wave_depths'] else [int(config['chain_length'])] * len(widths))
    else:
        raise ValueError('unsupported graph model')
    if len(widths) != len(depths) or not widths or min(widths + depths) <= 0:
        raise ValueError('invalid graph dimensions')
    operators = sum(width * depth for width, depth in zip(widths, depths))
    if (int(request['operator_nodes']) != operators or int(request['cpu_nodes']) != operators + len(widths)
            or int(request['waves']) != len(widths)):
        raise ValueError('request node counts differ from configured graph')
    edges, kinds, offset = {}, {}, 0
    for wave, (width, depth) in enumerate(zip(widths, depths)):
        for branch in range(width):
            for level in range(depth):
                node = offset + branch * depth + level
                kinds[node] = 'operator'
                edges[node] = [node - 1] if level else ([operators + wave - 1] if wave else [])
        join = operators + wave
        kinds[join] = 'join'
        edges[join] = [offset + (branch + 1) * depth - 1 for branch in range(width)]
        offset += width * depth
    return edges, kinds


def analyze(directory):
    metadata = json.loads((directory / 'metadata.json').read_text())
    summary = json.loads((directory / 'summary.json').read_text())
    if metadata.get('schema_version', 0) < 2 or not metadata['config'].get('task_log'):
        raise ValueError(f'{directory}: requires schema 2 and --task-log 1')
    if metadata.get('phase') != 'completed' or not summary.get('conservation_ok') or summary.get('io_failed'):
        raise ValueError(f'{directory}: incomplete or invalid run')
    requests = read_csv(directory / 'requests.csv')
    tasks = read_csv(directory / 'tasks.csv')
    if len(requests) != summary['offered'] or len(tasks) != summary['task_rows']:
        raise ValueError(f'{directory}: truncated CSV')
    request_ids = {row['request_id'] for row in requests}
    if len(request_ids) != len(requests):
        raise ValueError('duplicate request ID')
    by_request = {}
    for row in tasks:
        if row['request_id'] not in request_ids:
            raise ValueError('task belongs to an unknown request')
        by_request.setdefault(row['request_id'], []).append(row)
    results = []
    edge_count = 0
    for request in requests:
        if request['status'] != 'completed':
            continue
        task_rows = by_request.get(request['request_id'], [])
        task_map = {(int(row['node_id']), int(row['stage'])): row for row in task_rows}
        if len(task_map) != len(task_rows):
            raise ValueError('duplicate task stage')
        if {key[0] for key in task_map} != set(range(int(request['cpu_nodes']))):
            raise ValueError('missing or unexpected CPU node')
        expected_edges, expected_kinds = expected_graph(metadata['config'], request)
        if any((node, 0) not in task_map for node in expected_edges):
            raise ValueError('missing initial task stage')
        predecessors = {}
        for key, row in task_map.items():
            if row['status'] != 'completed':
                raise ValueError('completed request contains incomplete task')
            if key[1] not in (0, 1) or row['kind'] != expected_kinds[key[0]] or (key[1] == 1 and row['kind'] != 'operator'):
                raise ValueError('invalid task stage or kind')
            submit, start, finish = (int(row[name]) for name in ('submit_ns', 'start_ns', 'finish_ns'))
            if not int(request['scheduled_ns']) <= submit <= start <= finish <= int(request['finish_ns']):
                raise ValueError('invalid task timestamps')
            recorded_edges = [int(value) for value in row['predecessors'].split(';') if value]
            expected = [key[0]] if key[1] == 1 else expected_edges[key[0]]
            if sorted(recorded_edges) != sorted(expected):
                raise ValueError('recorded dependencies differ from configured graph')
            dependencies = []
            for node in recorded_edges:
                dependency = (node, 0) if node == key[0] else ((node, 1) if (node, 1) in task_map else (node, 0))
                if dependency not in task_map or dependency == key:
                    raise ValueError('invalid predecessor')
                if int(task_map[dependency]['finish_ns']) > int(row['submit_ns']):
                    raise ValueError('task was submitted before its predecessor completed')
                dependencies.append(dependency)
                edge_count += 1
            predecessors[key] = dependencies
        terminal = (int(request['cpu_nodes']) - 1, 0)
        if task_map[terminal]['kind'] != 'join':
            raise ValueError('terminal must be final join')
        path, seen, cursor = [], set(), terminal
        while True:
            if cursor in seen:
                raise ValueError('cycle found')
            seen.add(cursor)
            path.append(cursor)
            if not predecessors[cursor]:
                break
            cursor = max(predecessors[cursor], key=lambda key: int(task_map[key]['finish_ns']))
        path.reverse()
        previous_finish = int(request['scheduled_ns'])
        dispatch_ns = queue_ns = wall_ns = 0
        cpu_us = 0.0
        for key in path:
            row = task_map[key]
            submit, start, finish = (int(row[name]) for name in ('submit_ns', 'start_ns', 'finish_ns'))
            if not previous_finish <= submit <= start <= finish:
                raise ValueError('invalid observed path timestamps')
            dispatch_ns += submit - previous_finish
            queue_ns += start - submit
            wall_ns += finish - start
            cpu_us += float(row['cpu_us'])
            previous_finish = finish
        tail_ns = int(request['finish_ns']) - previous_finish
        latency_ns = int(request['finish_ns']) - int(request['scheduled_ns'])
        if tail_ns < 0 or dispatch_ns + queue_ns + wall_ns + tail_ns != latency_ns:
            raise ValueError('path decomposition does not conserve end-to-end latency')
        results.append({'request_id': request['request_id'], 'path_stages': len(path),
                        'latency_ms': latency_ns / 1e6, 'path_dispatch_rpc_ms': dispatch_ns / 1e6,
                        'path_submit_to_start_ms': queue_ns / 1e6, 'path_execution_wall_ms': wall_ns / 1e6,
                        'path_kernel_cpu_ms': cpu_us / 1000, 'request_tail_ms': tail_ns / 1e6,
                        'parallel_sum_execution_wall_ms': sum(float(row['wall_us']) for row in task_rows) / 1000,
                        'parallel_sum_submit_to_start_ms': sum(float(row['enqueue_us']) for row in task_rows) / 1000,
                        'path': [f'{node}:{stage}' for node, stage in path]})
    fields = ('latency_ms', 'path_dispatch_rpc_ms', 'path_submit_to_start_ms', 'path_execution_wall_ms',
              'path_kernel_cpu_ms', 'request_tail_ms', 'parallel_sum_execution_wall_ms', 'parallel_sum_submit_to_start_ms')
    aggregates = {field: {'mean': statistics.mean([row[field] for row in results]) if results else None,
                          'p99': percentile([row[field] for row in results], .99)} for field in fields}
    return {'directory': str(directory), 'requests_completed': len(results), 'edges_checked': edge_count,
            'excluded_requests': len(requests) - len(results), 'aggregates_ms': aggregates, 'requests': results,
            'limitations': ['The path uses the latest observed predecessor at each join; it is not an intrinsic service-only critical path.',
                            'The latest recorded CPU finish can differ from the branch that performs the final dependency decrement and selects join parent.',
                            'Submit-to-start combines Folly work, wakeup, and kernel scheduling; it is not kernel runq delay.',
                            'Dispatch/RPC includes admission lag, producer fanout, async timer, and successor submission.',
                            'CPU is measured only around the synthetic kernel; execution wall minus CPU is not a direct runq measurement.',
                            'Per-request parallel sums overlap and are not additive components of request latency.',
                            'The p99 of individual components does not add to request p99; decomposition is exact per request.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True, help='Output prefix for JSON and Markdown')
    args = parser.parse_args()
    reports = [analyze(run) for run in args.runs]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix('.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2) + '\n')
    lines = ['# DAG 依赖与观测完成路径分析', '',
             '逐请求沿各 join 最晚完成的前驱回溯。下表为同一批请求的平均值，分量可以相加；各分量 p99 不可相加。', '',
             '| 运行 | 完成请求 | 已验证依赖边 | 请求 ms | 路径调度/RPC间隙 ms | 路径提交至开始 ms | 路径执行墙钟 ms | 请求尾部 ms |',
             '|---|---:|---:|---:|---:|---:|---:|---:|']
    for report in reports:
        values = report['aggregates_ms']
        formatted = [f'{values[field]["mean"]:.3f}' if values[field]['mean'] is not None else '—' for field in
                     ('latency_ms', 'path_dispatch_rpc_ms', 'path_submit_to_start_ms', 'path_execution_wall_ms', 'request_tail_ms')]
        lines.append(f'| {report["directory"]} | {report["requests_completed"]} | {report["edges_checked"]} | ' + ' | '.join(formatted) + ' |')
    lines += ['', '提交至开始不是内核 runq 时间；执行墙钟减去算子 CPU 时间也不能精确指认为 runq。并发分支的累计等待不能直接相加解释请求时延。',
              '', 'JSON 保留每请求的路径节点、分量、完整性检查与排除的未完成请求数量。存在拒绝或截断时，完成请求 p99 不是全部请求的无偏估计。', '']
    args.output.with_suffix('.md').write_text('\n'.join(lines))
    print(args.output.with_suffix('.md'))


if __name__ == '__main__':
    main()
