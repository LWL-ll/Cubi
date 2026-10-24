"""提交受理服务层：同步校验 + 快照冻结 + 原子建表 + 投递

docs/OJ对接设计.md 第七章：
- 九项同步校验（毫秒级 400/404/429 快速失败）
- 受理流程：校验与建表同事务（防并发绕过频率闸），
  事务提交后 on_commit 投递判题任务（long 集群）
- 资格语义：受理时冻结完整判题快照，判题零实时查题（7.3）

校验拒绝统一抛 DRF 异常（ValidationError→400 / NotFound→404 /
Throttled→429），视图层无需转换。
"""
import hashlib
import json
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django_q.tasks import async_task
from rest_framework.exceptions import NotFound, Throttled, ValidationError

from teaching.helpers import getStudentTeachingClass
from teaching.models import (Homework, HomeworkProblem, JudgeTask, Problem,
                             Submission)

logger = logging.getLogger('codemind.submission')

FREQUENCY_WINDOW_SECONDS = 10   # 频率检测窗口（秒）：10 秒内 3 次触发 429
FREQUENCY_MAX_SUBMITS = 3       # 窗口内最大提交次数（已有 3 条即拒绝新提交）
QUEUE_MAX_ACTIVE = 3            # 队列闸：同用户未终态判题任务上限
CODE_MAX_BYTES = 64 * 1024      # 代码大小上限 64KB（防大报文进队列）

# 判题任务函数路径（long 集群消费；runJudgeTask 实现见 tasks/tasks_judge.py）
JUDGE_TASK_FUNC = 'tasks.tasks_judge.runJudgeTask'


def normalizeNewlines(code):
    """统一换行：\\r\\n 与 \\r 均折叠为 \\n（指纹计算的输入规范化）"""
    return code.replace('\r\n', '\n').replace('\r', '\n')


def calcFingerprint(code):
    """统一换行后原文 SHA256：重复检测指纹

    v2 修订：不去空白——缩进/字符串内空白具有语义，
    去空白指纹会误伤 Python 缩进；语义指纹推迟到二期。
    """
    normalized = normalizeNewlines(code)
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def calcTestsHash(tests):
    """测试数据哈希：排序序列化后的 SHA256（不可复现时审计定位用）"""
    serialized = json.dumps(tests, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def calcTotalScore(tests):
    """题目总分 = Σ 测试点分值（points 由录题解析器保证存在）"""
    return sum(int(t.get('points', 0)) for t in tests)


def buildJudgeSnapshot(problem):
    """构建判题数据快照：受理时冻结，判题任务零实时查题（7.3）"""
    return {
        'tests': problem.tests,
        'spj_code': problem.spj_code,
        'tier': problem.tier,
        'compare_strategy': problem.compare_strategy,
    }


def submitSolution(requestUser, payload):
    """受理提交主入口：校验 → 快照 → 原子建表 → 投递

    payload 键（白名单提取，多余字段忽略）：
    problemId / submitType / language / code / homeworkId（作业提交必填）
    返回新 Submission 的 id；校验不过抛 DRF 异常。
    """
    problemId = payload.get('problemId')
    submitType = payload.get('submitType', Submission.SUBMIT_HOMEWORK)
    language = payload.get('language')
    code = payload.get('code')
    homeworkId = payload.get('homeworkId')

    problem = Problem.objects.filter(pk=problemId).first()
    if problem is None:
        raise NotFound('题目不存在')

    validateSubmitType(submitType, homeworkId)
    homework, frozenProblem = validateHomeworkSubmission(requestUser, homeworkId, problem)
    validateLanguage(frozenProblem, language)
    validateCode(code)
    # 脏数据防护：题目未配置测试点时不接受提交（否则 0 分判题无意义）
    if not frozenProblem.tests:
        raise ValidationError('题目尚未配置测试点，请联系教师')

    # 频率 count 与建表必须同事务：并发提交下 count 与插入串行化
    # （SQLite 单写者天然串行；切 PG 后需按用户行 select_for_update 锁）
    with transaction.atomic():
        checkFrequency(requestUser)
        checkQueueLimit(requestUser)
        submission = createSubmissionRecord(
            requestUser, problem, homework, language, code, submitType, frozenProblem)
        dispatchJudgeTask(submission.id)
    return submission.id


def validateSubmitType(submitType, homeworkId):
    """提交类型白名单 + 与作业参数的一致性（7.1 第 6、7 项）"""
    if submitType != Submission.SUBMIT_HOMEWORK:
        raise ValidationError('仅允许提交教师布置的作业')
    if homeworkId is None:
        raise ValidationError('作业提交必须指定作业')


def validateLanguage(problem, language):
    """语言白名单枚举 + 题目允许语言集（7.1 第 4 项）"""
    if language not in {Problem.LANGUAGE_C, Problem.LANGUAGE_PYTHON}:
        raise ValidationError('不支持的语言')
    if language not in (problem.languages or []):
        raise ValidationError('该题目不允许此语言')


def validateCode(code):
    """代码非空且 ≤ 64KB（7.1 第 5 项，防大报文进队列）"""
    if not code or not code.strip():
        raise ValidationError('代码不能为空')
    if len(code.encode('utf-8')) > CODE_MAX_BYTES:
        raise ValidationError(f'代码超过 {CODE_MAX_BYTES // 1024}KB 上限')


def validateHomeworkSubmission(requestUser, homeworkId, problem):
    """作业提交组合校验（7.1 第 7 项）

    - 作业不存在 → 404；学生不在教学班 → 404（不暴露存在性）
    - 题目不属于作业 → 404（不暴露作业-题组合关系）
    - 作业未开始/已截止 → 400（时间状态学生本身可见）
    """
    homework = Homework.objects.filter(pk=homeworkId).first()
    if homework is None:
        raise NotFound('作业不存在')
    # 对象级校验：学生经「行政班 → 教学班」三跳推导，不在班 404
    getStudentTeachingClass(requestUser, homework.teaching_class_id)
    item = HomeworkProblem.objects.filter(homework=homework, problem=problem).first()
    if item is None:
        raise NotFound('题目不属于该作业')
    now = timezone.now()
    if now < homework.start_at:
        raise ValidationError('作业尚未开始')
    if now > homework.deadline:
        raise ValidationError('作业已截止')
    return homework, item.getFrozenProblem()


def checkFrequency(user):
    """频率闸：同用户近 10 秒 ≥3 次提交 → 429（7.1 第 8 项）

    必须在 transaction.atomic 内调用（与建表同事务防并发绕过）。
    """
    cutoff = timezone.now() - timedelta(seconds=FREQUENCY_WINDOW_SECONDS)
    recentCount = Submission.objects.filter(
        user=user, created_at__gte=cutoff).count()
    if recentCount >= FREQUENCY_MAX_SUBMITS:
        raise Throttled(detail='提交过于频繁，请稍后再试')


def checkQueueLimit(user):
    """队列闸：同用户未终态 JudgeTask ≥ 3 个 → 400（7.1 第 9 项，防积压滥用）"""
    activeCount = JudgeTask.objects.filter(
        submission__user=user).exclude(
        status__in=[JudgeTask.STATUS_DONE, JudgeTask.STATUS_FAILED]).count()
    if activeCount >= QUEUE_MAX_ACTIVE:
        raise ValidationError('排队中的提交过多，请等待判题完成后再试')


def createSubmissionRecord(user, problem, homework, language, code, submitType,
                           frozenProblem):
    """事务内建表：Submission(status=queued, 完整快照) + JudgeTask(pending)"""
    submission = Submission.objects.create(
        user=user,
        problem=problem,
        homework=homework,
        language=language,
        code=code,
        fingerprint=calcFingerprint(code),
        submit_type=submitType,
        status=Submission.STATUS_QUEUED,
        total_score=calcTotalScore(frozenProblem.tests),
        problem_revision=frozenProblem.revision,
        tests_hash=calcTestsHash(frozenProblem.tests),
        judge_snapshot=buildJudgeSnapshot(frozenProblem),
    )
    JudgeTask.objects.create(submission=submission)
    return submission


def dispatchJudgeTask(submissionId):
    """注册事务提交后的投递回调：async_task 投递 long 集群并回写 q2_task_id

    任务函数签名 runJudgeTask(submissionId)（docs/OJ对接设计.md 7.2）。
    投递失败不影响受理响应（记日志、q2_task_id 留空），
    补投扫描定时任务兜底（docs/OJ对接设计.md 7.2）。
    """

    def doDispatch():
        try:
            taskId = async_task(JUDGE_TASK_FUNC, submissionId, cluster='long')
            JudgeTask.objects.filter(submission_id=submissionId).update(
                q2_task_id=taskId)
        except Exception:
            logger.exception('提交 %s 判题任务投递失败，等待补投扫描兜底', submissionId)

    transaction.on_commit(doDispatch)
