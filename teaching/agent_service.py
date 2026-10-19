"""Agent 讲题服务：OJ 内部模块，直接调用函数（非独立 HTTP 服务）

接入契约（Agent 本体开发中，2026-10-19 仅留接口）：
1. 判题错误终态落库后，tasks_judge 通过 transaction.on_commit 调用
   dispatchTutorAgent，投递到 Q2 agent 集群；
2. runTutorAgent 读取 Submission（code / judge_snapshot / error_detail），
   调用 judge.runCode 复现学生代码；
3. Agent 自写修复代码 → runCode 验证 → 迭代，直到自己的代码所有测试点 AC；
4. 仅当全部 AC 后写入 diagnosis/explanation（不含正确代码）；
   corrected_code 只写库留痕，永不下发；
5. Agent 失败只影响报告状态，绝不改动 Submission 判题结果。

本期开关：settings.AGENT_ENABLED 默认 False——不创建报告、不投递任务。
"""
import logging

from django.conf import settings
from django.utils import timezone
from django_q.tasks import async_task

from teaching.models import AgentTutorReport, Submission

logger = logging.getLogger('codemind.agent')

# Q2 任务函数路径：tasks/tasks_agent.py 转发到本模块的 runTutorAgent
AGENT_TASK_FUNC = 'tasks.tasks_agent.runTutorAgent'

# 触发 Agent 的学生代码错误状态（2026-10-19 确认：所有错误都自动触发）
# repeat：判题未执行（重复提交拦截）；system_error：平台/checker 故障，
# 均不属于学生代码错误，不触发（避免 Agent 无意义调用）
AGENT_TRIGGER_STATUSES = frozenset([
    Submission.STATUS_WRONG_ANSWER,
    Submission.STATUS_COMPILE_ERROR,
    Submission.STATUS_TLE,
    Submission.STATUS_MLE,
    Submission.STATUS_OLE,
    Submission.STATUS_RUNTIME_ERROR,
])


def dispatchTutorAgent(submissionId):
    """投递 Agent 讲题任务（判题终态事务 on_commit 后调用）

    异常兜底：on_commit 回调抛异常会干扰判题任务的收尾，这里整体捕获，
    投递失败只记日志（报告停在 pending，不影响判题结果）。
    """
    if not getattr(settings, 'AGENT_ENABLED', False):
        return
    try:
        _dispatchTutorAgent(submissionId)
    except Exception:
        logger.exception('投递 Agent 讲题任务失败（提交 %s）', submissionId)


def _dispatchTutorAgent(submissionId):
    """投递实现：状态过滤 + 一对一报告幂等 + 投递 agent 集群"""
    submission = Submission.objects.filter(pk=submissionId).first()
    if submission is None or submission.status not in AGENT_TRIGGER_STATUSES:
        return
    _, created = AgentTutorReport.objects.get_or_create(
        submission=submission)
    if not created:
        return
    async_task(AGENT_TASK_FUNC, submissionId,
               cluster=settings.AGENT_CLUSTER)


def runTutorAgent(submissionId):
    """Agent 讲题任务入口（Q2 agent 集群消费，Agent 本体接入点）

    状态机：pending → running → done / failed；
    已 done/running 直接返回（Q2 重复投递幂等）。
    """
    report = AgentTutorReport.objects.filter(
        submission_id=submissionId).first()
    if report is None or report.status in (
            AgentTutorReport.STATUS_DONE, AgentTutorReport.STATUS_RUNNING):
        return
    AgentTutorReport.objects.filter(pk=report.pk).update(
        status=AgentTutorReport.STATUS_RUNNING)
    try:
        _runAgentTutor(report)
    except NotImplementedError:
        logger.info('Agent 本体尚未接入，跳过讲题（提交 %s）', submissionId)
        _markReportFailed(report.pk, 'Agent 尚未接入')
    except Exception:
        logger.exception('Agent 讲题失败（提交 %s）', submissionId)
        _markReportFailed(report.pk, 'Agent 讲题暂时不可用')


def _markReportFailed(reportId, message):
    """报告失败兜底：只改报告状态，不动 Submission 判题结果"""
    AgentTutorReport.objects.filter(pk=reportId).update(
        status=AgentTutorReport.STATUS_FAILED, error=message,
        finished_at=timezone.now())


def _runAgentTutor(report):
    """Agent 本体实现位（待接入，当前抛 NotImplementedError）

    实现时遵循以下契约：
    - 输入：report.submission（code / judge_snapshot / error_detail）
    - 工具：teaching.judge.runCode(code, language, stdin, ...) 多语言执行
    - 自测：Agent 生成的代码必须自己所有测试点 AC 才可生成讲解
    - 输出：diagnosis + explanation（学生可见，不含隐藏测试点/预期输出）
      corrected_code 只写库，序列化器绝不下发
    """
    raise NotImplementedError('Agent 本体尚未接入')


def getTutorReport(submission):
    """读取提交对应的讲题报告（无报告返回 None，供序列化器使用）"""
    return getattr(submission, 'agent_report', None)
