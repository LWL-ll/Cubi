"""判题主控测试：租约状态机 + 五阶段流程 + 部分分聚合 + SPJ + 脱敏

规格：docs/OJ对接设计.md 第八章（租约/执行/聚合）、第五章（三策略比对）、
第十一章（SPJ 分数协议）、第十三章（资源档）、14.7（WA 脱敏）。
HTTP 层统一 mock（FakeJudge 按调用序列返回），断言口径：
- /run 调用次数 = 编译 1 次（仅 C）+ 测试点数（spj 题每点再加 1 次 checker）
- repeat/资格检查路径零 HTTP
- 终态/进度/清理写操作带 attempt_id 条件（过期 attempt 结果丢弃）
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.db.models.query import QuerySet
from django.utils import timezone

from academics.models import (AdminClass, ClassMembership, Course,
                              CourseCollege, Department, Major, TeachingClass)
from teaching.models import (Homework, JudgeTask, Problem, Submission)
from users.models import SchoolIdentity, StudentProfile, User

from . import tasks_judge
from .tasks_judge import (JudgeTransientError, aggregatePoints, claimLease,
                          compareOutputs, recycleStaleLeases, runJudgeTask,
                          sanitizeOutput)


def acceptedResult(stdout, exitStatus=0, timeUsed=10_000_000, memory=1_048_576,
                   status='Accepted'):
    """构造 go-judge 响应（与 cmd 一一对应的 Result 条目）

    status 默认 Accepted；部分分协议场景需传 status='Nonzero Exit Status'
    （go-judge 对 exit≠0 的程序必然报该状态，Accepted+exitStatus≠0 的组合
    现实中不会出现——验收 4.2 实测教训）。
    """
    return {'status': status, 'exitStatus': exitStatus,
            'files': {'stdout': stdout, 'stderr': ''},
            'time': timeUsed, 'memory': memory}


def compileOkResult():
    """构造编译成功的 go-judge 响应（copyOutCached 返回 fileIds）"""
    return {'status': 'Accepted', 'exitStatus': 0,
            'files': {'stdout': '', 'stderr': ''},
            'fileIds': {'main': 'fid-1'}}


def statusResult(status, stderr=''):
    """构造非 Accepted 的 go-judge 响应（TLE/MLE/RE 等）"""
    return {'status': status, 'exitStatus': 1,
            'files': {'stdout': '', 'stderr': stderr},
            'time': 5_000_000_000, 'memory': 1_048_576}


class JudgeTaskTests(TestCase):
    """判题主控全场景（任务层单测，HTTP 层全部 mock）"""

    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(username='T9001', password='Test12345')
        cls.student = User.objects.create_user(username='S9001', password='Test12345')
        # 学生身份链（checkEligibility 的移出教学班检查依赖 3 跳推导）
        department = Department.objects.create(code='D95', name='测试学院')
        major = Major.objects.create(department=department, code='95', name='测试专业')
        adminClass = AdminClass.objects.create(
            name='测试2301', major=major, enrollment_year=2023)
        identity = SchoolIdentity.objects.create(
            user=cls.student, school_number='20239501',
            identity_type=SchoolIdentity.IDENTITY_STUDENT)
        profile = StudentProfile.objects.create(
            identity=identity, major=major, enrollment_year=2023)
        ClassMembership.objects.create(
            student_profile=profile, admin_class=adminClass,
            joined_at=timezone.now().date())
        # 教学班不包含学生所在行政班（移出教学班测试用）
        course = Course.objects.create(code='TEST01', name='测试课程')
        courseCollege = CourseCollege.objects.create(
            course=course, department=department)
        cls.teachingClass = TeachingClass.objects.create(
            name='测试一班', term='2026-2027-1', course_college=courseCollege)
        cls.homework = Homework.objects.create(
            teaching_class=cls.teachingClass, title='第一次作业',
            start_at=timezone.now() - timedelta(hours=1),
            deadline=timezone.now() + timedelta(hours=1),
            created_by=cls.teacher)
        cls.problem = Problem.objects.create(
            title='A+B 问题', statement_md='计算两个整数的和。',
            samples=[{'input': '1 2', 'output': '3'}],
            tests=[{'input': '1 2', 'output': '3', 'points': 40},
                   {'input': '2 3', 'output': '5', 'points': 30},
                   {'input': '3 4', 'output': '7', 'points': 30}],
            languages=['c', 'python'], owner=cls.teacher)
        # 受理时冻结的判题快照（测试统一用它，保证零实时查题）
        cls.snapshot = {
            'tests': cls.problem.tests,
            'spj_code': '',
            'tier': 'beginner',
            'compare_strategy': 'exact',
        }

    def setUp(self):
        self.submissionSeq = 0
        self.patchRun = mock.patch('tasks.tasks_judge.judge.submitRun')
        self.mockRun = self.patchRun.start()
        self.addCleanup(self.patchRun.stop)
        self.patchDelete = mock.patch('tasks.tasks_judge.judge.deleteFile')
        self.mockDelete = self.patchDelete.start()
        self.addCleanup(self.patchDelete.stop)
        self.patchAsync = mock.patch('tasks.tasks_judge.async_task')
        self.mockAsync = self.patchAsync.start()
        self.mockAsync.return_value = 'q2-new-task'
        self.addCleanup(self.patchAsync.stop)

    def makeSubmission(self, **kwargs):
        """建提交 + JudgeTask（走任务层，不经过受理 API）"""
        self.submissionSeq += 1
        data = dict(
            user=self.student, problem=self.problem, language='c',
            code='int main(){return 0;}',
            fingerprint=f'fp-{self.submissionSeq}',
            submit_type=Submission.SUBMIT_PRACTICE,
            total_score=100, problem_revision=self.problem.revision,
            tests_hash='tests-hash',
            judge_snapshot=self.snapshot)
        data.update(kwargs)  # 显式传参覆盖默认值（dict 重复键不能用 **kwargs 展开）
        submission = Submission.objects.create(**data)
        JudgeTask.objects.create(submission=submission)
        return submission

    # ---------- 主流程 ----------

    def test_fullAccepted(self):
        """全 AC：score=total、终态 done、fileId 判后删除、资源档显式携带"""
        submission = self.makeSubmission()
        self.mockRun.side_effect = [
            [compileOkResult()],
            [acceptedResult('3')],
            [acceptedResult('5')],
            [acceptedResult('7')],
        ]
        runJudgeTask(submission.pk)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.STATUS_ACCEPTED)
        self.assertEqual(submission.score, 100)
        self.assertEqual(submission.total_score, 100)
        task = submission.judge_task
        self.assertEqual(task.status, JudgeTask.STATUS_DONE)
        self.assertEqual(task.progress, 100)
        self.assertEqual(task.file_id, '')
        self.assertIsNotNone(task.heartbeat_at)      # 心跳每点同写
        self.assertEqual(self.mockRun.call_count, 4)  # 编译 + 3 点
        self.mockDelete.assert_called_once_with('fid-1')
        # 资源档显式携带（第十三章：入门档 cpu 2s、procLimit 32）
        firstRun = self.mockRun.call_args_list[1].args[0][0]
        self.assertEqual(firstRun['cpuLimit'], 2_000_000_000)
        self.assertEqual(firstRun['procLimit'], 32)
        self.assertEqual(firstRun['args'], ['./main'])
        # time/memory 聚合取最大单点（纳秒/byte）
        self.assertEqual(submission.memory_used, 1_048_576)
        self.assertEqual(submission.time_used, 10_000_000)

    def test_partialScoreAggregation(self):
        """部分点 WA/TLE：全部测试点执行完 + score 聚合 + 主状态取最差单点"""
        submission = self.makeSubmission()
        self.mockRun.side_effect = [
            [compileOkResult()],
            [acceptedResult('3')],                      # 点 1：40 分
            [acceptedResult('999\n')],                    # 点 2：WA
            [statusResult('Time Limit Exceeded')],        # 点 3：TLE
        ]
        runJudgeTask(submission.pk)
        submission.refresh_from_db()
        self.assertEqual(self.mockRun.call_count, 4)  # 不提前终止，全点执行
        self.assertEqual(submission.score, 40)
        self.assertEqual(submission.status, Submission.STATUS_TLE)
        self.assertIn('第 3 个测试点', submission.error_detail)

    def test_compileError(self):
        """CE：compile_error + 零逐点调用 + 编译 stderr 入详情"""
        submission = self.makeSubmission()
        self.mockRun.side_effect = [
            [statusResult('Nonzero Exit Status', stderr='main.c:1: error: xyz')],
        ]
        runJudgeTask(submission.pk)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.STATUS_COMPILE_ERROR)
        self.assertEqual(submission.score, 0)
        self.assertEqual(self.mockRun.call_count, 1)
        self.assertIn('error: xyz', submission.error_detail)
        self.mockDelete.assert_not_called()

    def test_repeatZeroHttp(self):
        """repeat：fingerprint 命中 → repeat_of 指向 + 零 HTTP"""
        first = self.makeSubmission(fingerprint='same-fp')
        first.status = Submission.STATUS_ACCEPTED
        first.save(update_fields=['status'])
        dup = self.makeSubmission(fingerprint='same-fp')
        runJudgeTask(dup.pk)
        dup.refresh_from_db()
        self.assertEqual(dup.status, Submission.STATUS_REPEAT)
        self.assertEqual(dup.repeat_of_id, first.pk)
        self.assertEqual(dup.error_detail, f'重复于提交 #{first.pk}')
        self.assertEqual(self.mockRun.call_count, 0)
        self.assertEqual(self.mockDelete.call_count, 0)

    def test_changed_problem_version_is_not_repeat(self):
        from .tasks_judge import detectRepeat

        self.makeSubmission(fingerprint='same-fp')
        changed = self.makeSubmission(fingerprint='same-fp', problem_revision=2)
        self.assertIsNone(detectRepeat(changed))
        newTests = self.makeSubmission(fingerprint='same-fp', tests_hash='new-hash')
        self.assertIsNone(detectRepeat(newTests))
        otherLanguage = self.makeSubmission(fingerprint='same-fp', language='python')
        self.assertIsNone(detectRepeat(otherLanguage))
        otherHomework = self.makeSubmission(fingerprint='same-fp', homework=self.homework,
                                            submit_type=Submission.SUBMIT_HOMEWORK)
        self.assertIsNone(detectRepeat(otherHomework))

    def test_repeat_ignores_unfinished_and_platform_failed_submissions(self):
        for status in (Submission.STATUS_QUEUED, Submission.STATUS_RUNNING,
                       Submission.STATUS_SYSTEM_ERROR, Submission.STATUS_REPEAT):
            with self.subTest(status=status):
                fingerprint = f'fp-{status}'
                self.makeSubmission(fingerprint=fingerprint, status=status)
                current = self.makeSubmission(fingerprint=fingerprint)
                self.assertIsNone(tasks_judge.detectRepeat(current))

    def test_pythonSkipsCompile(self):
        """Python：无编译请求，代码逐点内联"""
        submission = self.makeSubmission(language='python')
        self.mockRun.side_effect = [
            [acceptedResult('3')],
            [acceptedResult('5')],
            [acceptedResult('7')],
        ]
        runJudgeTask(submission.pk)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.STATUS_ACCEPTED)
        self.assertEqual(self.mockRun.call_count, 3)
        firstRun = self.mockRun.call_args_list[0].args[0][0]
        self.assertEqual(firstRun['args'], ['python3', 'main.py'])
        self.mockDelete.assert_not_called()

    # ---------- 资格检查（全部零 HTTP） ----------

    def test_problemDeleted(self):
        """题目删除 → system_error「题目已被删除」"""
        submission = self.makeSubmission(problem=None)
        runJudgeTask(submission.pk)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.STATUS_SYSTEM_ERROR)
        self.assertEqual(submission.error_detail, '题目已被删除')
        self.assertEqual(self.mockRun.call_count, 0)

    def test_userBanned(self):
        """用户封禁 → system_error「账号已被封禁」"""
        self.student.is_active = False
        self.student.save(update_fields=['is_active'])
        submission = self.makeSubmission()
        runJudgeTask(submission.pk)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.STATUS_SYSTEM_ERROR)
        self.assertEqual(submission.error_detail, '账号已被封禁')
        self.assertEqual(self.mockRun.call_count, 0)

    def test_removedFromClass(self):
        """作业提交者已移出教学班 → system_error（学生不在教学班组成中）"""
        submission = self.makeSubmission(
            homework=self.homework, submit_type=Submission.SUBMIT_HOMEWORK)
        runJudgeTask(submission.pk)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.STATUS_SYSTEM_ERROR)
        self.assertEqual(submission.error_detail, '已被移出教学班')
        self.assertEqual(self.mockRun.call_count, 0)

    # ---------- 租约状态机 ----------

    def test_terminalIdempotent(self):
        """终态幂等：done 重投直接返回，零 HTTP"""
        submission = self.makeSubmission()
        JudgeTask.objects.filter(submission=submission).update(
            status=JudgeTask.STATUS_DONE)
        runJudgeTask(submission.pk)
        self.assertEqual(self.mockRun.call_count, 0)

    def test_claimLeaseConcurrentOnlyOneWins(self):
        """并发租约：条件 UPDATE 原子性保证两个 worker 只有一个领到"""
        submission = self.makeSubmission()
        task = submission.judge_task
        self.assertEqual(claimLease(task), 1)
        secondView = JudgeTask.objects.get(pk=task.pk)
        self.assertIsNone(claimLease(secondView))  # 已 leased，第二个领不到

    def test_claim_rolls_back_if_submission_cannot_enter_running(self):
        submission = self.makeSubmission()
        task = submission.judge_task
        originalUpdate = QuerySet.update

        def failSubmission(queryset, **fields):
            if queryset.model is Submission:
                raise RuntimeError('模拟提交状态写入失败')
            return originalUpdate(queryset, **fields)

        with mock.patch.object(QuerySet, 'update', failSubmission):
            with self.assertRaises(RuntimeError):
                claimLease(task)
        task.refresh_from_db()
        self.assertEqual(task.status, JudgeTask.STATUS_PENDING)
        self.assertEqual(task.attempt_id, 0)

    def test_terminal_submission_cannot_be_claimed_or_overwritten(self):
        submission = self.makeSubmission()
        task = submission.judge_task
        attemptId = claimLease(task)
        self.assertTrue(tasks_judge.writeTerminal(
            task, attemptId, Submission.STATUS_ACCEPTED, 100, ''))
        self.assertFalse(tasks_judge.writeTerminal(
            task, attemptId, Submission.STATUS_WRONG_ANSWER, 0, 'late'))
        tasks_judge.markFailed(task, attemptId, 'late failure')
        JudgeTask.objects.filter(pk=task.pk).update(status=JudgeTask.STATUS_PENDING)
        self.assertIsNone(claimLease(task))
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.STATUS_ACCEPTED)
        self.assertEqual(submission.score, 100)

    def test_expiredAttemptWriteDiscarded(self):
        """过期 attempt 丢弃：租约被回收后，旧 attempt 的终态写不生效"""
        submission = self.makeSubmission()
        task = submission.judge_task
        attemptId = claimLease(task)
        # 模拟租约回收：attempt_id+1 → retryable（新令牌建立）
        JudgeTask.objects.filter(pk=task.pk).update(
            status=JudgeTask.STATUS_RETRYABLE, attempt_id=attemptId + 1)
        written = tasks_judge.writeTerminal(
            task, attemptId, Submission.STATUS_ACCEPTED, 100, '')
        self.assertFalse(written)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.STATUS_RUNNING)

    def test_transientErrorFallsBackForRetry(self):
        """沙箱瞬时错误：回退 retryable + 清 file_id + 清理产物 + 抛异常重投"""
        submission = self.makeSubmission()
        self.mockRun.side_effect = [
            [compileOkResult()],
            [statusResult('File Error')],
        ]
        with self.assertRaises(JudgeTransientError):
            runJudgeTask(submission.pk)
        task = submission.judge_task
        task.refresh_from_db()
        self.assertEqual(task.status, JudgeTask.STATUS_RETRYABLE)
        self.assertEqual(task.attempt_id, 2)   # 回退与回收均作废旧令牌
        self.assertEqual(task.file_id, '')
        self.mockDelete.assert_called_once_with('fid-1')
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.STATUS_RUNNING)

    def test_retryExhaustedFails(self):
        """重试耗尽：第 2 次执行再遇瞬时错误 → failed + system_error 不抛"""
        submission = self.makeSubmission()
        # 预置为回收后等待重投的状态（attempt_id=2 → 领取后为第 2 次执行）
        JudgeTask.objects.filter(submission=submission).update(
            status=JudgeTask.STATUS_RETRYABLE, attempt_id=2)
        self.mockRun.side_effect = [
            [compileOkResult()],
            [statusResult('File Error')],
        ]
        runJudgeTask(submission.pk)  # 不应抛异常
        submission.refresh_from_db()
        task = submission.judge_task
        task.refresh_from_db()
        self.assertEqual(task.status, JudgeTask.STATUS_FAILED)
        self.assertEqual(task.attempt_id, 3)   # claimLease +1
        self.assertEqual(submission.status, Submission.STATUS_SYSTEM_ERROR)

    def test_two_actual_transient_failures_exhaust_attempts(self):
        submission = self.makeSubmission()
        self.mockRun.side_effect = [
            [compileOkResult()], [statusResult('File Error')],
            [compileOkResult()], [statusResult('File Error')],
        ]
        with self.assertRaises(JudgeTransientError):
            runJudgeTask(submission.pk)
        runJudgeTask(submission.pk)
        runJudgeTask(submission.pk)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.STATUS_SYSTEM_ERROR)
        self.assertEqual(submission.judge_task.status, JudgeTask.STATUS_FAILED)
        self.assertEqual(self.mockRun.call_count, 4)

    def test_recycleStaleLease(self):
        """租约回收：心跳超过重试预算 → 作废旧令牌 + 清产物 + 重投。"""
        submission = self.makeSubmission()
        task = submission.judge_task
        JudgeTask.objects.filter(pk=task.pk).update(
            status=JudgeTask.STATUS_LEASED, attempt_id=1, file_id='fid-x',
            heartbeat_at=timezone.now() - timedelta(
                seconds=tasks_judge.getHeartbeatTimeoutSeconds() + 1))
        recycleStaleLeases()
        task.refresh_from_db()
        self.assertEqual(task.status, JudgeTask.STATUS_RETRYABLE)
        self.assertEqual(task.attempt_id, 2)   # 回收 +1 作废旧令牌
        self.assertEqual(task.file_id, '')
        self.mockDelete.assert_called_once_with('fid-x')
        self.mockAsync.assert_called_once_with(
            'tasks.tasks_judge.runJudgeTask', submission.pk, cluster='long')
        recycleStaleLeases()
        self.assertEqual(self.mockAsync.call_count, 1)

    def test_recycle_rechecks_a_heartbeat_refreshed_after_scan(self):
        submission = self.makeSubmission()
        task = submission.judge_task
        JudgeTask.objects.filter(pk=task.pk).update(
            status=JudgeTask.STATUS_LEASED, attempt_id=1,
            heartbeat_at=timezone.now() - timedelta(hours=1))
        originalUpdate = QuerySet.update
        refreshed = False

        def refreshBeforeRecycle(queryset, **fields):
            nonlocal refreshed
            if (queryset.model is JudgeTask
                    and fields.get('status') == JudgeTask.STATUS_RETRYABLE
                    and not refreshed):
                refreshed = True
                originalUpdate(JudgeTask.objects.filter(pk=task.pk),
                               heartbeat_at=timezone.now())
            return originalUpdate(queryset, **fields)

        with mock.patch.object(QuerySet, 'update', refreshBeforeRecycle):
            recycleStaleLeases()
        task.refresh_from_db()
        self.assertTrue(refreshed)
        self.assertEqual(task.status, JudgeTask.STATUS_LEASED)
        self.assertEqual(task.attempt_id, 1)
        self.mockAsync.assert_not_called()

    def test_recycleFreshLeaseUntouched(self):
        """心跳新鲜的 leased 任务不受回收影响"""
        submission = self.makeSubmission()
        JudgeTask.objects.filter(submission=submission).update(
            status=JudgeTask.STATUS_LEASED, attempt_id=1,
            heartbeat_at=timezone.now())
        recycleStaleLeases()
        task = submission.judge_task
        task.refresh_from_db()
        self.assertEqual(task.status, JudgeTask.STATUS_LEASED)
        self.assertEqual(task.attempt_id, 1)
        self.mockAsync.assert_not_called()

    def test_pendingRedispatch(self):
        """补投扫描：pending 超 120s 未投递 → 重新投递（受理投递失败兜底）"""
        submission = self.makeSubmission()
        Submission.objects.filter(pk=submission.pk).update(
            created_at=timezone.now() - timedelta(seconds=130))
        recycleStaleLeases()
        self.mockAsync.assert_called_once_with(
            'tasks.tasks_judge.runJudgeTask', submission.pk, cluster='long')
        self.assertEqual(submission.judge_task.status, JudgeTask.STATUS_PENDING)

    def test_pending_dispatch_is_recorded_and_not_duplicated_by_next_scan(self):
        submission = self.makeSubmission()
        Submission.objects.filter(pk=submission.pk).update(
            created_at=timezone.now() - timedelta(hours=1))
        recycleStaleLeases()
        recycleStaleLeases()
        task = submission.judge_task
        task.refresh_from_db()
        self.assertEqual(task.q2_task_id, 'q2-new-task')
        self.assertIsNotNone(task.last_dispatched_at)
        self.assertEqual(self.mockAsync.call_count, 1)

    def test_successfully_queued_task_waits_for_long_retry_window(self):
        submission = self.makeSubmission()
        Submission.objects.filter(pk=submission.pk).update(
            created_at=timezone.now() - timedelta(hours=1))
        JudgeTask.objects.filter(submission=submission).update(
            q2_task_id='q2-old-task',
            last_dispatched_at=timezone.now() - timedelta(seconds=200))
        recycleStaleLeases()
        self.mockAsync.assert_not_called()
        JudgeTask.objects.filter(submission=submission).update(
            last_dispatched_at=timezone.now() - timedelta(seconds=1001))
        recycleStaleLeases()
        self.assertEqual(self.mockAsync.call_count, 1)

    def test_failed_redispatch_recovers_without_immediate_duplicate_scan(self):
        submission = self.makeSubmission()
        Submission.objects.filter(pk=submission.pk).update(
            created_at=timezone.now() - timedelta(hours=1))
        self.mockAsync.side_effect = RuntimeError('broker down')
        recycleStaleLeases()
        recycleStaleLeases()
        task = submission.judge_task
        task.refresh_from_db()
        self.assertEqual(task.q2_task_id, '')
        self.assertIsNotNone(task.last_dispatched_at)
        self.assertEqual(self.mockAsync.call_count, 1)
        self.mockAsync.side_effect = None
        JudgeTask.objects.filter(pk=task.pk).update(
            last_dispatched_at=timezone.now() - timedelta(seconds=121))
        recycleStaleLeases()
        self.assertEqual(self.mockAsync.call_count, 2)

    def test_dispatch_reservation_blocks_an_overlapping_recycle_scan(self):
        submission = self.makeSubmission()
        Submission.objects.filter(pk=submission.pk).update(
            created_at=timezone.now() - timedelta(hours=1))

        def scanWhileDispatching(*args, **kwargs):
            recycleStaleLeases()
            return 'q2-new-task'

        self.mockAsync.side_effect = scanWhileDispatching
        recycleStaleLeases()
        self.assertEqual(self.mockAsync.call_count, 1)

    def test_orphan_retries_stop_when_execution_budget_is_used(self):
        submission = self.makeSubmission()
        JudgeTask.objects.filter(submission=submission).update(
            status=JudgeTask.STATUS_RETRYABLE, attempt_id=4,
            claimed_at=None)
        recycleStaleLeases()
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.STATUS_SYSTEM_ERROR)
        self.assertEqual(submission.judge_task.status, JudgeTask.STATUS_FAILED)
        self.mockAsync.assert_not_called()

    def test_late_compilation_artifact_is_deleted_when_lease_was_recycled(self):
        submission = self.makeSubmission()

        def expireDuringCompile(request):
            JudgeTask.objects.filter(submission=submission).update(
                attempt_id=2, status=JudgeTask.STATUS_RETRYABLE)
            return [compileOkResult()]

        self.mockRun.side_effect = expireDuringCompile
        runJudgeTask(submission.pk)
        self.mockDelete.assert_called_once_with('fid-1')
        self.assertEqual(self.mockRun.call_count, 1)
        self.assertEqual(submission.judge_task.file_id, '')

    def test_failed_artifact_cleanup_remains_available_to_recycler(self):
        submission = self.makeSubmission()
        self.mockRun.side_effect = [
            [compileOkResult()], [acceptedResult('3')],
            [acceptedResult('5')], [acceptedResult('7')],
        ]
        self.mockDelete.side_effect = tasks_judge.judge.NetworkError('offline')
        runJudgeTask(submission.pk)
        task = submission.judge_task
        task.refresh_from_db()
        self.assertEqual(task.file_id, 'fid-1')
        self.mockDelete.side_effect = None
        recycleStaleLeases()
        task.refresh_from_db()
        self.assertEqual(task.file_id, '')

    def test_claim_waits_for_previous_artifact_cleanup(self):
        submission = self.makeSubmission()
        task = submission.judge_task
        JudgeTask.objects.filter(pk=task.pk).update(file_id='old-artifact')
        self.mockDelete.side_effect = tasks_judge.judge.NetworkError('offline')
        self.assertIsNone(claimLease(task))
        task.refresh_from_db()
        self.assertEqual(task.status, JudgeTask.STATUS_PENDING)
        self.assertEqual(task.file_id, 'old-artifact')
        self.mockDelete.side_effect = None
        self.assertEqual(claimLease(task), 1)
        task.refresh_from_db()
        self.assertEqual(task.file_id, '')

    def test_failed_late_artifact_delete_is_enqueued_for_cleanup(self):
        submission = self.makeSubmission()

        def expireDuringCompile(request):
            JudgeTask.objects.filter(submission=submission).update(
                attempt_id=2, status=JudgeTask.STATUS_RETRYABLE)
            return [compileOkResult()]

        self.mockRun.side_effect = expireDuringCompile
        self.mockDelete.side_effect = tasks_judge.judge.NetworkError('offline')
        runJudgeTask(submission.pk)
        self.mockAsync.assert_called_once_with(
            'tasks.tasks_judge.cleanupDetachedFile', 'fid-1', cluster='long')
        self.mockDelete.side_effect = None
        tasks_judge.cleanupDetachedFile('fid-1')
        self.assertEqual(self.mockDelete.call_count, 2)

    # ---------- SPJ ----------

    def test_spjFullScore(self):
        """spj 满分：exitStatus=0 → 该点满分；每点 2 次沙箱调用"""
        spjSnapshot = dict(self.snapshot, spj_code='import sys\nsys.exit(0)')
        submission = self.makeSubmission(judge_snapshot=spjSnapshot)
        self.mockRun.side_effect = [
            [compileOkResult()],
            [acceptedResult('3')], [acceptedResult('ok')],
            [acceptedResult('5')], [acceptedResult('ok')],
            [acceptedResult('7')], [acceptedResult('ok')],
        ]
        runJudgeTask(submission.pk)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.STATUS_ACCEPTED)
        self.assertEqual(submission.score, 100)
        self.assertEqual(self.mockRun.call_count, 7)  # 编译 + 3×(用户+checker)

    def test_spjPartialScore(self):
        """spj 部分分：exitStatus≠0（Nonzero Exit Status）时 stdout 首行
        整数为得分；非法输出 0 分；checker 非零退出不按故障处理"""
        spjSnapshot = dict(self.snapshot, spj_code='spj')
        submission = self.makeSubmission(judge_snapshot=spjSnapshot)
        self.mockRun.side_effect = [
            [compileOkResult()],
            [acceptedResult('3')],
            # 真实形态：go-judge 对 exit≠0 报 Nonzero Exit Status（协议信号）
            [acceptedResult('15', exitStatus=1,
                            status='Nonzero Exit Status')],   # 15 ∈ [0,40) → 15 分
            [acceptedResult('5')], [acceptedResult('', exitStatus=0)],
            [acceptedResult('7')],
            [acceptedResult('abc', exitStatus=1,
                            status='Nonzero Exit Status')],   # 首行非整数 → 0 分
        ]
        runJudgeTask(submission.pk)
        submission.refresh_from_db()
        self.assertEqual(submission.score, 15 + 30 + 0)
        self.assertEqual(submission.status, Submission.STATUS_WRONG_ANSWER)

    def test_spjCheckerFailureSystemError(self):
        """checker 沙箱故障重试 1 次仍失败 → system_error（≠ 用户 WA）"""
        spjSnapshot = dict(self.snapshot, spj_code='spj')
        submission = self.makeSubmission(judge_snapshot=spjSnapshot)
        self.mockRun.side_effect = [
            [compileOkResult()],
            [acceptedResult('3')],
            [statusResult('File Error')],
            [statusResult('File Error')],
        ]
        runJudgeTask(submission.pk)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.STATUS_SYSTEM_ERROR)
        self.assertEqual(self.mockRun.call_count, 4)  # 编译+用户+checker×2
        self.assertIn('checker', submission.error_detail)

    def test_checker_client_transient_errors_retry_locally(self):
        submission = self.makeSubmission(judge_snapshot=dict(
            self.snapshot, spj_code='spj'))
        self.mockRun.side_effect = [
            [compileOkResult()], [acceptedResult('3')],
            tasks_judge.judge.SandboxError('Internal Error'),
            tasks_judge.judge.NetworkError('offline'),
        ]
        runJudgeTask(submission.pk)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.STATUS_SYSTEM_ERROR)
        self.assertIn('checker', submission.error_detail)
        self.assertEqual(submission.judge_task.attempt_id, 1)
        self.assertEqual(self.mockRun.call_count, 4)

    def test_lease_is_renewed_before_each_sandbox_request(self):
        submission = self.makeSubmission(judge_snapshot=dict(
            self.snapshot, spj_code='spj'))
        self.mockRun.side_effect = [
            [compileOkResult()],
            [acceptedResult('3')], [acceptedResult('')],
            [acceptedResult('5')], [acceptedResult('')],
            [acceptedResult('7')], [acceptedResult('')],
        ]
        with mock.patch('tasks.tasks_judge.renewLease',
                        wraps=tasks_judge.renewLease) as renewed:
            runJudgeTask(submission.pk)
        self.assertGreaterEqual(renewed.call_count, 14)
        self.assertGreaterEqual(tasks_judge.getHeartbeatTimeoutSeconds(), 150)

    def test_slow_http_retry_stays_inside_lease_budget(self):
        submission = self.makeSubmission(language='python', judge_snapshot=dict(
            self.snapshot, tests=[{'input': '', 'output': 'ok', 'points': 100}]))
        simulatedNow = timezone.now()

        def slowResponse(request):
            nonlocal simulatedNow
            # 不实际等待；模拟单次 /run 重试链耗时 100s，同时回收扫描运行。
            simulatedNow += timedelta(seconds=100)
            recycleStaleLeases()
            return [acceptedResult('ok')]

        self.mockRun.side_effect = slowResponse
        with mock.patch('tasks.tasks_judge.timezone.now',
                        side_effect=lambda: simulatedNow):
            runJudgeTask(submission.pk)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.STATUS_ACCEPTED)
        self.assertEqual(submission.judge_task.attempt_id, 1)
        self.mockAsync.assert_not_called()

    # ---------- 比对器与聚合 ----------

    def test_compareStrategies(self):
        """三策略比对（第五章精确定义）正反例"""
        # exact：逐字节相等，不规范化
        self.assertTrue(compareOutputs('1 2\n', '1 2\n', 'exact'))
        self.assertFalse(compareOutputs('1 2\n', '1 2 \n', 'exact'))
        self.assertFalse(compareOutputs('1 2\n', '1 2\r\n', 'exact'))
        # trailing：仅行尾 ASCII 空白 + 首尾空行容忍
        self.assertTrue(compareOutputs('1 2  \n\n', '1 2\n', 'trailing'))
        self.assertTrue(compareOutputs('\n1 2\n', '1 2\n', 'trailing'))
        self.assertFalse(compareOutputs(' 1 2\n', '1 2\n', 'trailing'))  # 行首不动
        # loose：全部空白忽略 + 统一换行（含 Unicode 空白）
        self.assertTrue(compareOutputs('1 2\r\n', '12\n', 'loose'))
        self.assertTrue(compareOutputs('a b', 'ab', 'loose'))
        self.assertFalse(compareOutputs('1 2', '1 3', 'loose'))

    def test_aggregateSeverityOrder(self):
        """聚合严重度序（8.5）：RE > TLE > MLE > OLE > WA"""
        points = [
            {'status': Submission.STATUS_ACCEPTED, 'score': 40, 'detail': '',
             'time': 1, 'memory': 1},
            {'status': Submission.STATUS_TLE, 'score': 0, 'detail': 't',
             'time': 1, 'memory': 1},
            {'status': Submission.STATUS_RUNTIME_ERROR, 'score': 0,
             'detail': 'r', 'time': 1, 'memory': 1},
        ]
        status, score = aggregatePoints(points)
        self.assertEqual(status, Submission.STATUS_RUNTIME_ERROR)
        self.assertEqual(score, 40)

    def test_unknownTierFallsBackToBeginner(self):
        """未知资源档位按入门档兜底（保守从严）"""
        self.assertEqual(tasks_judge.getTierLimits('bogus'),
                         tasks_judge.getTierLimits(Problem.TIER_BEGINNER))

    # ---------- 脱敏 ----------

    def test_waDetailSanitized(self):
        """隐藏点只给固定文案，学生回显输入不能泄露测试数据。"""
        # 用独特哨兵文本作预期输出，避免与输出预览内容巧合撞车
        sentinel = 'EXPECT_SENTINEL_7788'
        snapshot = dict(self.snapshot, tests=[
            {'input': '1 2', 'output': sentinel, 'points': 40},
            {'input': '2 3', 'output': '5', 'points': 30},
            {'input': '3 4', 'output': '7', 'points': 30},
        ])
        submission = self.makeSubmission(judge_snapshot=snapshot)
        actual = '\x1b[31m99\x1b[0m' + 'x' * 600
        self.mockRun.side_effect = [
            [compileOkResult()],
            [acceptedResult(actual)],
            [acceptedResult('5')],
            [acceptedResult('7')],
        ]
        runJudgeTask(submission.pk)
        submission.refresh_from_db()
        detail = submission.error_detail
        self.assertIn('第 1 个测试点未通过', detail)
        self.assertEqual(detail, '第 1 个测试点未通过')
        self.assertNotIn('99', detail)
        self.assertNotIn('\x1b', detail)                    # 控制字符被清洗
        self.assertNotIn(sentinel, detail)                  # 无预期输出
        self.assertLess(len(detail), 600)                   # 截断生效

    def test_runtime_stderr_cannot_echo_hidden_test_input(self):
        submission = self.makeSubmission(language='python')
        self.mockRun.side_effect = [
            [statusResult('Nonzero Exit Status', stderr='HIDDEN_INPUT')],
            [acceptedResult('5')], [acceptedResult('7')],
        ]
        runJudgeTask(submission.pk)
        submission.refresh_from_db()
        self.assertEqual(submission.error_detail, '第 1 个测试点：运行时错误')
        self.assertNotIn('HIDDEN_INPUT', submission.error_detail)

    def test_sanitizeOutput(self):
        """sanitizeOutput：控制字符清洗 + 截断（含截断标记）"""
        self.assertEqual(sanitizeOutput('ab\x01c'), 'abc')
        self.assertEqual(sanitizeOutput('a\nb'), 'a\nb')   # 换行保留
        truncated = sanitizeOutput('x' * 1000, 100)
        self.assertTrue(truncated.startswith('x' * 100))
        self.assertIn('截断', truncated)
