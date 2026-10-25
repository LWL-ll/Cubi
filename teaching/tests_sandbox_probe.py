"""沙箱探针的接口保护、保密及产物清理回归，不连接真实沙箱。"""
import io
import os
from pathlib import Path
import runpy
import urllib.error
from contextlib import redirect_stdout
from unittest import mock

from django.test import SimpleTestCase, override_settings

from scripts import sandbox_probe as probe
from teaching import judge


@override_settings(OJ_AUTH_TOKEN='PROBE_SECRET_SENTINEL')
class SandboxProbeTests(SimpleTestCase):
    def setUp(self):
        probe.results.clear()
        self.output = io.StringIO()
        self.capture = redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def test_closed_config_continues_functional_checks(self):
        for code in (403, 404):
            error = urllib.error.HTTPError('https://example/config', code, '', {}, None)
            with self.subTest(code=code), mock.patch.object(
                    probe, 'fetchConfig', side_effect=error), mock.patch.object(
                    probe, 'probeVersion') as version, mock.patch.object(
                    probe, 'probeGcc'), mock.patch.object(probe, 'probePython'), mock.patch.object(
                    probe, 'probePaths'), mock.patch.object(probe, 'probeMemoryLimit'):
                self.assertEqual(probe.main(), 2)
                version.assert_called_once()
                self.assertEqual([r[1] for r in probe.results], ['PASS', 'PENDING'])

    def test_public_config_is_failure_and_never_dumps_config(self):
        for controllers, expected in ((['cpu', 'memory'], 'PASS'), (['cpu'], 'FAIL')):
            probe.results.clear()
            config = {'secret': 'PROBE_SECRET_SENTINEL',
                      'runnerConfig': {'cgroupControllers': controllers,
                                       'seccomp': True, 'other': 'DO_NOT_DUMP'}}
            with self.subTest(controllers=controllers), mock.patch.object(
                    probe, 'fetchConfig', return_value=config):
                probe.probeConfig()
                self.assertEqual(probe.results[0][1], 'FAIL')
                self.assertEqual(probe.results[1][1], expected)
                self.assertEqual(probe.summarize(), 1)
        self.assertNotIn('PROBE_SECRET_SENTINEL', self.output.getvalue())
        self.assertNotIn('DO_NOT_DUMP', self.output.getvalue())

    def test_config_faults_are_not_protection_success(self):
        faults = [
            urllib.error.HTTPError('https://example/config', 500, '', {}, None),
            TimeoutError('PROBE_SECRET_SENTINEL'),
            ValueError('PROBE_SECRET_SENTINEL'),
        ]
        for fault in faults:
            probe.results.clear()
            with self.subTest(fault=type(fault).__name__), mock.patch.object(
                    probe, 'fetchConfig', side_effect=fault):
                probe.probeConfig()
                self.assertEqual(probe.summarize(), 1)
                self.assertFalse(any(r[1] == 'PENDING' for r in probe.results))
        self.assertNotIn('PROBE_SECRET_SENTINEL', self.output.getvalue())

    def test_error_responses_and_token_are_not_printed(self):
        with mock.patch.object(judge, 'submitRun', side_effect=judge.SandboxError(
                'PROBE_SECRET_SENTINEL DO_NOT_DUMP')):
            self.assertIsNone(probe.rawRunDiag([], '执行'))
        probe.record('测试', False, 'PROBE_SECRET_SENTINEL')
        self.assertNotIn('PROBE_SECRET_SENTINEL', self.output.getvalue())
        self.assertNotIn('DO_NOT_DUMP', self.output.getvalue())

    def gccRun(self, outcomes, deleteErrors=None):
        with mock.patch.object(judge, 'uploadFile', return_value='source'), mock.patch.object(
                probe, 'rawRunDiag', side_effect=outcomes), mock.patch.object(
                judge, 'deleteFile', side_effect=deleteErrors) as delete:
            try:
                probe.probeGcc()
            except RuntimeError:
                pass
            return delete.call_args_list

    def test_c_success_cleans_source_and_binary(self):
        version = [{'status': 'Accepted'}]
        compiled = [{'status': 'Accepted', 'fileIds': {'main': 'binary'}}]
        run = [{'status': 'Accepted', 'files': {'stdout': 'HELLO_PROBE\n'}}]
        self.assertEqual(self.gccRun([version, compiled, run, run]),
                         [mock.call('source'), mock.call('binary')])
        self.assertFalse(any(r[1] == 'FAIL' for r in probe.results))

    def test_compile_failure_still_cleans_obtained_artifacts(self):
        self.assertEqual(self.gccRun([
            [{'status': 'Accepted'}],
            [{'status': 'Nonzero Exit Status', 'fileIds': {'main': 'binary'}}],
        ]), [mock.call('source'), mock.call('binary')])

    def test_failures_at_each_stage_cleanup(self):
        version = [{'status': 'Accepted'}]
        compiled = [{'status': 'Accepted', 'fileIds': {'main': 'binary'}}]
        for outcomes, expected in (
            ([RuntimeError('interrupted')], ['source']),
            ([version, None], ['source']),
            ([version, compiled, None], ['source', 'binary']),
            ([version, compiled, RuntimeError('interrupted')], ['source', 'binary']),
        ):
            with self.subTest(stage=len(outcomes)):
                self.assertEqual(self.gccRun(outcomes), [mock.call(x) for x in expected])

    def test_first_delete_failure_does_not_skip_second(self):
        calls = self.gccRun([
            [{'status': 'Accepted'}],
            [{'status': 'Nonzero Exit Status', 'fileIds': {'main': 'binary'}}],
        ], [judge.SandboxError('delete failed'), None])
        self.assertEqual(calls, [mock.call('source'), mock.call('binary')])
        self.assertEqual([r[1] for r in probe.results if r[0] == 'C 临时文件清理'],
                         ['FAIL', 'PASS'])

    def test_exit_codes(self):
        for passed, expected in ((True, 0), (False, 1), (None, 2)):
            probe.results.clear()
            probe.record('测试', passed, '')
            self.assertEqual(probe.summarize(), expected)
        probe.results.clear()
        probe.record('内部配置', None, '')
        probe.record('执行失败', False, '')
        self.assertEqual(probe.summarize(), 1)


class QueueAcceptanceIsolationTests(SimpleTestCase):
    def test_missing_parameters_cannot_fall_back_to_business_database(self):
        with mock.patch.dict(os.environ, {'OJ_ACCEPTANCE_DB': '', 'OJ_ACCEPTANCE_RUN': ''}):
            with self.assertRaisesRegex(RuntimeError, '独立临时数据库'):
                runpy.run_module('codemind.settings_oj_acceptance')

    def test_business_database_is_rejected_before_opening(self):
        database = Path(__file__).resolve().parent.parent / 'db.sqlite3'
        with mock.patch.dict(os.environ, {
                'OJ_ACCEPTANCE_DB': str(database), 'OJ_ACCEPTANCE_RUN': 'isolation-test'}):
            with self.assertRaisesRegex(RuntimeError, '不属于本次临时目录'):
                runpy.run_module('codemind.settings_oj_acceptance')
