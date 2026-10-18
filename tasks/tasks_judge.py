"""判题任务主控：long 集群消费（docs/OJ对接设计.md 第八章租约状态机）

执行顺序（8.2）：租约领取 → 资格检查 → 重复检测 → 编译 →
全测试点判题（聚合部分分）→ 结果落库。

设计要点：
- Q2 管「至少一次投递」，租约状态机管「至多一次生效」——所有写操作
  带 attempt_id 条件，过期 attempt 的执行结果直接丢弃
- 判题只用受理时冻结的快照（judge_snapshot），零实时查题
- 测试数据保密链（14.7）：日志/错误详情不含测试输入与预期输出，
  WA 只给「第 k 点」+ 实际输出截断预览；spj 输出不回显
"""
import logging
import re
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.http import Http404
from django.utils import timezone
from django_q.tasks import async_task

from teaching import judge
from teaching.helpers import getStudentTeachingClass
from teaching.models import JudgeTask, Problem, Submission

logger = logging.getLogger('codemind.judge_task')

# ---------- 判题参数（docs/OJ对接设计.md 第十三章资源档）----------

# 资源档：cpu/clock 单位纳秒，memory/copyOutMax 单位字节
# 每次请求显式携带全部 limit 字段，不依赖沙箱默认值（13 章固定项）
TIER_LIMITS = {
    Problem.TIER_BEGINNER: {'cpuLimit': 2_000_000_000, 'clockLimit': 5_000_000_000,
                            'memoryLimit': 268_435_456, 'copyOutMax': 16_777_216},
    Problem.TIER_BASIC: {'cpuLimit': 2_000_000_000, 'clockLimit': 5_000_000_000,
                         'memoryLimit': 268_435_456, 'copyOutMax': 16_777_216},
    Problem.TIER_ADVANCED: {'cpuLimit': 5_000_000_000, 'clockLimit': 10_000_000_000,
                            'memoryLimit': 536_870_912, 'copyOutMax': 33_554_432},
}
PROC_LIMIT = 32                     # 固定进程数上限（13 章固定项）
STDOUT_MAX = 8_388_608              # stdout/stderr 各 8MB（13 章固定项）
# 编译档（独立）：clock 10s、copyOutMax 32MB（编译比运行慢）
COMPILE_CLOCK_LIMIT = 10_000_000_000
COMPILE_COPY_OUT_MAX = 33_554_432
# 编译命令冻结（8.3：copyOutCached ["main"] 与 args ["./main"] 对应）
COMPILE_CMD = ['gcc', 'main.c', '-o', 'main', '-O2', '-std=c11', '-lm']
# spj 独立档（第十一章）：比用户程序档更紧，stdout/stderr 各 1MB
SPJ_OUTPUT_MAX = 1_048_576
SPJ_LIMITS = {'cpuLimit': 2_000_000_000, 'clockLimit': 5_000_000_000,
              'memoryLimit': 268_435_456, 'copyOutMax': SPJ_OUTPUT_MAX}

MAX_JUDGE_ATTEMPTS = 2              # 单提交最多执行 2 次（与 long 集群 max_attempts 对齐）
HEARTBEAT_TIMEOUT_SECONDS = 60      # 租约心跳超时（8.1 租约回收）
PENDING_REDISPATCH_SECONDS = 120    # pending 补投扫描阈值（受理投递失败兜底）
REPEAT_SCAN_COUNT = 20              # 重复检测窗口：同用户同题最近 20 条（8.2）
MAX_PREVIEW_CHARS = 500             # WA 输出预览截断（14.7）
JUDGE_TASK_FUNC = 'tasks.tasks_judge.runJudgeTask'
# 沙箱瞬时错误字面量：任务层转异常交给 q2 重投（8.2 沙箱级错误路径）
SANDBOX_TRANSIENT_STATUSES = frozenset(['File Error', 'Internal Error'])
# 聚合严重度序（8.5）：主状态取最差单点
SEVERITY_ORDER = [
    Submission.STATUS_RUNTIME_ERROR, Submission.STATUS_TLE,
    Submission.STATUS_MLE, Submission.STATUS_OLE,
    Submission.STATUS_WRONG_ANSWER,
]
# 非 WA 详情的固定白名单文案（14.7：学生可见详情只用固定文案）
STATUS_LABELS = {
    Submission.STATUS_TLE: '超出时间限制',
    Submission.STATUS_MLE: '超出内存限制',
    Submission.STATUS_OLE: '输出超出限制',
    Submission.STATUS_RUNTIME_ERROR: '运行时错误',
}


class JudgeTransientError(Exception):
    """沙箱/网络瞬时错误：回退租约后抛给 q2 按 retry 重投（8.2）"""


class JudgeLeaseLost(Exception):
    """判题过程中租约被回收：本副本结果作废，静默退出（8.1）"""


class SpjFailedError(Exception):
    """checker 连续故障：提交置 system_error（第十一章，checker 失败 ≠ 用户 WA）"""


def getTierLimits(tier):
    """资源档查询：未知档位按入门档兜底（保守从严，13 章）"""
    return TIER_LIMITS.get(tier, TIER_LIMITS[Problem.TIER_BEGINNER])


def getExecutionCount(attemptId):
    """执行次数：租约令牌每经过一次「回收 → 重投 → 领取」+2
    （8.1 回收 +1 作废令牌、领取 +1 建立新租约）"""
    return (attemptId + 1) // 2


def callJudge(func, *args):
    """调用判题客户端：沙箱/网络故障转瞬时异常（q2 重投）；
    认证/请求构造错误为确定性失败（调用方决定终态）"""
    try:
        return func(*args)
    except (judge.SandboxError, judge.NetworkError) as e:
        raise JudgeTransientError(str(e)) from e


def claimLease(judgeTask):
    """原子领取租约（8.1）：pending/retryable → leased + attempt_id+1
    影响行数=1 才算领到（并发 worker 只有一个成功）；返回新 attempt_id"""
    updated = JudgeTask.objects.filter(
        pk=judgeTask.pk, status__in=(
            JudgeTask.STATUS_PENDING, JudgeTask.STATUS_RETRYABLE)).update(
        status=JudgeTask.STATUS_LEASED,
        attempt_id=F('attempt_id') + 1,
        claimed_at=timezone.now(), heartbeat_at=timezone.now())
    if not updated:
        return None
    judgeTask.refresh_from_db()
    JudgeTask.objects.filter(pk=judgeTask.pk, started_at__isnull=True) \
        .update(started_at=timezone.now())
    Submission.objects.filter(pk=judgeTask.submission_id).update(
        status=Submission.STATUS_RUNNING)
    return judgeTask.attempt_id


def recycleStaleLeases():
    """租约回收 + 补投扫描（8.1，qcluster 主集群 schedule 每分钟执行）：
    - leased 心跳超 60s → retryable + attempt_id+1（作废旧令牌）
      + 清 file_id + 清理残留产物 + 重新投递
    - pending 超 120s 未投递 → 补投（受理 on_commit 投递失败兜底）"""
    now = timezone.now()
    stale = list(JudgeTask.objects.select_related('submission').filter(
        status=JudgeTask.STATUS_LEASED,
        heartbeat_at__lt=now - timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS)))
    for task in stale:
        fileId = task.file_id
        updated = JudgeTask.objects.filter(
            pk=task.pk, attempt_id=task.attempt_id,
            status=JudgeTask.STATUS_LEASED).update(
            status=JudgeTask.STATUS_RETRYABLE,
            attempt_id=task.attempt_id + 1, file_id='',
            claimed_at=None, heartbeat_at=None)
        if not updated:
            continue  # 心跳过期与正常完成竞争：已完成则不动
        if fileId:
            try:
                judge.deleteFile(fileId)
            except judge.JudgeClientError as e:
                logger.warning('回收清理编译产物 %s 失败：%s', fileId, e)
        redispatch(task)
    # 补投扫描：受理时投递失败的任务（q2_task_id 留空、长时间停在 pending）
    pending = list(JudgeTask.objects.select_related('submission').filter(
        status=JudgeTask.STATUS_PENDING,
        submission__created_at__lt=now - timedelta(seconds=PENDING_REDISPATCH_SECONDS)))
    for task in pending:
        redispatch(task)


def redispatch(judgeTask):
    """重新投递到 long 集群：投递失败记日志（下轮扫描继续兜底）"""
    try:
        async_task(JUDGE_TASK_FUNC, judgeTask.submission_id, cluster='long')
    except Exception:
        logger.exception('补投提交 %s 失败', judgeTask.submission_id)


def checkEligibility(submission):
    """资格检查（8.2 阶段 1）：仅查身份类资格，判题只用快照不重查题目
    返回 None 表示通过，否则为 system_error 文案"""
    if submission.problem_id is None:
        return '题目已被删除'
    if not submission.user.is_active:
        return '账号已被封禁'
    if submission.homework_id is not None:
        try:
            getStudentTeachingClass(submission.user,
                                    submission.homework.teaching_class_id)
        except Http404:
            return '已被移出教学班'
    return None


def detectRepeat(submission):
    """重复检测（8.2 阶段 2）：同用户同题最近 20 条提交中找相同 fingerprint
    返回被重复的提交对象，未命中返回 None"""
    recent = Submission.objects.filter(
        user_id=submission.user_id, problem_id=submission.problem_id,
        created_at__lt=submission.created_at).exclude(pk=submission.pk) \
        .order_by('-created_at')[:REPEAT_SCAN_COUNT]
    for previous in recent:
        if previous.fingerprint == submission.fingerprint:
            return previous
    return None


def buildCompileRequest(code, tierLimits):
    """编译请求（8.3）：copyOutCached ["main"] 产物复用，编译档独立限制"""
    return [{
        'args': COMPILE_CMD,
        'env': ['PATH=/usr/bin:/bin'],
        'files': [{'content': ''}, {'name': 'stdout', 'max': STDOUT_MAX},
                  {'name': 'stderr', 'max': STDOUT_MAX}],
        'copyIn': {'main.c': {'content': code}},
        'copyOutCached': ['main'],
        'cpuLimit': tierLimits['cpuLimit'],
        'clockLimit': COMPILE_CLOCK_LIMIT,
        'memoryLimit': tierLimits['memoryLimit'],
        'procLimit': PROC_LIMIT,
        'copyOutMax': COMPILE_COPY_OUT_MAX,
    }]


def buildRunRequest(submission, test, fileId, tierLimits):
    """单测试点请求（8.4）：stdin 注入、stdout/stderr 8MB、
    资源档显式携带；C 用编译产物 fileId，Python 内联代码原文"""
    if submission.language == 'c':
        args = ['./main']
        copyIn = {'main': {'fileId': fileId}}
    else:
        args = ['python3', 'main.py']
        copyIn = {'main.py': {'content': submission.code}}
    return [{
        'args': args,
        'env': ['PATH=/usr/bin:/bin'],
        'files': [{'content': test.get('input', '')},
                  {'name': 'stdout', 'max': STDOUT_MAX},
                  {'name': 'stderr', 'max': STDOUT_MAX}],
        'copyIn': copyIn,
        'cpuLimit': tierLimits['cpuLimit'],
        'clockLimit': tierLimits['clockLimit'],
        'memoryLimit': tierLimits['memoryLimit'],
        'procLimit': PROC_LIMIT,
        'copyOutMax': tierLimits['copyOutMax'],
    }]


def buildSpjRequest(spjCode, test, userOutput):
    """spj 请求（第十一章）：argv 三参数协议 + spj 源码内联 + 独立更小档
    Python 视角 sys.argv = [checker.py, input, user_out, expected]"""
    return [{
        'args': ['python3', 'checker.py', 'input.txt', 'user_out.txt',
                 'expected.txt'],
        'env': ['PATH=/usr/bin:/bin'],
        'files': [{'content': ''}, {'name': 'stdout', 'max': SPJ_OUTPUT_MAX},
                  {'name': 'stderr', 'max': SPJ_OUTPUT_MAX}],
        'copyIn': {'checker.py': {'content': spjCode},
                   'input.txt': {'content': test.get('input', '')},
                   'user_out.txt': {'content': userOutput},
                   'expected.txt': {'content': test.get('output', '')}},
        'cpuLimit': SPJ_LIMITS['cpuLimit'],
        'clockLimit': SPJ_LIMITS['clockLimit'],
        'memoryLimit': SPJ_LIMITS['memoryLimit'],
        'procLimit': PROC_LIMIT,
        'copyOutMax': SPJ_LIMITS['copyOutMax'],
    }]


def compileSubmission(judgeTask, attemptId, submission, tierLimits):
    """C 编译（8.3）：成功 → fileId 落 JudgeTask（回收审计用）；
    编译失败 → compile_error 终态返回 None；沙箱故障 → 瞬时异常 q2 重投"""
    request = buildCompileRequest(submission.code, tierLimits)
    result = callJudge(judge.submitRun, request)[0]
    if result['status'] != 'Accepted':
        if result['status'] in SANDBOX_TRANSIENT_STATUSES:
            raise JudgeTransientError(f'编译沙箱错误：{result["status"]}')
        stderr = result.get('files', {}).get('stderr', '')
        writeTerminal(judgeTask, attemptId, Submission.STATUS_COMPILE_ERROR, 0,
                      sanitizeOutput(stderr) or '编译失败')
        return None
    fileId = result.get('fileIds', {}).get('main')
    if not fileId:
        raise JudgeTransientError('编译成功但缺少 fileIds["main"]')
    JudgeTask.objects.filter(pk=judgeTask.pk, attempt_id=attemptId) \
        .update(file_id=fileId)
    return fileId


def normalizeTrailing(text):
    """忽略行尾空白策略（第五章精确定义）：逐行 rstrip（仅行尾 ASCII
    空格与制表符，不动行首），首尾空行两侧都去掉后比对"""
    lines = [line.rstrip(' \t') for line in text.splitlines()]
    while lines and lines[0] == '':
        lines.pop(0)
    while lines and lines[-1] == '':
        lines.pop()
    return '\n'.join(lines)


LOOSE_WHITESPACE_RE = re.compile(r'\s+')


def normalizeLoose(text):
    """忽略全部空白策略（第五章）：去掉 ASCII 与 Unicode 空白字符
    （\xa0 等空格类）、统一换行（\r\n → \n）后比对"""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    return LOOSE_WHITESPACE_RE.sub('', text)


def compareOutputs(actual, expected, strategy):
    """答案比对（第五章三策略）：exact 逐字节 / trailing 忽略行尾空白
    / loose 忽略全部空白；未知策略按精确比对兜底"""
    if strategy == 'trailing':
        return normalizeTrailing(actual) == normalizeTrailing(expected)
    if strategy == 'loose':
        return normalizeLoose(actual) == normalizeLoose(expected)
    return actual == expected


CONTROL_CHARS_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def sanitizeOutput(text, maxChars=MAX_PREVIEW_CHARS):
    """输出脱敏（14.7）：控制字符清洗（ANSI 转义等）+ 截断，学生可见预览"""
    cleaned = CONTROL_CHARS_RE.sub('', text)
    if len(cleaned) <= maxChars:
        return cleaned
    return cleaned[:maxChars] + '\n…（输出过长已截断）'


def buildWaDetail(testIndex, stdout):
    """WA 详情（14.7）：只给「第 k 点」+ 实际输出截断预览，不给预期输出"""
    return f'第 {testIndex} 个测试点未通过\n实际输出：\n{sanitizeOutput(stdout)}'


def buildStatusDetail(testIndex, status, result):
    """非 WA 详情（14.7 固定白名单文案）：RE 附截断清洗后的 stderr
    （学生自己程序的输出，无保密问题）"""
    label = STATUS_LABELS.get(status, status)
    detail = f'第 {testIndex} 个测试点：{label}'
    stderr = result.get('files', {}).get('stderr', '')
    if status == Submission.STATUS_RUNTIME_ERROR and stderr.strip():
        detail += '\n' + sanitizeOutput(stderr)
    return detail


# checker 正常执行状态（第十一章部分分协议）：
# - Accepted：exitStatus=0 → 该点满分
# - Nonzero Exit Status：exitStatus≠0 → stdout 首行整数 = 部分分（协议信号，
#   不是故障；go-judge 对 exit≠0 的程序必然报此状态）
# 其余状态（TLE/MLE/OLE/Signalled/Dangerous Syscall/File Error/Internal Error）
# 才是 checker 故障：重试 1 次仍失败 → SpjFailedError（≠ 用户 WA）。
# 实测教训：v6 曾写「非 Accepted 一律按故障」，把部分分协议的 exit≠0
# 误杀成 system_error（验收 4.2 抓到，见 OJ对接设计.md v9 修订记录）。
SPJ_NORMAL_STATUSES = frozenset(['Accepted', 'Nonzero Exit Status'])


def interpretSpjScore(result, points):
    """spj 分数协议（第十一章）：exitStatus=0 → 满分；
    exitStatus≠0 → stdout 首行 strip 后若是 [0,points) 整数 → 该分；否则 0"""
    if result.get('exitStatus') == 0:
        return points
    firstLine = result.get('files', {}).get('stdout', '').strip().splitlines()
    if not firstLine:
        return 0
    try:
        score = int(firstLine[0].strip())
    except ValueError:
        return 0
    if 0 <= score < points:
        return score
    return 0


def runSpj(submission, test, userOutput):
    """跑 spj（第十一章）：checker 沙箱故障重试 1 次，仍失败 → SpjFailedError
    （checker 失败 ≠ 用户 WA）；正常执行（Accepted/Nonzero Exit Status）
    返回分数协议解释后的得分"""
    spjCode = submission.judge_snapshot.get('spj_code', '')
    points = test.get('points', 0)
    for attempt in range(2):
        result = callJudge(judge.submitRun,
                           buildSpjRequest(spjCode, test, userOutput))[0]
        if result['status'] in SPJ_NORMAL_STATUSES:
            return interpretSpjScore(result, points)
        if attempt == 0:
            logger.warning('提交 %s checker 沙箱故障（%s），重试 1 次',
                           submission.pk, result['status'])
            continue
        raise SpjFailedError(f'checker 连续故障（{result["status"]}）')
    raise SpjFailedError('checker 执行异常')  # 防御性兜底，正常路径不经过


def judgeOneTest(submission, testIndex, test, fileId, tierLimits):
    """单测试点判题（8.4）：沙箱运行 → Accepted 进比对（或 spj）
    返回 {status, score, detail, time, memory}；沙箱瞬时错误抛异常重投"""
    request = buildRunRequest(submission, test, fileId, tierLimits)
    result = callJudge(judge.submitRun, request)[0]
    point = {
        'time': int(result.get('time', 0) * 1_000_000_000),  # 秒 → 纳秒
        'memory': result.get('memory', 0) or 0,
    }
    if result['status'] != 'Accepted':
        if result['status'] in SANDBOX_TRANSIENT_STATUSES:
            raise JudgeTransientError(f'沙箱错误：{result["status"]}')
        mapped = judge.mapStatus(result['status'])
        point.update(status=mapped, score=0,
                     detail=buildStatusDetail(testIndex, mapped, result))
        return point
    stdout = result.get('files', {}).get('stdout')
    if stdout is None:
        raise JudgeTransientError('Accepted 结果缺少 stdout')
    points = test.get('points', 0)
    if submission.judge_snapshot.get('spj_code', '').strip():
        score = runSpj(submission, test, stdout)
        passed = score >= points
        point.update(status=Submission.STATUS_ACCEPTED if passed
                     else Submission.STATUS_WRONG_ANSWER,
                     score=score,
                     detail='' if passed else f'第 {testIndex} 个测试点未通过')
        return point
    strategy = submission.judge_snapshot.get('compare_strategy', 'exact')
    if compareOutputs(stdout, test.get('output', ''), strategy):
        point.update(status=Submission.STATUS_ACCEPTED, score=points, detail='')
    else:
        point.update(status=Submission.STATUS_WRONG_ANSWER, score=0,
                     detail=buildWaDetail(testIndex, stdout))
    return point


def judgeAllTests(submission, judgeTask, attemptId, fileId, tierLimits):
    """全测试点判题（8.5）：不提前终止；心跳与 progress 每点同写（8.1），
    写失败说明租约被回收 → JudgeLeaseLost（本副本结果作废）"""
    tests = submission.judge_snapshot.get('tests', [])
    points = []
    total = len(tests)
    for index, test in enumerate(tests, start=1):
        points.append(judgeOneTest(submission, index, test, fileId, tierLimits))
        progress = index * 100 // total
        updated = JudgeTask.objects.filter(
            pk=judgeTask.pk, attempt_id=attemptId).update(
            progress=progress, heartbeat_at=timezone.now())
        if not updated:
            raise JudgeLeaseLost()
    return points


def aggregatePoints(points):
    """部分分聚合（8.5）：score=Σ通过点分值；主状态取最差单点
    （RE > TLE > MLE > OLE > WA）"""
    score = sum(point['score'] for point in points)
    statuses = {point['status'] for point in points}
    if statuses <= {Submission.STATUS_ACCEPTED}:
        return Submission.STATUS_ACCEPTED, score
    for severity in SEVERITY_ORDER:
        if severity in statuses:
            return severity, score
    return Submission.STATUS_WRONG_ANSWER, score  # 防御性兜底


def writeVerdict(judgeTask, attemptId, points):
    """聚合部分分并写终态（8.5）：错误详情取主状态对应点的详情"""
    status, score = aggregatePoints(points)
    worst = next((p for p in points if p['status'] == status), None)
    timeUsed = max((p['time'] for p in points), default=0) or None
    memoryUsed = max((p['memory'] for p in points), default=0) or None
    writeTerminal(judgeTask, attemptId, status, score,
                  worst['detail'] if worst else '',
                  timeUsed=timeUsed, memoryUsed=memoryUsed)


def writeTerminal(judgeTask, attemptId, status, score, errorDetail,
                  timeUsed=None, memoryUsed=None, repeatOf=None):
    """写终态（事务内，8.1）：JudgeTask 条件 UPDATE 带 attempt_id 作闸，
    闸通过才写 Submission——过期 attempt 的结果直接丢弃；返回是否写入"""
    with transaction.atomic():
        updated = JudgeTask.objects.filter(
            pk=judgeTask.pk, attempt_id=attemptId).update(
            status=JudgeTask.STATUS_DONE, progress=100,
            finished_at=timezone.now())
        if not updated:
            return False
        fields = {'status': status, 'score': score,
                  'error_detail': errorDetail, 'judged_at': timezone.now()}
        if timeUsed is not None:
            fields['time_used'] = timeUsed
        if memoryUsed is not None:
            fields['memory_used'] = memoryUsed
        if repeatOf is not None:
            fields['repeat_of'] = repeatOf
        Submission.objects.filter(pk=judgeTask.submission_id).update(**fields)
    return True


def markFailed(judgeTask, attemptId, message):
    """重试耗尽（8.1）：JudgeTask failed + Submission system_error，
    与 writeTerminal 同款 attempt_id 闸"""
    with transaction.atomic():
        updated = JudgeTask.objects.filter(
            pk=judgeTask.pk, attempt_id=attemptId).update(
            status=JudgeTask.STATUS_FAILED, progress=100,
            finished_at=timezone.now())
        if updated:
            Submission.objects.filter(pk=judgeTask.submission_id).update(
                status=Submission.STATUS_SYSTEM_ERROR, score=0,
                error_detail=message, judged_at=timezone.now())


def fallbackForRetry(judgeTask, attemptId):
    """沙箱瞬时错误回退：下次执行仍有余量 → retryable（q2 重投后领取）；
    已用尽 → failed + system_error（8.1 重试上限）；返回是否抛异常"""
    if getExecutionCount(attemptId) + 1 > MAX_JUDGE_ATTEMPTS:
        markFailed(judgeTask, attemptId, '判题多次失败，请联系管理员')
        return False
    current = JudgeTask.objects.filter(
        pk=judgeTask.pk, attempt_id=attemptId).first()
    fileId = current.file_id if current else ''
    updated = JudgeTask.objects.filter(
        pk=judgeTask.pk, attempt_id=attemptId).update(
        status=JudgeTask.STATUS_RETRYABLE, file_id='',
        claimed_at=None, heartbeat_at=None)
    if not updated:
        return False  # 租约已被回收：交给回收路径处理
    if fileId:
        try:
            judge.deleteFile(fileId)
        except judge.JudgeClientError as e:
            logger.warning('回退清理编译产物 %s 失败：%s', fileId, e)
    return True


def cleanupFile(judgeTask, attemptId):
    """判后清理编译产物（14.5）：条件清空带 attempt_id，租约被回收后
    本副本不误删新 attempt 的产物；DELETE 404 幂等（客户端已处理）"""
    current = JudgeTask.objects.filter(
        pk=judgeTask.pk, attempt_id=attemptId).first()
    fileId = current.file_id if current else ''
    if not fileId:
        return
    JudgeTask.objects.filter(pk=judgeTask.pk, attempt_id=attemptId,
                             file_id=fileId).update(file_id='')
    try:
        judge.deleteFile(fileId)
    except judge.JudgeClientError as e:
        logger.warning('清理编译产物 %s 失败：%s', fileId, e)


def executeJudge(submissionId, judgeTask, attemptId):
    """判题执行体（8.2 阶段 1~4）：资格检查 → 重复检测 → 编译 → 全点判题"""
    submission = judgeTask.submission
    # 阶段 1：资格检查（题目删除/用户封禁/移出教学班 → system_error，零 HTTP）
    error = checkEligibility(submission)
    if error:
        writeTerminal(judgeTask, attemptId, Submission.STATUS_SYSTEM_ERROR, 0,
                      error)
        return
    # 阶段 2：重复检测（fingerprint 比对最近 20 条，命中 → repeat，零 HTTP）
    repeatOf = detectRepeat(submission)
    if repeatOf is not None:
        writeTerminal(judgeTask, attemptId, Submission.STATUS_REPEAT, 0,
                      f'重复于提交 #{repeatOf.pk}', repeatOf=repeatOf)
        return
    # 阶段 3：编译（仅 C，copyOutCached 产物复用）
    tierLimits = getTierLimits(submission.judge_snapshot.get('tier'))
    fileId = None
    if submission.language == 'c':
        fileId = compileSubmission(judgeTask, attemptId, submission, tierLimits)
        if fileId is None:
            return  # 编译失败已写终态
    # 阶段 4：全测试点判题（每个点独立判定计分，跑完聚合）
    tests = submission.judge_snapshot.get('tests', [])
    if not tests:
        writeTerminal(judgeTask, attemptId, Submission.STATUS_SYSTEM_ERROR, 0,
                      '题目测试数据异常')
        return
    points = judgeAllTests(submission, judgeTask, attemptId, fileId, tierLimits)
    writeVerdict(judgeTask, attemptId, points)


def runJudgeTask(submissionId):
    """判题任务入口（8.2）：Q2 管「至少一次投递」，租约管「至多一次生效」

    终态幂等：done/failed 直接返回；领取失败（他人持有租约）直接返回；
    过期 attempt 的所有写操作带 attempt_id 条件，自然失败丢弃。
    """
    judgeTask = JudgeTask.objects.select_related(
        'submission__user', 'submission__homework').filter(
        submission_id=submissionId).first()
    if judgeTask is None:
        logger.error('判题任务不存在（提交 %s），丢弃', submissionId)
        return
    if judgeTask.status in (JudgeTask.STATUS_DONE, JudgeTask.STATUS_FAILED):
        return
    attemptId = claimLease(judgeTask)
    if attemptId is None:
        return
    if getExecutionCount(attemptId) > MAX_JUDGE_ATTEMPTS:
        markFailed(judgeTask, attemptId, '判题多次失败，请联系管理员')
        return
    try:
        executeJudge(submissionId, judgeTask, attemptId)
    except JudgeLeaseLost:
        logger.warning('提交 %s 租约在判题中被回收，本副本结果作废', submissionId)
    except JudgeTransientError:
        logger.warning('提交 %s 沙箱瞬时错误，回退租约等待 q2 重投', submissionId)
        if fallbackForRetry(judgeTask, attemptId):
            raise  # 抛给 q2 记录失败并按 retry 重投
    except SpjFailedError:
        writeTerminal(judgeTask, attemptId, Submission.STATUS_SYSTEM_ERROR, 0,
                      '判题程序（checker）异常，请联系教师')
    except judge.JudgeClientError as e:
        logger.error('提交 %s 判题客户端确定性错误：%s', submissionId, e)
        writeTerminal(judgeTask, attemptId, Submission.STATUS_SYSTEM_ERROR, 0,
                      '判题服务暂时不可用，请稍后重试')
    finally:
        cleanupFile(judgeTask, attemptId)
