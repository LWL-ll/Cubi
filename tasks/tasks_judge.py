"""判题任务函数：long 集群消费（docs/OJ对接设计.md 第八章租约状态机）

M4 阶段说明：本文件为最小占位实现，保证受理提交终态可达——
领取租约 → running → system_error「判题主控未部署」→ done。
M5 落地完整判题主控后替换：租约状态机 + copyOutCached 编译 +
逐测试点判题 + 部分分聚合 + 指纹检测 + spj。
"""
import logging

from django.utils import timezone

from teaching.models import JudgeTask, Submission

logger = logging.getLogger('codemind.judge_task')


def runJudgeTask(submissionId):
    """判题任务入口（Q2 管「至少一次投递」，租约状态机管「至多一次生效」）

    终态幂等：重复投递直接返回，不重复执行（测试矩阵「幂等」项）。
    """
    judgeTask = JudgeTask.objects.select_related('submission').filter(
        submission_id=submissionId).first()
    if judgeTask is None:
        logger.error('判题任务不存在（提交 %s），丢弃', submissionId)
        return
    if judgeTask.status in (JudgeTask.STATUS_DONE, JudgeTask.STATUS_FAILED):
        return
    if not claimLease(judgeTask):
        # 已被其他 worker 领取且租约有效：本副本不生效，直接退出
        return
    try:
        # M5 替换点：这里将执行完整判题主控（编译 → 逐点 → 聚合 → 回写）
        markSystemError(judgeTask, '判题主控尚未部署（M5 上线后自动恢复）')
    finally:
        JudgeTask.objects.filter(pk=judgeTask.pk).update(
            status=JudgeTask.STATUS_DONE, finished_at=timezone.now())


def claimLease(judgeTask):
    """原子领取租约：仅当 pending 时成功（attempt_id+1、心跳起算）

    M4 占位简化：只领 pending；leased 心跳过期回收与 retryable
    重投在 M5 实现（原子条件 UPDATE 防并发重复执行，第八章）。
    """
    updated = JudgeTask.objects.filter(
        pk=judgeTask.pk, status=JudgeTask.STATUS_PENDING).update(
        status=JudgeTask.STATUS_LEASED,
        attempt_id=judgeTask.attempt_id + 1,
        claimed_at=timezone.now(),
        heartbeat_at=timezone.now())
    if not updated:
        return False
    Submission.objects.filter(pk=judgeTask.submission_id).update(
        status=Submission.STATUS_RUNNING)
    return True


def markSystemError(judgeTask, message):
    """提交置系统错误终态（占位：未部署期间所有提交可达终态）"""
    Submission.objects.filter(pk=judgeTask.submission_id).update(
        status=Submission.STATUS_SYSTEM_ERROR,
        error_detail=message,
        judged_at=timezone.now())
