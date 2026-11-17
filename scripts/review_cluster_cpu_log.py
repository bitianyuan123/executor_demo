#!/usr/bin/env python3
"""Extract the bounded six-round CPU supplement from a terminal transcript.

Only START ROUND/END ROUND blocks are evidence. Plan commands are ignored;
commands and provenance are taken from the EXECUTE preamble. Repeated summary
rows following the last block are deliberately excluded. Printed precision is
retained; /proc/PID CPU windows have different read times from /proc/stat, so
process duration-weighted aggregates cannot be rederived from CPU_SERIES times.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import shlex
import sys

ORDER = ['01-home', '02-queue', '03-outstanding', '04-outstanding', '05-queue', '06-home']
SERIES_FIELDS = ['relative_start_ms', 'relative_end_ms', 'cluster', 'valid',
                 'busy_cpu_equivalent', 'util_percent', 'idle_cpu_equivalent',
                 'iowait_cpu_equivalent', 'steal_cpu_equivalent',
                 'process_cpu_equivalent', 'invalid_reason', 'sample_lag_ms']
ROLES = ['init', 'first-wave stage0', 'later-wave stage0', 'ordinary successors', 'join']
ROLE_HEADER = 'role,n,changed_pool,changed_pool_percent,wait_mean_ms'


class EvidenceError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise EvidenceError(message)


def only(items, name):
    require(len(items) == 1, f'{name}: expected one record, found {len(items)}')
    return items[0]


def near(a, b, tolerance, name):
    require(math.isfinite(a) and math.isfinite(b) and abs(a-b) <= tolerance,
            f'{name}: {a} != {b} (tolerance {tolerance})')


def quantile(values, p):
    return sorted(values)[max(0, math.ceil(len(values)*p)-1)]


def cells(line):
    return [x.strip() for x in line.strip().strip('|').split('|')]


def numeric_options(tokens):
    result = {}
    for i, token in enumerate(tokens):
        if token.startswith('--') and i+1 < len(tokens) and not tokens[i+1].startswith('--'):
            result[token[2:].replace('-', '_')] = tokens[i+1]
    return result


def extract_metadata(lines, first_start):
    execute = [i for i, text in enumerate(lines[:first_start])
               if text == 'OPTIONAL CPU SUPPLEMENT: EXECUTE']
    start = only(execute, 'EXECUTE preamble')
    preamble = list(enumerate(lines[start:first_start], start+1))
    result = {'source_lines': {}, 'commands': {}}
    for line_no, text in preamble:
        if text.startswith(('CPU set: ', 'Partitions: ')):
            key = 'cpu_set' if text.startswith('CPU set: ') else 'partitions'
            result[key] = text.split(': ', 1)[1]
            result['source_lines'][key] = line_no
        elif text.startswith(('repository_commit=', 'binary_sha256=', 'started_utc=', 'policies=')):
            key, value = text.split('=', 1)
            result[key] = value
            result['source_lines'][key] = line_no
        elif text.startswith('ROUND '):
            round_id = text[6:]
            command = lines[line_no]  # 1-based current line -> next line index
            tokens = shlex.split(command)
            require('--' in tokens, f'{round_id}: command has no child separator')
            split = tokens.index('--')
            child = tokens[split+1:]
            opts = numeric_options(child)
            require(round_id not in result['commands'], f'duplicate command {round_id}')
            result['commands'][round_id] = {
                'source_line': line_no+1, 'argv': tokens,
                'capture_options': numeric_options(tokens[:split]), 'demo_options': opts}
    require(list(result['commands']) == ORDER, 'execution commands must have six fixed round order')
    require(all(key in result for key in ('cpu_set', 'partitions', 'repository_commit', 'binary_sha256')),
            'missing execution metadata')
    common = {k: v for k, v in result['commands'][ORDER[0]]['demo_options'].items()
              if k not in ('policy', 'output')}
    for round_id, command in result['commands'].items():
        opts = command['demo_options']
        require(opts['policy'] == round_id.split('-', 1)[1], f'{round_id}: command policy mismatch')
        require({k: v for k, v in opts.items() if k not in ('policy', 'output')} == common,
                f'{round_id}: workload/resource options changed')
    result['common_demo_options'] = common
    return result


def parse_round(round_id, numbered, command):
    def match(prefix):
        return only([(n, s[len(prefix):]) for n, s in numbered if s.startswith(prefix)],
                    f'{round_id}: {prefix}')

    loc = {'start': numbered[0][0]-1, 'end': numbered[-1][0]+1,
           'command': command['source_line']}
    cap_line, cap = match('CPU_CAPTURE ')
    scope_line, scope = match('CPU_SCOPE ')
    capture, scope = json.loads(cap), json.loads(scope)
    loc.update(cpu_capture=cap_line, cpu_scope=scope_line)
    header_line, header = match('CPU_SERIES_BEGIN ')
    end_series, _ = match('CPU_SERIES_END')
    require(header.split(',') == SERIES_FIELDS, f'{round_id}: unsupported CPU_SERIES header')
    rows = []
    for line_no, text in numbered:
        if header_line < line_no < end_series:
            values = next(csv.reader([text]))
            require(len(values) == len(SERIES_FIELDS), f'CPU row {line_no}: wrong column count')
            row = dict(zip(SERIES_FIELDS, values))
            for key in SERIES_FIELDS:
                if key == 'invalid_reason':
                    continue
                row[key] = int(row[key]) if key in ('cluster', 'valid') else (float(row[key]) if row[key] else None)
            row.update(round_id=round_id, policy=round_id.split('-', 1)[1], source_line=line_no)
            rows.append(row)
    loc['cpu_series'] = [header_line+1, end_series-1]
    report_line, report_raw = only([(n, s) for n, s in numbered if s.startswith('| demo |')],
                                   f'{round_id}: report row')
    c = cells(report_raw)
    model, policy, continuation = c[1].split('/')
    completed, offered = map(int, c[2].split('/'))
    op_cpu, op_wall = map(float, c[7].split('/'))
    migrations, tasks = map(int, c[8].split('/'))
    report = dict(model=model, policy=policy, continuation=continuation, completed=completed,
                  offered=offered, mean_ms=float(c[3]), p95_ms=float(c[4]), p99_ms=float(c[5]),
                  task_wait_p99_ms=float(c[6]), operator_cpu_mean_ms=op_cpu,
                  operator_wall_mean_ms=op_wall, migrations=migrations, tasks=tasks, cross_numa=int(c[9]))
    loc['report'] = report_line
    whole_line, whole_text = match('  WHOLE RUN (not cohort): ')
    whole = re.fullmatch(r'process CPU s=([\d.]+), process CPU/completed-request ms=([\d.]+), peak_inflight=(\d+), submission lag p99 ms=([\d.]+)', whole_text)
    require(whole is not None, f'{round_id}: malformed WHOLE RUN')
    whole = dict(zip(['process_cpu_seconds', 'process_cpu_per_request_ms', 'peak_inflight', 'submission_lag_p99_ms'],
                     [float(whole[1]), float(whole[2]), int(whole[3]), float(whole[4])]))
    seed_line, seed_text = match('  seed=')
    seed = re.fullmatch(r'(\d+), pool_count=(\d+), affinity=(\w+), threads/pool=(\d+), whole completed/rejected-or-failed=(\d+)/(\d+), operator CPU/request ms=([\d.]+)', seed_text)
    require(seed is not None, f'{round_id}: malformed run resources')
    resource = dict(seed=int(seed[1]), pool_count=int(seed[2]), affinity=seed[3], threads_per_pool=int(seed[4]))
    whole.update(completed=int(seed[5]), rejected_or_failed=int(seed[6]), operator_cpu_per_request_ms=float(seed[7]))
    work_line, work_text = match('  work_signature_sha256_prefix=')
    work = re.fullmatch(r'(\w+), arrival_hash=(\d+), checksum=(\d+), pages=(.*)', work_text)
    require(work is not None, f'{round_id}: malformed work signature')
    work = dict(work_signature_sha256_prefix=work[1], arrival_hash=int(work[2]), checksum=int(work[3]), pages=json.loads(work[4]))
    fixed_line, fixed_text = match('  fixed_work_sha256_prefix=')
    work['fixed_work_sha256_prefix'] = fixed_text.split(';')[0]
    arrival_line, arrival_text = match('  arrival=')
    arr = re.fullmatch(r'(\w+), burst size/spacing us=(.*), whole planned iterations/accesses=(\d+)/(\d+), per-request mean=(\d+)/(\d+)', arrival_text)
    require(arr is not None, f'{round_id}: malformed arrival')
    arrival = dict(kind=arr[1], burst_size_spacing_us=arr[2], whole_iterations=int(arr[3]), whole_accesses=int(arr[4]),
                   per_request_mean_iterations=int(arr[5]), per_request_mean_accesses=int(arr[6]))
    groups_line, groups_text = match('CPU groups (cluster): ')
    groups = json.loads(groups_text)
    occupancy_line, occupancy = match('  WALL-WINDOW callback occupancy ')
    flow_arrival_line, flow_arrival = match('Arrival=')
    require(f'completed/offered={completed}/{offered}' in flow_arrival, f'{round_id}: flow cohort mismatch')
    means_line, means_text = match('Completed means ms: planned latency=')
    means_match = re.fullmatch(r'([\d.]+) = attempt lag ([\d.]+) \+ attempt-to-finish ([\d.]+) \(rounding may differ\).', means_text)
    require(means_match is not None, f'{round_id}: malformed arrival mean components')
    arrival['mean_planned_latency_ms'], arrival['mean_attempt_lag_ms'], arrival['mean_attempt_to_finish_ms'] = map(float, means_match.groups())
    roles = []
    for line_no, text in numbered:
        if text.startswith('| ') and cells(text)[0] in ROLES:
            c = cells(text)
            reasons = dict((key, int(count)) for key, count in (entry.strip().split(':') for entry in c[7].split(',')))
            roles.append(dict(role=c[0], n=int(c[1]), changed_pool=int(c[2]), back_to_home=int(c[3]),
                              wait_mean_ms=float(c[4]), cpu_mean_ms=float(c[5]), wall_mean_ms=float(c[6]),
                              reasons=reasons, source_line=line_no))
    role_start, _ = match(ROLE_HEADER)
    exact = []
    for line_no, text in numbered:
        if line_no > role_start and text:
            c = next(csv.reader([text]))
            require(len(c) == 5 and c[0] in ROLES, f'{round_id}: malformed exact role CSV at {line_no}')
            exact.append(dict(role=c[0], n=int(c[1]), changed_pool=int(c[2]), changed_pool_percent=float(c[3]),
                              wait_mean_ms=float(c[4]), source_line=line_no))
    same_line, same = match('SAME_WORK_AUDIT: ')
    loc.update(whole_run=whole_line, resources=seed_line, work_signature=work_line, fixed_work=fixed_line,
               arrival=arrival_line, cpu_groups=groups_line, callback_occupancy=occupancy_line,
               flow_arrival=flow_arrival_line, arrival_components=means_line, role_csv=[role_start+1, role_start+len(exact)],
               same_work_audit=same_line)
    result = dict(round_id=round_id, policy=policy, cpu_capture=capture, cpu_scope=scope, report=report,
                  whole_run=whole, resources=resource, work=work, arrival=arrival, cpu_groups=groups,
                  callback_occupancy_text=occupancy, flow_arrival_text=flow_arrival,
                  roles=roles, role_csv=exact, same_work_audit=same, source_lines=loc)
    result['audit'], result['derived'] = audit_round(result, rows, command)
    return result, rows


def audit_round(r, rows, command):
    name, scope, report, groups = r['round_id'], r['cpu_scope'], r['report'], r['cpu_groups']
    opts, capture = command['demo_options'], r['cpu_capture']
    require(r['policy'] == name.split('-', 1)[1] == opts['policy'], f'{name}: policy mismatch')
    require(capture['status'] == 'complete' and capture['child_returncode'] == 0 and not capture['capture_errors'], f'{name}: capture failed')
    require(report['model'] == opts['model'] and report['continuation'] == opts['continuation'], f'{name}: routing command/report mismatch')
    require(r['resources']['seed'] == int(opts['seed']) and r['resources']['threads_per_pool'] == int(opts['threads'])
            and r['resources']['affinity'] == opts['affinity'], f'{name}: resource command/report mismatch')
    cap_opts = command['capture_options']
    require(capture['interval_ms'] == float(cap_opts['interval_ms']) and scope['warmup_seconds'] == float(cap_opts['warmup_seconds'])
            and scope['window_seconds'] == float(cap_opts['window_seconds']), f'{name}: capture command/scope mismatch')
    require(opts['cpus'] == cap_opts['partitions'], f'{name}: capture/child partition mismatch')
    expected_groups = []
    for part in opts['cpus'].split(';'):
        cpus = []
        for token in part.split(','):
            endpoints = [int(x) for x in token.split('-')]
            cpus.extend(range(endpoints[0], endpoints[-1]+1))
        expected_groups.append(cpus)
    require([g['cpus'] for g in groups] == expected_groups, f'{name}: command/report CPU mismatch')
    require(scope['available'] and scope['window_seconds'] > 0, f'{name}: CPU scope unavailable')
    require(len(groups) == 6 == r['resources']['pool_count'] == len(scope['clusters']), f'{name}: expected six clusters')
    require(all(len(g['cpus']) == 16 and g['capacity'] == 16 for g in groups), f'{name}: expected 16 CPUs per cluster')
    require(len({c for g in groups for c in g['cpus']}) == 96, f'{name}: CPU groups overlap')
    require(capture['controller_allowed_cpus'] == sorted(c for g in groups for c in g['cpus']), f'{name}: controller mask mismatch')
    require(r['arrival']['kind'] == opts['arrival'], f'{name}: arrival command/report mismatch')
    near(r['arrival']['mean_planned_latency_ms'], report['mean_ms'], .000001, f'{name}: request mean agreement')
    near(r['arrival']['mean_attempt_lag_ms']+r['arrival']['mean_attempt_to_finish_ms'], report['mean_ms'],
         .00101, f'{name}: mean latency components')
    require(report['completed'] == report['offered'] and r['whole_run']['completed'] == int(opts['requests'])
            and r['whole_run']['rejected_or_failed'] == 0, f'{name}: completion failure')
    require(len(rows) > 0 and len(rows) % 6 == 0, f'{name}: incomplete CPU groups')
    for row in rows:
        require(row['valid'] == 1 and row['invalid_reason'] == '', f'{name}: invalid CPU window')
        require(all(row[k] is not None and math.isfinite(row[k]) for k in SERIES_FIELDS if k != 'invalid_reason'), f'{name}: missing CPU field')
        require(row['relative_end_ms'] > row['relative_start_ms'], f'{name}: nonpositive CPU duration')
        near(row['busy_cpu_equivalent'], row['util_percent']*16/100, 0.00000059, f'{name}: CPU/util at {row["source_line"]}')
    previous_end = None
    for offset in range(0, len(rows), 6):
        window = rows[offset:offset+6]
        require([x['cluster'] for x in window] == list(range(6)), f'{name}: missing/reordered cluster group')
        for field in ('relative_start_ms', 'relative_end_ms', 'process_cpu_equivalent', 'sample_lag_ms'):
            require(len({x[field] for x in window}) == 1, f'{name}: inconsistent {field} within window')
        first = window[0]
        if previous_end is not None:
            near(first['relative_start_ms'], previous_end, 0.0000011, f'{name}: CPU window gap/overlap')
        previous_end = first['relative_end_ms']
        require(first['relative_start_ms'] >= scope['warmup_seconds']*1000 and
                first['relative_end_ms'] <= (scope['warmup_seconds']+scope['window_seconds'])*1000,
                f'{name}: window outside formal scope')
    for cluster, declared in enumerate(scope['clusters']):
        selected = rows[cluster::6]
        require(declared['cluster'] == cluster and declared['cpus'] == groups[cluster]['cpus'], f'{name}: scope topology mismatch')
        require(declared['valid_windows'] == declared['complete_windows'] == len(selected), f'{name}: scope row count mismatch')
        require(not declared['invalid_reasons'] and declared['invalid_seconds'] == 0, f'{name}: scope invalid windows')
        seconds = sum((x['relative_end_ms']-x['relative_start_ms'])/1000 for x in selected)
        util = sum((x['relative_end_ms']-x['relative_start_ms'])/1000*x['util_percent'] for x in selected)/seconds
        near(seconds, declared['valid_seconds'], 0.00000001, f'{name}: cluster duration')
        near(100*seconds/scope['window_seconds'], declared['valid_coverage_percent'], 0.000001, f'{name}: cluster coverage')
        near(util, declared['util_time_weighted_mean_percent'], 0.0000006, f'{name}: weighted CPU mean')
        for suffix, p in [('p50', .50), ('p95', .95), ('p99', .99), ('max', 1)]:
            near(quantile([x['util_percent'] for x in selected], p), declared[f'window_util_{suffix}_percent'],
                 0.00000051, f'{name}: CPU {suffix}')
    proc = scope['process']
    require(proc['valid_windows'] == len(rows)//6 and not proc['invalid_reasons'], f'{name}: process coverage mismatch')
    near(proc['cpu_seconds']/proc['valid_seconds'], proc['cpu_equivalent'], 1e-9, f'{name}: process CPU arithmetic')
    near(100*proc['valid_seconds']/scope['window_seconds'], proc['valid_coverage_percent'], 1e-9, f'{name}: process coverage arithmetic')
    require([x['role'] for x in r['roles']] == ROLES == [x['role'] for x in r['role_csv']], f'{name}: role list')
    for rounded, exact in zip(r['roles'], r['role_csv']):
        for key in ('n', 'changed_pool'):
            require(rounded[key] == exact[key], f'{name}: role CSV/table disagree')
        require(sum(rounded['reasons'].values()) == rounded['n'], f'{name}: reason conservation')
        near(rounded['wait_mean_ms'], exact['wait_mean_ms'], .000501, f'{name}: role wait rounding')
        near(exact['changed_pool']/exact['n']*100, exact['changed_pool_percent'], .00000051, f'{name}: role migration arithmetic')
    widths, depths = [int(x) for x in opts['widths'].split(',')], [int(x) for x in opts['depths'].split(',')]
    require(len(widths) == len(depths) and opts['async_percent'] == '0', f'{name}: unsupported callback graph')
    per_request_roles = [1, widths[0], sum(widths[1:]), sum(w*(d-1) for w,d in zip(widths,depths)), len(widths)]
    require([x['n'] for x in r['roles']] == [report['completed']*n for n in per_request_roles], f'{name}: graph role conservation')
    require(sum(x['n'] for x in r['roles']) == report['tasks'] == report['completed']*sum(per_request_roles), f'{name}: callback conservation')
    require(sum(x['changed_pool'] for x in r['roles']) == report['migrations'], f'{name}: migration conservation')
    require(r['same_work_audit'] == 'resources, arrival, request checksums and operator work match first home', f'{name}: same-work audit absent')
    near(r['whole_run']['process_cpu_seconds']*1000/r['whole_run']['completed'],
         r['whole_run']['process_cpu_per_request_ms'], .00051, f'{name}: whole CPU/request arithmetic')
    audit = {'status': 'passed', 'cpu_rows': len(rows), 'complete_six_cluster_windows': len(rows)//6,
             'role_callbacks': report['tasks'], 'checks': ['six clusters, complete consecutive windows, finite valid counters',
             'CPU equivalents vs utilization, window coverage and duration, all cluster scope means and nearest-rank quantiles',
             'same-window repeated process fields, process scope arithmetic',
             'report/flow roles/exact CSV conservation and printed-rounding agreement',
             'same-work marker, workload/resource command agreement, zero failed requests'],
             'limitations': ['Process read timestamps are not printed in CPU_SERIES: process weighted scope is checked internally, not reconstructed from cluster timestamps.',
                            'Same-work audit is a target-side reported check; per-request checksums are not printed individually.',
                            'These checks validate transcript consistency, not raw kernel counter correctness or hardware topology.']}
    total_busy = sum(x['util_time_weighted_mean_percent']*16/100 for x in scope['clusters'])
    derived = {'cluster_cpu_equivalent_total_time_weighted_mean': total_busy,
               'selected_96_cpu_util_time_weighted_mean_percent': total_busy/96*100,
               'routing_changed_pool_percent': report['migrations']/report['tasks']*100,
               'cpu_windows_csv_rows': len(rows)}
    return audit, derived


def extract(data, source_name='transcript.log'):
    lines = data.decode('utf-8-sig').splitlines()
    blocks, active, numbered = [], None, []
    first_start = None
    for index, text in enumerate(lines):
        if text.startswith('START ROUND '):
            require(active is None, f'nested START ROUND at line {index+1}')
            active, numbered = text[12:], []
            first_start = index if first_start is None else first_start
        elif text.startswith('END ROUND '):
            require(active is not None and text[10:] == active, f'unmatched END ROUND at line {index+1}')
            blocks.append((active, numbered))
            active = None
        elif active is not None:
            numbered.append((index+1, text))
    require(active is None, 'truncated final round')
    require([x[0] for x in blocks] == ORDER, 'expected exactly six executed blocks in fixed order')
    metadata = extract_metadata(lines, first_start)
    rounds, all_rows = [], []
    for round_id, numbered in blocks:
        result, rows = parse_round(round_id, numbered, metadata['commands'][round_id])
        rounds.append(result)
        all_rows.extend(rows)
    first = rounds[0]
    for r in rounds[1:]:
        for key in ('work_signature_sha256_prefix', 'arrival_hash', 'checksum', 'fixed_work_sha256_prefix'):
            require(r['work'][key] == first['work'][key], f'{r["round_id"]}: {key} mismatch')
        require(r['resources'] == first['resources'] and r['cpu_groups'] == first['cpu_groups'] and
                {k:v for k,v in r['arrival'].items() if not k.startswith('mean_')} ==
                {k:v for k,v in first['arrival'].items() if not k.startswith('mean_')}, 'cross-round work/resource mismatch')
    summary = {'schema_version': 'cluster_cpu_log_review_v1',
               'source': {'name': Path(source_name).name, 'sha256': hashlib.sha256(data).hexdigest(),
                          'bytes': len(data), 'line_count': len(lines)},
               'metadata': metadata, 'rounds': rounds,
               'audit': {'status': 'passed', 'executed_rounds': len(rounds), 'cpu_rows': len(all_rows),
                         'scope': 'START ROUND through matching END ROUND only; PLAN and trailing repeated reports excluded',
                         'printed_numeric_precision': '6 decimals in CPU and exact role CSV; 3 decimals in report/role tables'}}
    return summary, all_rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--prefix', help='output filename prefix (default: input filename stem)')
    args = parser.parse_args(argv)
    try:
        summary, rows = extract(args.log.read_bytes(), args.log.name)
        prefix = args.prefix or args.log.stem
        require(Path(prefix).name == prefix and prefix not in ('', '.', '..'), 'prefix must be a filename component')
        args.output_dir.mkdir(parents=True, exist_ok=True)
        csv_path = args.output_dir / f'{prefix}_cpu_windows.csv'
        fields = ['round_id', 'policy', 'source_line'] + SERIES_FIELDS
        with csv_path.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)
        summary['cpu_windows_file'] = {'name': csv_path.name, 'sha256': hashlib.sha256(csv_path.read_bytes()).hexdigest(), 'rows': len(rows)}
        output = args.output_dir / f'{prefix}_summary.json'
        output.write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(f'PASS: {len(summary["rounds"])} executed rounds, {len(rows)} CPU rows; {output}')
        return 0
    except (EvidenceError, ValueError, KeyError, OSError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
