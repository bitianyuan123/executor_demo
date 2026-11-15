#!/usr/bin/env python3
"""Generate local Perfetto SQL for an explicit TID-to-executor map."""
import argparse
import csv
import pathlib


def generate(mapping, pid=None, start_ns=None, end_ns=None):
    if not mapping:
        raise ValueError("nonempty tid,pool CSV required")
    if any(tid <= 0 or not pool for tid, pool in mapping):
        raise ValueError("invalid TID/pool")
    if len({tid for tid, _ in mapping}) != len(mapping):
        raise ValueError("duplicate TIDs")
    if pid is not None and pid <= 0:
        raise ValueError("PID must be positive")
    if (start_ns is None) != (end_ns is None) or (start_ns is not None and not 0 <= start_ns < end_ns):
        raise ValueError("supply both absolute --start-ns and --end-ns in increasing order")
    def quote(value):
        return "'" + value.replace("'", "''") + "'"
    rows = ",\n".join(f"({tid},{quote(pool)})" for tid, pool in mapping)
    bounds = (f"SELECT {start_ns} AS lo, {end_ns} AS hi" if start_ns is not None
              else "SELECT start_ts AS lo, end_ts AS hi FROM trace_bounds")
    pid_filter = "" if pid is None else f"WHERE p.pid={pid}"
    return f"""-- Local read-only trace analysis. dur<0 is censored, never counted as zero.
-- Verify CPU coverage, lost events and importer event counts before interpretation.
CREATE TABLE executor_tid_map(tid INTEGER PRIMARY KEY, pool TEXT);
INSERT INTO executor_tid_map VALUES {rows};
CREATE PERFETTO TABLE executor_window AS {bounds};
CREATE PERFETTO TABLE executor_threads AS
SELECT t.utid,t.tid,t.name,t.upid,m.pool,p.pid
FROM thread t JOIN executor_tid_map m USING(tid)
LEFT JOIN process p USING(upid) {pid_filter};

SELECT 'quality_window' AS report,lo AS absolute_start_ns,hi AS absolute_end_ns,
       (hi-lo)/1e9 AS window_s FROM executor_window;
SELECT 'quality_errors' AS report,name,value FROM stats
WHERE value>0 AND severity IN ('error','data_loss');
-- Generic rows retained by the importer, NOT a count of every processed event.
-- Some scheduling events can update thread_state without a ftrace_event row.
SELECT 'stored_generic_ftrace_events' AS report,name,count(*) AS count FROM ftrace_event GROUP BY name;
SELECT 'mapping_lifetimes' AS report,m.pool,m.tid,count(t.utid) AS matched_lifetimes
FROM executor_tid_map m LEFT JOIN executor_threads t USING(tid,pool)
GROUP BY m.pool,m.tid HAVING count(t.utid)<>1;

CREATE PERFETTO TABLE executor_cpu AS
SELECT t.pool,t.tid,s.ts,s.dur,s.cpu,
       max(0,min(s.ts+s.dur,w.hi)-max(s.ts,w.lo)) AS overlap_ns
FROM sched_slice s JOIN executor_threads t USING(utid) CROSS JOIN executor_window w
WHERE s.dur>=0 AND s.ts<w.hi AND s.ts+s.dur>w.lo;
SELECT 'observed_oncpu' AS report,pool,count(*) AS closed_slices,
       count(DISTINCT tid) AS whole_window_unique_oncpu_threads,
       sum(overlap_ns)/1e6 AS oncpu_ms,
       sum(overlap_ns)*1.0/(SELECT hi-lo FROM executor_window) AS oncpu_core_equivalents
FROM executor_cpu GROUP BY pool;
SELECT 'open_cpu_slices' AS report,t.pool,count(*) AS censored_slices
FROM sched_slice s JOIN executor_threads t USING(utid) WHERE s.dur<0 GROUP BY t.pool;

-- thread_state is importer-derived. Missing R rows do NOT prove no runq wait.
SELECT 'imported_thread_states' AS report,t.pool,s.state,count(*) AS closed_intervals,
       sum(max(0,min(s.ts+s.dur,w.hi)-max(s.ts,w.lo)))/1e6 AS clipped_thread_ms
FROM thread_state s JOIN executor_threads t USING(utid) CROSS JOIN executor_window w
WHERE s.dur>=0 AND s.ts<w.hi AND s.ts+s.dur>w.lo
GROUP BY t.pool,s.state;
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tid-map", required=True)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--start-ns", type=int)
    parser.add_argument("--end-ns", type=int)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        with open(args.tid_map, newline="") as stream:
            reader = csv.DictReader(stream)
            if not {'tid', 'pool'} <= set(reader.fieldnames or []):
                parser.error("map requires tid,pool columns")
            mapping = [(int(row['tid']), row['pool']) for row in reader]
        sql = generate(mapping, args.pid, args.start_ns, args.end_ns)
    except (OSError, TypeError, ValueError) as error:
        parser.error(str(error))
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(sql)


if __name__ == '__main__':
    main()
