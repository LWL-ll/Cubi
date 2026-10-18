"""判题客户端测试：状态映射 + 三接口封装 + 重试/异常分类

全部用例 mock HTTP 层（urllib.request.urlopen），不发真实网络请求。
重试策略的断言口径（docs/OJ对接设计.md 第十五章）：
可自愈错误（网络/5xx/Internal Error）恰好重试 1 次（urlopen 调用 2 次）；
不可自愈错误（401/400/File Error）不重试（调用 1 次）。
"""
import io
import json
import urllib.error
from unittest import mock

from django.test import SimpleTestCase

from teaching import judge
from teaching.judge import (AuthError, BadRequestError, JudgeClientError,
                            NetworkError, SandboxError)

SAMPLE_RESULT = {
    'status': 'Accepted',
    'exitStatus': 0,
    'time': 1826422,
    'memory': 2400256,
    'runTime': 27833769,
    'files': {'stderr': '', 'stdout': 'hello-oj\n'},
    'fileIds': {},
    'error': None,
    'fileError': [],
}


class FakeResponse:
    """模拟 urlopen 的响应对象（支持 with 语法）"""

    def __init__(self, status=200, body=b''):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def makeHttpError(code):
    """构造 urllib 的 HTTPError（模拟 401/400/500 等响应）"""
    return urllib.error.HTTPError(
        'https://oj.liuwenli.cn', code, 'error', {}, io.BytesIO(b''))


def makeResults(status='Accepted'):
    """生成合法 Result 数组（可选覆盖 status）"""
    return [{**SAMPLE_RESULT, 'status': status}]


class JudgeClientTests(SimpleTestCase):
    """判题客户端：mock HTTP 层测试（无真实网络请求）"""

    def setUp(self):
        self.patcher = mock.patch('teaching.judge.urllib.request.urlopen')
        self.mockUrlopen = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _assertCallCount(self, count):
        self.assertEqual(self.mockUrlopen.call_count, count)

    # ---------- 状态映射 ----------

    def test_mapStatusCoversAllLiterals(self):
        """9 种原始字面量全部映射正确（映射表与状态语义不可漂移）"""
        expected = {
            'Accepted': 'accepted',
            'Time Limit Exceeded': 'time_limit_exceeded',
            'Memory Limit Exceeded': 'memory_limit_exceeded',
            'Output Limit Exceeded': 'output_limit_exceeded',
            'Nonzero Exit Status': 'runtime_error',
            'Signalled': 'runtime_error',
            'Dangerous Syscall': 'runtime_error',
            'File Error': 'system_error',
            'Internal Error': 'system_error',
        }
        for literal, mapped in expected.items():
            self.assertEqual(judge.mapStatus(literal), mapped, literal)

    def test_mapStatusUnknownFallsBackToSystemError(self):
        """未知字面量兜底 system_error（防沙箱升级新增状态污染业务数据）"""
        self.assertEqual(judge.mapStatus('Brand New Status'), 'system_error')

    # ---------- submitRun ----------

    def test_submitRunReturnsResults(self):
        """200 + 合法 Result 数组 → 原样返回"""
        self.mockUrlopen.return_value = FakeResponse(
            body=json.dumps(makeResults()).encode())
        results = judge.submitRun([{'cmd': 'mock'}])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['status'], 'Accepted')
        self._assertCallCount(1)

    def test_submitRunSendsPostToRunWithBearer(self):
        """请求打到 POST /run，带 Bearer 认证与判题超时"""
        self.mockUrlopen.return_value = FakeResponse(
            body=json.dumps(makeResults()).encode())
        judge.submitRun([{'cmd': 'mock'}])
        request = self.mockUrlopen.call_args.args[0]
        self.assertEqual(request.method, 'POST')
        self.assertTrue(request.full_url.endswith('/run'))
        self.assertEqual(
            request.headers['Authorization'],
            f"Bearer {judge.settings.OJ_AUTH_TOKEN}")
        # 超时必须使用 OJ_RUN_TIMEOUT（settings 配置）
        self.assertEqual(
            self.mockUrlopen.call_args.kwargs['timeout'],
            judge.settings.OJ_RUN_TIMEOUT)

    def test_networkErrorRetriesOnce(self):
        """网络错误重试 1 次 → 仍失败抛 NetworkError（调用恰好 2 次）"""
        self.mockUrlopen.side_effect = [
            urllib.error.URLError('connection refused'),
            urllib.error.URLError('connection refused'),
        ]
        with self.assertRaises(NetworkError):
            judge.submitRun([{'cmd': 'mock'}])
        self._assertCallCount(2)

    def test_5xxRetriesOnce(self):
        """沙箱 500 重试 1 次 → 仍失败抛 NetworkError（调用恰好 2 次）"""
        self.mockUrlopen.side_effect = [makeHttpError(500), makeHttpError(502)]
        with self.assertRaises(NetworkError):
            judge.submitRun([{'cmd': 'mock'}])
        self._assertCallCount(2)

    def test_401NoRetry(self):
        """401 token 失效：抛 AuthError 且不重试（调用恰好 1 次）"""
        self.mockUrlopen.side_effect = makeHttpError(401)
        with self.assertRaises(AuthError):
            judge.submitRun([{'cmd': 'mock'}])
        self._assertCallCount(1)

    def test_400NoRetry(self):
        """400 请求构造错误：抛 BadRequestError 且不重试（调用恰好 1 次）"""
        self.mockUrlopen.side_effect = makeHttpError(400)
        with self.assertRaises(BadRequestError):
            judge.submitRun([{'cmd': 'mock'}])
        self._assertCallCount(1)

    def test_sandboxInternalErrorRetriedThenSucceeds(self):
        """沙箱 Internal Error 重试 1 次 → 第二次正常则成功（调用 2 次）"""
        self.mockUrlopen.side_effect = [
            FakeResponse(body=json.dumps(
                makeResults('Internal Error')).encode()),
            FakeResponse(body=json.dumps(makeResults()).encode()),
        ]
        results = judge.submitRun([{'cmd': 'mock'}])
        self.assertEqual(results[0]['status'], 'Accepted')
        self._assertCallCount(2)

    def test_sandboxInternalErrorTwiceFails(self):
        """沙箱 Internal Error 连续 2 次 → 抛 SandboxError"""
        self.mockUrlopen.side_effect = [
            FakeResponse(body=json.dumps(
                makeResults('Internal Error')).encode()),
            FakeResponse(body=json.dumps(
                makeResults('Internal Error')).encode()),
        ]
        with self.assertRaises(SandboxError):
            judge.submitRun([{'cmd': 'mock'}])
        self._assertCallCount(2)

    def test_unknownStatusRaisesSandboxError(self):
        """响应 status 为未知字面量 → 结构校验拦截抛 SandboxError"""
        self.mockUrlopen.return_value = FakeResponse(
            body=json.dumps(makeResults('Brand New Status')).encode())
        with self.assertRaises(SandboxError):
            judge.submitRun([{'cmd': 'mock'}])

    def test_invalidJsonRaisesSandboxError(self):
        """响应不是合法 JSON → 抛 SandboxError"""
        self.mockUrlopen.return_value = FakeResponse(body=b'<html>502</html>')
        with self.assertRaises(SandboxError):
            judge.submitRun([{'cmd': 'mock'}])

    def test_nonArrayResponseRaisesSandboxError(self):
        """响应是 JSON 但不是数组 → 抛 SandboxError"""
        self.mockUrlopen.return_value = FakeResponse(body=b'{"status": "Accepted"}')
        with self.assertRaises(SandboxError):
            judge.submitRun([{'cmd': 'mock'}])

    # ---------- uploadFile ----------

    def test_uploadFileReturnsFileId(self):
        """上传成功：dict 形态 {"fileId"} 键名返回 fileId（兼容分支）"""
        self.mockUrlopen.return_value = FakeResponse(body=b'{"fileId": "7MEHVO76"}')
        self.assertEqual(judge.uploadFile(b'hello'), '7MEHVO76')

    def test_uploadFileAcceptsIdsFormat(self):
        """兼容 dict 形态 {"ids": {"file"}} 键名"""
        self.mockUrlopen.return_value = FakeResponse(
            body=b'{"ids": {"file": "7MEHVO76"}}')
        self.assertEqual(judge.uploadFile(b'hello'), '7MEHVO76')

    def test_uploadFileAcceptsStringForm(self):
        """实测形态（v1.13.3）：响应是纯 JSON 字符串 fileId"""
        self.mockUrlopen.return_value = FakeResponse(body=b'"ZSGDNRTD"')
        self.assertEqual(judge.uploadFile(b'hello'), 'ZSGDNRTD')

    def test_uploadFileMissingFileIdRaisesSandboxError(self):
        """响应缺少 fileId → 抛 SandboxError"""
        self.mockUrlopen.return_value = FakeResponse(body=b'{}')
        with self.assertRaises(SandboxError):
            judge.uploadFile(b'hello')

    def test_uploadFileUsesMultipart(self):
        """请求为 multipart/form-data 且带 file 字段与文件内容"""
        self.mockUrlopen.return_value = FakeResponse(body=b'{"fileId": "X"}')
        judge.uploadFile(b'hello-content')
        request = self.mockUrlopen.call_args.args[0]
        self.assertEqual(request.method, 'POST')
        self.assertTrue(request.full_url.endswith('/file'))
        # urllib 会把 Content-Type 重写为 Content-type（小写 t），
        # 断言时统一小写键名再取
        headerDict = {k.lower(): v for k, v in request.headers.items()}
        self.assertIn('multipart/form-data', headerDict['content-type'])
        # multipart 请求体必须包含文件内容与 file 字段名
        body = request.data
        self.assertIn(b'name="file"', body)
        self.assertIn(b'hello-content', body)

    # ---------- deleteFile ----------

    def test_deleteFileSuccess(self):
        """DELETE 200 → 正常完成，无异常"""
        self.mockUrlopen.return_value = FakeResponse(body=b'')
        judge.deleteFile('7MEHVO76')  # 不抛异常即通过

    def test_deleteFile404TreatedAsCleaned(self):
        """404（文件已不存在）→ 视为已清理，不抛异常（幂等清理语义）"""
        self.mockUrlopen.side_effect = makeHttpError(404)
        judge.deleteFile('7MEHVO76')  # 不抛异常即通过

    def test_deleteFileOther4xxRaised(self):
        """400 等非 404 的 4xx → 抛 BadRequestError"""
        self.mockUrlopen.side_effect = makeHttpError(400)
        with self.assertRaises(BadRequestError):
            judge.deleteFile('7MEHVO76')
