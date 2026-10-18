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
