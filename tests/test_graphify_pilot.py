"""Synthetic pilot failures: no Graphify install, network or model calls."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('graphify_pilot', Path(__file__).resolve().parents[1] / 'scripts/graphify_pilot.py')
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


class PilotTests(unittest.TestCase):
    def exercise(self, behavior):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'new'
            def git(*args):
                if args == ('rev-parse', 'HEAD'):
                    return b'a' * 40
                if args == ('rev-parse', 'a' * 40 + '^{tree}'):
                    return b'b' * 40
                if args == ('ls-tree', '-rz', 'a' * 40):
                    return b''
                raise AssertionError(args)
            def run(*args, **kwargs):
                return behavior(output, *args, **kwargs)
            with patch.object(sys, 'argv', ['pilot', '--output', str(output)]), \
                    patch.object(pilot.importlib.metadata, 'version', return_value=pilot.VERSION), \
                    patch.object(pilot, 'git', side_effect=git), \
                    patch.object(pilot.subprocess, 'run', side_effect=run) as process:
                status = pilot.main()
            return status, json.loads((output / 'metrics.json').read_text()), process.call_count

    def test_timeout_records_failure_without_queries(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired('fixture', 180)
        status, metrics, calls = self.exercise(timeout)
        self.assertEqual((status, metrics['exit'], calls), (124, 124, 1))
        self.assertEqual(metrics['runs'][0]['error'], 'timeout')

    def test_spawn_failure_records_failure_without_queries(self):
        def missing(*args, **kwargs):
            raise OSError('private exception detail')
        status, metrics, calls = self.exercise(missing)
        self.assertEqual((status, metrics['exit'], calls), (1, 1, 1))
        self.assertNotIn('private', json.dumps(metrics))

    def test_invalid_or_missing_graph_keeps_extraction_record(self):
        for graph_content in (None, '{invalid', '{}', '{"nodes": {}, "edges": []}'):
            with self.subTest(graph_content=graph_content):
                def invalid(output, *args, **kwargs):
                    if graph_content is not None:
                        graph = output / 'result/graphify-out/graph.json'
                        graph.parent.mkdir(parents=True)
                        graph.write_text(graph_content)
                    return subprocess.CompletedProcess([], 0, '', '')
                status, metrics, calls = self.exercise(invalid)
                self.assertEqual((status, metrics['exit'], calls), (1, 1, 1))
                self.assertEqual(metrics['runs'][0]['exit'], 0)
                self.assertEqual(metrics['error'], 'artifact_unavailable_or_invalid')

    def test_success_and_query_failure_preserve_all_results(self):
        for fail_query in (False, True):
            with self.subTest(fail_query=fail_query):
                def result(output, argv, **kwargs):
                    if argv[3] == 'extract':
                        graph = output / 'result/graphify-out/graph.json'
                        graph.parent.mkdir(parents=True)
                        graph.write_text('{"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}')
                        return subprocess.CompletedProcess(argv, 0, '', '')
                    if fail_query and argv[3] == 'query':
                        raise subprocess.TimeoutExpired(argv, 180)
                    code = 4 if '__jarvis_missing_node__' in argv else 3 if 'build' in argv else 0
                    return subprocess.CompletedProcess(argv, code, 'TRUNCATED' if argv[3] == 'query' else '', '')
                status, metrics, calls = self.exercise(result)
                self.assertEqual((status, metrics['exit']), (int(fail_query), int(fail_query)))
                self.assertEqual(calls, 9)
                self.assertEqual(len(metrics['runs']), 9)


if __name__ == '__main__':
    unittest.main()
