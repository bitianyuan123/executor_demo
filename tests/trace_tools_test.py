#!/usr/bin/env python3
"""Offline behavior tests for explicit worker maps and generated trace SQL.

No real /proc process is inspected and no Perfetto package is downloaded.
SQLite exercises the portable query logic after replacing only Perfetto's
materialized-table declaration syntax; this is not an importer compatibility test.
"""

import csv
import importlib.util
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TID_MAP = ROOT / 'scripts' / 'executor_tid_map.py'
SQL_TOOL = ROOT / 'scripts' / 'trace_processor_report_sql.py'
spec = importlib.util.spec_from_file_location('trace_sql_generator', SQL_TOOL)
sql_generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sql_generator)


def run_tool(script, *arguments):
    return subprocess.run([sys.executable, str(script), *map(str, arguments)],
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=10)


class TidMappingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='trace-map-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pid = 777
        self.tasks = self.root / 'proc' / str(self.pid) / 'task'
        self.tasks.mkdir(parents=True)
        self.output = self.root / 'nested' / 'workers.csv'

    def thread(self, tid, comm, starttime):
        directory = self.tasks / str(tid)
        directory.mkdir()
        (directory / 'comm').write_text(comm + '\n')
        # remainder[0] is stat field 3 (state); remainder[19] is field 22.
        fields = ['S', *(['0'] * 18), str(starttime), *(['0'] * 10)]
        (directory / 'stat').write_text(f'{tid} ({comm}) ' + ' '.join(fields) + '\n')
        return directory

    def invoke(self, *extra):
        return run_tool(TID_MAP, '--pid', self.pid, '--proc-root', self.root / 'proc', *extra)

    def test_identical_names_complex_comm_starttime_and_vanished_threads(self):
        self.thread(102, 'CPU pool alpha', 222)
        self.thread(101, 'CPU pool alpha', 111)
        complex_name = "CPU pool beta (rank) ) worker, O'Brien"
        self.thread(103, complex_name, 999999999)
        self.thread(104, 'io worker', 444)
        missing = self.tasks / '105'
        missing.mkdir()
        (missing / 'comm').write_text('CPU pool alpha\n')
        result = self.invoke('--pool', 'alpha=^CPU pool alpha$', '--pool', 'beta=^CPU pool beta',
                             '--output', self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('live_threads=4 missing_or_invalid_during_snapshot=1 mapped=3', result.stdout)
        with self.output.open(newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([int(row['tid']) for row in rows], [101, 102, 103])
        self.assertEqual([int(row['starttime']) for row in rows], [111, 222, 999999999])
        self.assertEqual(rows[-1]['comm'], complex_name)
        self.assertEqual(rows[-1]['pool'], 'beta')

    def test_malformed_stat_identity_and_starttime_are_not_mapped(self):
        self.thread(101, 'CPU valid', 111)
        # Each malformed record still has enough tokens for field 22: token
        # count alone must not turn a corrupt stat record into a worker map.
        fields = ' '.join(['S', *(['0'] * 18), '222', *(['0'] * 10)])
        malformed = {
            102: f'102 CPU missing opener) {fields}\n',
            103: f'103 (CPU missing closer {fields}\n',
            104: f'999 (CPU mismatched tid) {fields}\n',
            105: '105 (CPU negative birth) ' + fields.replace('222', '-1') + '\n',
            106: '106 (CPU nonnumeric birth) ' + fields.replace('222', 'unknown') + '\n',
            107: '107 (CPU nonascii birth) ' + fields.replace('222', '٢٢٢') + '\n',
        }
        for tid, stat in malformed.items():
            directory = self.thread(tid, 'CPU malformed', 222)
            (directory / 'stat').write_text(stat)
        result = self.invoke('--pool', 'p=^CPU', '--output', self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('live_threads=1 missing_or_invalid_during_snapshot=6 mapped=1', result.stdout)
        with self.output.open(newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([(row['tid'], row['starttime']) for row in rows], [('101', '111')])

    def test_all_invalid_stat_records_fail_without_writing_mapping(self):
        self.thread(101, 'CPU negative birth', -1)
        result = self.invoke('--pool', 'p=^CPU', '--output', self.output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('live_threads=0 missing_or_invalid_during_snapshot=1 mapped=0', result.stdout)
        self.assertFalse(self.output.exists())

    def test_inventory_without_rules_does_not_guess_pools(self):
        self.thread(101, 'CPU group 0', 111)
        self.thread(102, 'CPU group 0', 222)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('mapped=0', result.stdout)
        self.assertIn("comm='CPU group 0' threads=2", result.stdout)
        self.assertFalse(self.output.exists())

    def test_ambiguous_rules_and_unmatched_map_fail_before_writing(self):
        self.thread(101, 'CPU pool alpha', 111)
        cases = [('--pool', 'one=CPU', '--pool', 'two=alpha'),
                 ('--pool', 'same=CPU', '--pool', 'same=alpha'),
                 ('--pool', 'none=does-not-match',), ()]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments, '--output', self.output)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())

    def test_bad_pool_rules_and_pid_are_rejected(self):
        self.thread(101, 'CPU pool alpha', 111)
        for rule in ('missing_equals', '=CPU', 'empty=', 'broken=['):
            with self.subTest(rule=rule):
                result = self.invoke('--pool', rule, '--output', self.output)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())
        result = run_tool(TID_MAP, '--pid', '-1', '--proc-root', self.root / 'proc')
        self.assertNotEqual(result.returncode, 0)

    def test_csv_pool_label_quoting_round_trips_into_sql(self):
        self.thread(101, "CPU O'Brien, worker", 111)
        pool = "pool,O'Brien; DROP TABLE process; --"
        result = self.invoke('--pool', pool + '=^CPU', '--output', self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        sql_output = self.root / 'sql' / 'report.sql'
        result = run_tool(SQL_TOOL, '--tid-map', self.output, '--pid', self.pid,
                          '--start-ns', '100', '--end-ns', '200', '--output', sql_output)
        self.assertEqual(result.returncode, 0, result.stderr)
        sql = sql_output.read_text()
        self.assertIn("'pool,O''Brien; DROP TABLE process; --'", sql)
        connection = make_trace_fixture()
        self.addCleanup(connection.close)
        connection.executescript(sql.replace('CREATE PERFETTO TABLE', 'CREATE TABLE'))
        self.assertEqual(connection.execute('SELECT pool FROM executor_tid_map').fetchone()[0], pool)
        self.assertEqual(connection.execute('SELECT count(*) FROM process').fetchone()[0], 2)


def make_trace_fixture():
    connection = sqlite3.connect(':memory:')
    connection.executescript('''
      CREATE TABLE trace_bounds(start_ts INTEGER,end_ts INTEGER);
      INSERT INTO trace_bounds VALUES(0,1000);
      CREATE TABLE thread(utid INTEGER,tid INTEGER,name TEXT,upid INTEGER);
      CREATE TABLE process(upid INTEGER,pid INTEGER);
      INSERT INTO process VALUES(10,777),(20,888);
      INSERT INTO thread VALUES(1,101,'CPU alpha',10),(2,102,'CPU beta',10),
                               (3,101,'reused TID in another process',20);
      CREATE TABLE stats(name TEXT,value INTEGER,severity TEXT);
      INSERT INTO stats VALUES('lost',2,'data_loss'),('ok',0,'error'),('info',3,'info');
      CREATE TABLE ftrace_event(name TEXT);
      INSERT INTO ftrace_event VALUES('sched_switch'),('sched_switch'),('sched_waking');
      CREATE TABLE sched_slice(utid INTEGER,ts INTEGER,dur INTEGER,cpu INTEGER);
      INSERT INTO sched_slice VALUES(1,80,40,0),(1,150,100,0),(1,190,-1,0),
                                    (1,250,20,0),(2,110,30,1),(3,120,20,2);
      CREATE TABLE thread_state(utid INTEGER,ts INTEGER,dur INTEGER,state TEXT);
      INSERT INTO thread_state VALUES(1,50,100,'R'),(1,180,50,'S'),(1,195,-1,'R'),
                                     (2,100,100,'Running'),(3,110,20,'R');
    ''')
    return connection


def report_statement(sql, name):
    match = re.search(r"SELECT '" + re.escape(name) + r"'.*?;", sql, re.DOTALL)
    if not match:
        raise AssertionError(f'missing report {name}')
    return match.group(0)


class GeneratedSqlTest(unittest.TestCase):
    def test_closed_intervals_are_clipped_and_censored_intervals_are_separate(self):
        sql = sql_generator.generate([(101, 'a'), (102, 'b')], pid=777, start_ns=100, end_ns=200)
        connection = make_trace_fixture()
        self.addCleanup(connection.close)
        connection.executescript(sql.replace('CREATE PERFETTO TABLE', 'CREATE TABLE'))
        self.assertEqual(connection.execute('SELECT pool,overlap_ns FROM executor_cpu ORDER BY pool,ts').fetchall(),
                         [('a', 20), ('a', 50), ('b', 30)])
        oncpu = connection.execute(report_statement(sql, 'observed_oncpu')).fetchall()
        a = next(row for row in oncpu if row[1] == 'a')
        self.assertEqual(a[2:4], (2, 1))
        self.assertAlmostEqual(a[-1], .7)
        self.assertEqual(connection.execute(report_statement(sql, 'open_cpu_slices')).fetchall(),
                         [('open_cpu_slices', 'a', 1)])
        states = connection.execute(report_statement(sql, 'imported_thread_states')).fetchall()
        state_a = {(row[2], row[3]): row[4] for row in states if row[1] == 'a'}
        self.assertAlmostEqual(state_a[('R', 1)], .00005)
        self.assertAlmostEqual(state_a[('S', 1)], .00002)
        self.assertEqual(connection.execute(report_statement(sql, 'mapping_lifetimes')).fetchall(), [])
        self.assertEqual(connection.execute(report_statement(sql, 'quality_errors')).fetchall(),
                         [('quality_errors', 'lost', 2)])

    def test_missing_and_reused_tid_lifetimes_are_reported(self):
        sql = sql_generator.generate([(101, 'a'), (999, 'missing')])
        connection = make_trace_fixture()
        self.addCleanup(connection.close)
        connection.executescript(sql.replace('CREATE PERFETTO TABLE', 'CREATE TABLE'))
        lifetimes = connection.execute(report_statement(sql, 'mapping_lifetimes')).fetchall()
        self.assertEqual(set(lifetimes), {('mapping_lifetimes', 'a', 101, 2),
                                         ('mapping_lifetimes', 'missing', 999, 0)})
        self.assertEqual(connection.execute('SELECT lo,hi FROM executor_window').fetchall(), [(0, 1000)])

    def test_generate_rejects_invalid_ids_duplicates_and_partial_or_bad_windows(self):
        for mapping in ([], [(0, 'p')], [(-2, 'p')], [(1, '')], [(1, 'a'), (1, 'b')]):
            with self.subTest(mapping=mapping):
                with self.assertRaises(ValueError):
                    sql_generator.generate(mapping)
        for options in ({'pid': 0}, {'pid': -1}, {'start_ns': 1}, {'end_ns': 2},
                        {'start_ns': -1, 'end_ns': 2}, {'start_ns': 2, 'end_ns': 2},
                        {'start_ns': 3, 'end_ns': 2}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    sql_generator.generate([(1, 'p')], **options)

    def test_cli_invalid_csv_cannot_produce_sql(self):
        with tempfile.TemporaryDirectory(prefix='trace-sql-cli-test-') as temporary:
            root = Path(temporary)
            mapping, output = root / 'map.csv', root / 'report.sql'
            for contents in ('pid,pool\n1,a\n', 'tid,pool\nnot-a-number,a\n',
                             'tid,pool\n0,a\n', 'tid,pool\n1,a\n1,b\n',
                             'tid,pool\n1,\n', 'tid,pool\n'):
                with self.subTest(contents=contents):
                    mapping.write_text(contents)
                    result = run_tool(SQL_TOOL, '--tid-map', mapping, '--output', output)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(output.exists())
            mapping.write_text('tid,pool\n1,a\n')
            result = run_tool(SQL_TOOL, '--tid-map', mapping, '--start-ns', '100', '--output', output)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
