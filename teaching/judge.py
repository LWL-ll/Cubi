"""go-judge 判题沙箱客户端：HTTP 封装 + 状态集中映射

对接 go-judge v1.13.3 新版 API（docs/OJ对接设计.md 第三/四章，2026-10-18 实测）：
- submitRun：POST /run 判题，请求体为 Request 对象 {"cmd": [...]}（4.1），
  一次请求同步跑完，返回 Result 数组（4.2）
- uploadFile：POST /file 上传文件（multipart，字段名 file），
  响应为纯 JSON 字符串 fileId（实测形态，如 "ZSGDNRTD"）
- deleteFile：DELETE /file/<fileId> 删除文件（释放沙箱磁盘）

重试策略（docs/OJ对接设计.md 第十五章）：
- 网络错误/超时/5xx → 透明重试 1 次，仍失败抛 NetworkError
- 401 → AuthError 不重试（token 失效，重试无意义，提示运维轮换）
- 400/其他 4xx → BadRequestError 不重试（请求构造错误，重试同样错）
- 沙箱 Internal Error → 整体重试 1 次，仍失败抛 SandboxError
- File Error → 不重试（保守默认：go-judge fileError 细分类型尚无实测数据，
  一律按确定性错误处理；实测到瞬时类型（磁盘/系统繁忙）再放开重试）

安全（docs/OJ对接设计.md 第十四章）：
- 凭证只从 settings.OJ_AUTH_TOKEN（环境变量）读取，严禁硬编码
- 响应结构校验：status 必须属于 9 种原始字面量，
  防沙箱异常响应污染业务数据（安全设计第 4 条）
"""
import json
import logging
import urllib.error
import urllib.request
import uuid

from django.conf import settings

logger = logging.getLogger('codemind.judge')

# go-judge 9 种原始状态字面量（带空格，实测于 2026-10-18，勿改）
GO_JUDGE_STATUSES = frozenset([
    'Accepted',
    'Time Limit Exceeded',
    'Memory Limit Exceeded',
    'Output Limit Exceeded',
    'Nonzero Exit Status',
    'Signalled',
    'Dangerous Syscall',
    'File Error',
    'Internal Error',
])

# 状态集中映射：go-judge 原始字面量 → Submission.status（第九章模型草案）
# 未知字面量一律 system_error 兜底（防沙箱升级新增状态污染业务数据）
JUDGE_STATUS_MAP = {
    'Accepted': 'accepted',                 # 未判对错：后续进 stdout 比对（或 spj）
    'Time Limit Exceeded': 'time_limit_exceeded',
    'Memory Limit Exceeded': 'memory_limit_exceeded',
    'Output Limit Exceeded': 'output_limit_exceeded',
    'Nonzero Exit Status': 'runtime_error',
    'Signalled': 'runtime_error',
    'Dangerous Syscall': 'runtime_error',
    'File Error': 'system_error',           # 文件错误：任务层统一按系统错误
    'Internal Error': 'system_error',       # 沙箱内部错误（客户端已重试 1 次）
}

# 上传文件名（go-judge 不关心名字，固定即可）
UPLOAD_FILENAME = 'upload.bin'


class JudgeClientError(Exception):
    """判题客户端异常基类：任务层捕获后统一标记 system_error"""
    pass


class AuthError(JudgeClientError):
    """401：token 失效，不重试，提示运维轮换 OJ_AUTH_TOKEN"""
    pass


class BadRequestError(JudgeClientError):
    """4xx（除 401）：请求构造错误，不重试

    statusCode 属性供 deleteFile 区分 404（文件不存在视为已清理）。
    """

    def __init__(self, message, statusCode):
        super().__init__(message)
        self.statusCode = statusCode


class NetworkError(JudgeClientError):
    """网络错误/超时/5xx：已重试 1 次仍失败"""
    pass


class SandboxError(JudgeClientError):
    """沙箱级异常：Internal Error 重试后仍失败 / File Error / 响应结构非法"""
    pass


def mapStatus(goJudgeStatus):
    """go-judge 状态字面量 → codemind 提交状态（未知值 → system_error）

    Accepted 映射为 accepted 只是中间态：对错由任务层比对器再判
    （wrong_answer 不来自沙箱，来自 stdout 与预期输出的比对）。
    """
    return JUDGE_STATUS_MAP.get(goJudgeStatus, 'system_error')


def _buildHeaders(contentType):
    """构造请求头：Bearer 凭证只从环境变量读取（settings.OJ_AUTH_TOKEN）"""
    headers = {'Authorization': f'Bearer {settings.OJ_AUTH_TOKEN}'}
    if contentType:
        headers['Content-Type'] = contentType
    return headers


def _doRequest(method, path, body=None, contentType=None):
    """执行一次 HTTP 请求：网络层透明重试 1 次，返回 (statusCode, rawBody)

    重试只针对可自愈的错误（网络抖动/5xx/超时）；401/4xx 是请求本身
    的问题，重试无意义，立即抛出。
    """
    url = settings.OJ_BASE_URL.rstrip('/') + path
    for attempt in range(2):
        try:
            request = urllib.request.Request(
                url, data=body, headers=_buildHeaders(contentType),
                method=method)
            with urllib.request.urlopen(
                    request, timeout=settings.OJ_RUN_TIMEOUT) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise AuthError(
                    '判题沙箱认证失败（OJ_AUTH_TOKEN 失效），请运维轮换 token')
            if 400 <= e.code < 500:
                raise BadRequestError(
                    f'判题沙箱拒绝请求（HTTP {e.code}）', e.code)
            # 5xx：服务器错误，可重试
            if attempt == 0:
                logger.warning('判题沙箱返回 %s，重试 1 次', e.code)
                continue
            raise NetworkError(f'判题沙箱持续返回 HTTP {e.code}')
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 0:
                logger.warning('判题沙箱网络错误（%s），重试 1 次', e)
                continue
            raise NetworkError(f'判题沙箱网络错误（已重试）：{e}')
    # 防御性兜底：正常路径都在循环内 return/raise，此处不可达
    raise NetworkError('判题沙箱请求失败')


def submitRun(runRequest):
    """提交判题：POST /run，返回 Result 数组（与 cmd 一一对应）

    runRequest 为 go-judge cmd 数组（由任务构建器拼装），此处按新版 API
    包成 {"cmd": [...]} Request 对象（文档 4.1；裸数组形态实测被 400 拒：
    cannot unmarshal array into model.Request）。
    - 沙箱 Internal Error：整体重试 1 次（重跑是全新的沙箱进程，可自愈）
    - 响应结构校验：status 必须属于 9 种原始字面量（安全设计第 4 条）
    """
    body = json.dumps({'cmd': runRequest}).encode('utf-8')
    for attempt in range(2):
        statusCode, rawBody = _doRequest(
            'POST', '/run', body, 'application/json')
        try:
            results = json.loads(rawBody)
        except json.JSONDecodeError:
            raise SandboxError('判题响应不是合法 JSON')
        if not isinstance(results, list):
            raise SandboxError('判题响应不是数组（应为与 cmd 一一对应的 Result 列表）')
        for result in results:
            _validateResult(result)
        # 沙箱内部错误整体重试：错误随机出现，重跑一次大概率恢复
        if any(r.get('status') == 'Internal Error' for r in results):
            if attempt == 0:
                logger.warning('沙箱 Internal Error，整体重试 1 次')
                continue
            raise SandboxError('沙箱 Internal Error，重试后仍失败')
        return results
    # 防御性兜底：正常路径都在循环内 return/raise，此处不可达
    raise SandboxError('判题请求失败')


def _validateResult(result):
    """校验单条 Result 结构：防沙箱异常响应污染业务数据

    status 必须是 9 种原始字面量之一；files/fileIds/fileError 若存在
    必须是 dict。stdout 的存在性由任务层校验（它知道自己声明了什么）。
    """
    if not isinstance(result, dict):
        raise SandboxError(f'判题响应条目不是对象：{result!r}')
    status = result.get('status')
    if status not in GO_JUDGE_STATUSES:
        raise SandboxError(f'沙箱返回未知状态字面量：{status!r}')
    for field in ('files', 'fileIds'):
        value = result.get(field)
        if value is not None and not isinstance(value, dict):
            raise SandboxError(f'判题响应字段 {field} 结构非法')
    # fileError 是细分错误清单（实测为空数组，见 OJ对接设计.md 4.2 响应示例）
    fileError = result.get('fileError')
    if fileError is not None and not isinstance(fileError, list):
        raise SandboxError('判题响应字段 fileError 结构非法')


def uploadFile(fileContent):
    """上传文件到沙箱：POST /file（multipart，字段名 file），返回 fileId

    一期用途：大测试点输入文件上传后以 {"fileId"} 引用（copyIn）。
    """
    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\n'.encode('utf-8')
        + f'Content-Disposition: form-data; name="file"; '
          f'filename="{UPLOAD_FILENAME}"\r\n'.encode('utf-8')
        + b'Content-Type: application/octet-stream\r\n\r\n'
        + fileContent
        + f'\r\n--{boundary}--\r\n'.encode('utf-8')
    )
    statusCode, rawBody = _doRequest(
        'POST', '/file', body,
        f'multipart/form-data; boundary={boundary}')
    try:
        response = json.loads(rawBody)
    except json.JSONDecodeError:
        raise SandboxError('文件上传响应不是合法 JSON')
    # 实测形态（v1.13.3）：响应是纯 JSON 字符串 fileId（如 "ZSGDNRTD"）；
    # dict 形态保留兜底：{"fileId"} / {"ids": {"file"}}
    if isinstance(response, str):
        fileId = response
    else:
        fileId = response.get('fileId') or response.get('ids', {}).get('file')
    if not fileId:
        raise SandboxError(f'文件上传响应缺少 fileId：{response!r}')
    return fileId


def deleteFile(fileId):
    """删除沙箱文件：DELETE /file/<fileId>（释放沙箱磁盘）

    404（文件不存在/已过期）视为已清理——清理语义幂等，
    租约回收不会因 fileId 过期而失败。
    """
    try:
        _doRequest('DELETE', f'/file/{fileId}')
    except BadRequestError as e:
        if e.statusCode == 404:
            logger.debug('清理 fileId %s：文件已不存在，视为已清理', fileId)
            return
        raise


# ==================== Agent 多语言执行（OJ 内部模块调用）====================
# 题库语言白名单（Problem.languages）保持 c/python 不变；这里是 Agent 自调试
# 工具：Agent 生成修复代码后需在沙箱运行验证，直到自己代码全部测试点 AC。
# Java 留接口位（沙箱镜像暂未安装 JDK，2026-10-19 确认）。

AGENT_SUPPORTED_LANGUAGES = frozenset(['python', 'c', 'cpp'])
AGENT_LANGUAGE_ALIASES = {
    'python': 'python', 'python3': 'python', 'py': 'python',
    'c': 'c',
    'cpp': 'cpp', 'c++': 'cpp',
    'java': 'java',          # 别名保留：调用时按「暂不支持」明确报错
}
# Agent 单次执行限额：调用方可收紧，服务端强制夹紧上限
AGENT_DEFAULT_TIME_MS = 2000
AGENT_DEFAULT_MEMORY_MB = 128
AGENT_MAX_TIME_MS = 5000
AGENT_MAX_CLOCK_MS = 5000            # clockLimit 上限（毫秒）
AGENT_MAX_MEMORY_MB = 512
AGENT_OUTPUT_MAX = 1_048_576         # stdout/stderr 各 1MB
AGENT_COMPILE_CPU_LIMIT = 10_000_000_000
AGENT_COMPILE_CLOCK_LIMIT = 20_000_000_000
AGENT_COMPILE_MEMORY_LIMIT = 536_870_912


def runCode(code, language='python', stdin='',
            timeLimitMs=AGENT_DEFAULT_TIME_MS,
            memoryLimitMb=AGENT_DEFAULT_MEMORY_MB, clockLimitMs=None):
    """Agent 沙箱单次执行：运行一段代码并返回清洗后的结果

    与判题链路（submitRun + 测试点比对）的区别：
    - 不比对预期输出、不做部分分，每次调用独立（不缓存编译产物）
    - 编译型语言在同一次 /run 请求内「编译 → 运行」两阶段执行
    返回：{status, stdout, stderr, exitStatus, timeMs, memoryKb}
    编译失败时 status='Compile Error'（业务层状态，不属于沙箱 9 种字面量）。
    """
    normalized = AGENT_LANGUAGE_ALIASES.get(str(language).lower())
    if normalized not in AGENT_SUPPORTED_LANGUAGES:
        raise BadRequestError(
            f'Agent 执行暂不支持语言 {language!r}', 400)
    cpuMs = _clampMs(timeLimitMs, AGENT_MAX_TIME_MS)
    clockMs = cpuMs * 3 if clockLimitMs is None else clockLimitMs
    cpuLimit = cpuMs * 1_000_000
    clockLimit = _clampMs(clockMs, AGENT_MAX_CLOCK_MS) * 1_000_000
    memoryLimit = _clampMb(memoryLimitMb) * 1024 * 1024
    if normalized == 'python':
        results = submitRun(_buildPythonRunRequest(
            code, stdin, cpuLimit, clockLimit, memoryLimit))
        return _cleanRunResult(results[0] if results else {})
    results = submitRun(_buildCompiledRunRequest(
        code, normalized, stdin, cpuLimit, clockLimit, memoryLimit))
    compileResult = results[0] if results else {}
    if compileResult.get('status') != 'Accepted':
        # 编译失败不是运行时错误：统一成业务层 Compile Error 交给 Agent 分析
        return _cleanRunResult(compileResult, status='Compile Error')
    runResult = results[1] if len(results) > 1 else compileResult
    return _cleanRunResult(runResult)


def _clampMs(value, maxMs):
    """毫秒限额夹紧：非法值回退默认值，低于 100ms 按 100ms（防 0/负数）"""
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        milliseconds = AGENT_DEFAULT_TIME_MS
    return max(100, min(milliseconds, maxMs))


def _clampMb(value):
    """内存限额夹紧（MB）：非法值回退默认值，范围 16MB ~ 上限"""
    try:
        megabytes = int(value)
    except (TypeError, ValueError):
        megabytes = AGENT_DEFAULT_MEMORY_MB
    return max(16, min(megabytes, AGENT_MAX_MEMORY_MB))


def _buildPythonRunRequest(code, stdin, cpuLimit, clockLimit, memoryLimit):
    """Python 单阶段请求：源码内联 copyIn，stdin 作为 files[0] 注入"""
    return [{
        'args': ['python3', 'main.py'],
        'env': ['PATH=/usr/bin:/bin', 'PYTHONIOENCODING=utf-8'],
        'files': [{'content': stdin},
                  {'name': 'stdout', 'max': AGENT_OUTPUT_MAX},
                  {'name': 'stderr', 'max': AGENT_OUTPUT_MAX}],
        'copyIn': {'main.py': {'content': code}},
        'cpuLimit': cpuLimit,
        'clockLimit': clockLimit,
        'memoryLimit': memoryLimit,
        'procLimit': 32,
        'copyOutMax': AGENT_OUTPUT_MAX,
    }]


def _buildCompiledRunRequest(code, language, stdin,
                             cpuLimit, clockLimit, memoryLimit):
    """C/C++ 两阶段请求：编译（独立编译档）→ 运行（prepared 引用产物）

    go-judge 的 {"prepared": "main"} 表示引用同一请求前置命令的
    copyOut 产物，无需回传 fileId，适合 Agent 一次性执行场景。
    """
    if language == 'cpp':
        sourceName = 'main.cpp'
        compileArgs = ['g++', 'main.cpp', '-o', 'main', '-O2', '-std=c++17']
    else:
        sourceName = 'main.c'
        compileArgs = ['gcc', 'main.c', '-o', 'main', '-O2', '-std=c11', '-lm']
    return [
        {   # 阶段 1：编译（编译档独立，clock 20s 给足）
            'args': compileArgs,
            'env': ['PATH=/usr/bin:/bin'],
            'files': [{'content': ''},
                      {'name': 'stdout', 'max': AGENT_OUTPUT_MAX},
                      {'name': 'stderr', 'max': AGENT_OUTPUT_MAX}],
            'copyIn': {sourceName: {'content': code}},
            'copyOut': ['main'],
            'cpuLimit': AGENT_COMPILE_CPU_LIMIT,
            'clockLimit': AGENT_COMPILE_CLOCK_LIMIT,
            'memoryLimit': AGENT_COMPILE_MEMORY_LIMIT,
            'procLimit': 32,
            'copyOutMax': AGENT_OUTPUT_MAX,
        },
        {   # 阶段 2：运行（引用上一步 copyOut 的 main 可执行文件）
            'args': ['./main'],
            'env': ['PATH=/usr/bin:/bin'],
            'files': [{'content': stdin},
                      {'name': 'stdout', 'max': AGENT_OUTPUT_MAX},
                      {'name': 'stderr', 'max': AGENT_OUTPUT_MAX}],
            'copyIn': {'main': {'prepared': 'main'}},
            'cpuLimit': cpuLimit,
            'clockLimit': clockLimit,
            'memoryLimit': memoryLimit,
            'procLimit': 32,
            'copyOutMax': AGENT_OUTPUT_MAX,
        },
    ]


def _cleanRunResult(result, status=None):
    """把 go-judge 原生 Result 清洗成 Agent 易用的扁平结构

    注意：实测 v1.13.3 的 time 字段单位是秒（tasks_judge.py 已按秒处理），
    memory 单位是 byte；此处统一换算成毫秒 / KB。
    """
    files = result.get('files') or {}
    return {
        'status': status or result.get('status', 'Internal Error'),
        'stdout': files.get('stdout', ''),
        'stderr': files.get('stderr', '') or result.get('error', ''),
        'exitStatus': result.get('exitStatus', -1),
        'timeMs': round(result.get('time', 0) * 1000, 2),
        'memoryKb': round(result.get('memory', 0) / 1024, 2),
    }
