"""提交 API 测试：受理九项校验 + 快照冻结 + 投递断言 + 查询对象级与白名单

规格：docs/OJ对接设计.md 第七章（同步层）与 12.2（查询 API）。
投递断言口径：async_task 以 (函数路径, submissionId, cluster='long')
投递一次；投递失败不影响 200 响应（q2_task_id 留空，补投扫描兜底）。
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group
from django.utils import timezone
from rest_framework.test import APIClient
from django.test import TestCase

from academics.models import (AdminClass, ClassMembership, Course,
                              CourseCollege, Department, Major, TeachingClass)
from teaching.models import (Homework, HomeworkProblem, JudgeTask, Problem,
                             Submission)
from users.models import SchoolIdentity, StudentProfile, User

PASSWORD = 'Test12345'


class SubmissionApiTests(TestCase):
    """提交受理与查询 API（学生视角 + 越权矩阵）"""

    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(username='T9001', password=PASSWORD)
        cls.student = User.objects.create_user(username='S9001', password=PASSWORD)
        cls.otherStudent = User.objects.create_user(username='S9002', password=PASSWORD)
        cls.superUser = User.objects.create_superuser(username='SU9001', password=PASSWORD)
        # 学生入组（homework:view 权限码由数据迁移分配给学生组）
        studentGroup = Group.objects.get(name='学生')
        cls.student.groups.add(studentGroup)
        cls.otherStudent.groups.add(studentGroup)
        # 学生身份链（getStudentTeachingClass 三跳推导依赖）
        department = Department.objects.create(code='D95', name='测试学院')
        major = Major.objects.create(department=department, code='95', name='测试专业')
        adminClass = AdminClass.objects.create(
            name='测试2301', major=major, enrollment_year=2023)
        otherAdminClass = AdminClass.objects.create(
            name='测试2302', major=major, enrollment_year=2023)
        course = Course.objects.create(code='TEST01', name='测试课程')
        courseCollege = CourseCollege.objects.create(
            course=course, department=department)
        courseCollege.admin_classes.add(adminClass, otherAdminClass)
        cls.teachingClass = TeachingClass.objects.create(
            name='测试一班', term='2026-2027-1', course_college=courseCollege)
        cls.teachingClass.admin_classes.add(adminClass)
        cls.otherTeachingClass = TeachingClass.objects.create(
            name='测试二班', term='2026-2027-1', course_college=courseCollege)
        cls.otherTeachingClass.admin_classes.add(otherAdminClass)

        def makeStudent(user, number, targetClass):
            identity = SchoolIdentity.objects.create(
                user=user, school_number=number,
                identity_type=SchoolIdentity.IDENTITY_STUDENT)
            profile = StudentProfile.objects.create(
                identity=identity, major=major, enrollment_year=2023)
            ClassMembership.objects.create(
                student_profile=profile, admin_class=targetClass,
                joined_at=timezone.now().date())

        makeStudent(cls.student, '20239501', adminClass)
        makeStudent(cls.otherStudent, '20239502', otherAdminClass)

        cls.problem = Problem.objects.create(
            title='A+B 问题',
            statement_md='计算两个整数的和。',
            samples=[{'input': '1 2', 'output': '3'}],
            tests=[{'input': '1 2', 'output': '3', 'points': 100}],
            languages=['c', 'python'],
            owner=cls.teacher,
        )

    def setUp(self):
        # mock 投递：受理不产生真实队列记录（投递断言测试单独控制）
        self.asyncTaskPatcher = mock.patch(
            'teaching.submission_service.async_task', return_value='q2-task-1')
        self.mockAsyncTask = self.asyncTaskPatcher.start()
        self.addCleanup(self.asyncTaskPatcher.stop)
        self.client = APIClient()
        self.client.force_login(self.student)
        self.homework = self._openHomework()

    def _payload(self, **kwargs):
        payload = {
            'problemId': self.problem.id,
            'submitType': 'homework',
            'homeworkId': self.homework.id,
            'language': 'c',
            'code': 'int main() { return 0; }',
        }
        payload.update(kwargs)
        return payload

    def _submit(self, **kwargs):
        return self.client.post('/api/submissions/', self._payload(**kwargs),
                                format='json')

    def _openHomework(self, startOffset=-3600, endOffset=3600):
        """开放中的作业 + 题目关联（默认当前时间窗口内）"""
        now = timezone.now()
        homework = Homework.objects.create(
            teaching_class=self.teachingClass, title='第一次作业',
            start_at=now + timedelta(seconds=startOffset),
            deadline=now + timedelta(seconds=endOffset),
            created_by=self.teacher)
        HomeworkProblem.objects.create(
            homework=homework, problem=self.problem, order=1,
            problem_revision=self.problem.revision)
        return homework

    # ---------- 越权矩阵 ----------

    def test_unauthenticated403(self):
        """未登录 403（DRF SessionAuthentication 语义，与项目现有断言一致；
        身份只来自认证上下文，请求体 userId 被忽略）"""
        resp = APIClient().post('/api/submissions/', self._payload(),
                                format='json')
        self.assertEqual(resp.status_code, 403)

    def test_teacher403(self):
        """教师无 homework:view 权限码 → 403"""
        teacherClient = APIClient()
        teacherClient.force_login(self.teacher)
        resp = teacherClient.post('/api/submissions/', self._payload(),
                                  format='json')
        self.assertEqual(resp.status_code, 403)

    # ---------- 受理 ----------

    def test_homeworkSubmitQueued(self):
        """作业提交受理成功：200 submissionId + queued + JudgeTask pending"""
        resp = self._submit()
        self.assertEqual(resp.status_code, 200)
        submissionId = resp.json()['submissionId']
        submission = Submission.objects.get(pk=submissionId)
        self.assertEqual(submission.status, Submission.STATUS_QUEUED)
        self.assertEqual(submission.submit_type, Submission.SUBMIT_HOMEWORK)
        self.assertEqual(submission.user, self.student)
        self.assertEqual(submission.total_score, 100)
        task = submission.judge_task
        self.assertEqual(task.status, JudgeTask.STATUS_PENDING)

    def test_dispatchAfterCommit(self):
        """事务提交后投递：async_task(函数路径, submissionId, cluster='long')，
        q2_task_id 回写"""
        with self.captureOnCommitCallbacks(execute=True):
            resp = self._submit()
        self.assertEqual(resp.status_code, 200)
        submissionId = resp.json()['submissionId']
        self.mockAsyncTask.assert_called_once_with(
            'tasks.tasks_judge.runJudgeTask', submissionId, cluster='long')
        task = Submission.objects.get(pk=submissionId).judge_task
        self.assertEqual(task.q2_task_id, 'q2-task-1')

    def test_dispatchFailureStillAccepted(self):
        """投递失败不影响受理响应：200 + q2_task_id 留空（补投扫描兜底）"""
        self.mockAsyncTask.side_effect = RuntimeError('broker down')
        with self.captureOnCommitCallbacks(execute=True):
            resp = self._submit()
        self.assertEqual(resp.status_code, 200)
        submissionId = resp.json()['submissionId']
        task = Submission.objects.get(pk=submissionId).judge_task
        self.assertEqual(task.q2_task_id, '')

    def test_snapshotFrozenOnSubmit(self):
        """受理时冻结资格快照：改题不影响已受理提交（7.3 结果可复现）"""
        resp = self._submit()
        submission = Submission.objects.get(pk=resp.json()['submissionId'])
        oldTests = submission.judge_snapshot['tests']
        oldHash = submission.tests_hash
        # 教师改题（测试点 + 修订号 +1）
        self.problem.tests = [{'input': '9 9', 'output': '18', 'points': 100}]
        self.problem.revision += 1
        self.problem.save()
        submission.refresh_from_db()
        self.assertEqual(submission.judge_snapshot['tests'], oldTests)
        self.assertEqual(submission.problem_revision, 1)
        self.assertEqual(submission.tests_hash, oldHash)

    def test_submit_uses_homework_version_after_bank_edit(self):
        from teaching.submission_service import calcTestsHash

        original = self.homework.problems.get().problem_snapshot
        self.problem.tests = []
        self.problem.languages = ['python']
        self.problem.tier = 'advanced'
        self.problem.compare_strategy = 'loose'
        self.problem.spj_code = 'NEW_CHECKER'
        self.problem.revision = 5
        self.problem.save()
        response = self._submit(language='c')
        self.assertEqual(response.status_code, 200)
        submission = Submission.objects.get(pk=response.data['submissionId'])
        self.assertEqual(submission.problem_revision, 1)
        self.assertEqual(submission.total_score, 100)
        self.assertEqual(submission.tests_hash, calcTestsHash(original['tests']))
        for field in ('tests', 'tier', 'compare_strategy', 'spj_code'):
            self.assertEqual(submission.judge_snapshot[field], original[field])

    # ---------- 限流 ----------

    def test_frequencyThrottle429(self):
        """频率闸：窗口内第 4 次提交 → 429（10 秒 3 次）"""
        for _ in range(3):
            self.assertEqual(self._submit().status_code, 200)
        resp = self._submit()
        self.assertEqual(resp.status_code, 429)

    def test_queueLimit400(self):
        """队列闸：同用户未终态 JudgeTask ≥3 → 400

        已有提交的 created_at 移到频率窗口外，确保触发的是队列闸而非频率闸。
        """
        for _ in range(3):
            self._submit()
        Submission.objects.update(
            created_at=timezone.now() - timedelta(seconds=20))
        resp = self._submit()
        self.assertEqual(resp.status_code, 400)

    def test_queueLimitReleasedAfterTerminal(self):
        """队列闸释放：未终态任务转 done 后可继续提交"""
        for _ in range(3):
            self._submit()
        Submission.objects.update(
            created_at=timezone.now() - timedelta(seconds=20))
        JudgeTask.objects.update(status=JudgeTask.STATUS_DONE)
        self.assertEqual(self._submit().status_code, 200)

    # ---------- 脏输入 ----------

    def test_invalidInputs(self):
        """脏输入矩阵：语言/空代码/超 64KB/非法类型/无测试点题/题不存在"""
        self.assertEqual(
            self._submit(language='java').status_code, 400)         # 语言不在白名单
        self.assertEqual(
            self._submit(language='python').status_code, 200)       # 题允许 python
        self.assertEqual(self._submit(code='   ').status_code, 400)  # 空代码
        self.assertEqual(
            self._submit(code='a' * (64 * 1024 + 1)).status_code, 400)  # 超 64KB
        self.assertEqual(
            self._submit(submitType='exam').status_code, 400)       # exam 留位二期
        noTestsProblem = Problem.objects.create(
            title='无测试点', statement_md='', tests=[],
            languages=['c'], owner=self.teacher)
        HomeworkProblem.objects.create(homework=self.homework, problem=noTestsProblem,
                                       order=2, problem_revision=1)
        self.assertEqual(
            self._submit(problemId=noTestsProblem.id).status_code, 400)  # 无测试点
        self.assertEqual(self._submit(problemId=99999).status_code, 404)  # 题不存在

    # ---------- 作业提交组合 ----------

    def test_homeworkSubmitAccepted(self):
        """作业提交合法组合：开放中 + 学生在班 + 题在作业 → 200"""
        homework = self._openHomework()
        resp = self._submit(submitType='homework', homeworkId=homework.id)
        self.assertEqual(resp.status_code, 200)
        submission = Submission.objects.get(pk=resp.json()['submissionId'])
        self.assertEqual(submission.homework_id, homework.id)

    def test_homeworkSubmitCombos(self):
        """作业提交非法组合矩阵：题不属于作业 404 / 未开始 400 / 已截止 400 /
        学生不在班 404 / 类型与作业参数不一致 400"""
        homework = self._openHomework()
        notInHomework = Problem.objects.create(
            title='别的题', statement_md='',
            tests=[{'input': '1', 'output': '1', 'points': 100}],
            languages=['c'], owner=self.teacher)
        self.assertEqual(
            self._submit(submitType='homework', homeworkId=homework.id,
                         problemId=notInHomework.id).status_code, 404)
        notStarted = self._openHomework(startOffset=3600, endOffset=7200)
        self.assertEqual(
            self._submit(submitType='homework', homeworkId=notStarted.id)
            .status_code, 400)
        expired = self._openHomework(startOffset=-7200, endOffset=-3600)
        self.assertEqual(
            self._submit(submitType='homework', homeworkId=expired.id)
            .status_code, 400)
        otherClassHomework = Homework.objects.create(
            teaching_class=self.otherTeachingClass, title='别的班作业',
            start_at=timezone.now() - timedelta(hours=1),
            deadline=timezone.now() + timedelta(hours=1),
            created_by=self.teacher)
        self.assertEqual(
            self._submit(submitType='homework', homeworkId=otherClassHomework.id)
            .status_code, 404)
        self.assertEqual(
            self._submit(submitType='homework', homeworkId=None).status_code, 400)
        self.assertEqual(
            self._submit(submitType='practice', homeworkId=homework.id)
            .status_code, 400)  # practice 带 homeworkId
        self.assertEqual(
            self._submit(submitType='homework', homeworkId=99999)
            .status_code, 404)  # 作业不存在

    # ---------- 查询 ----------

    def test_listOnlyOwnSubmissions(self):
        """列表只含本人提交；他人提交不可见（超管可见全部）"""
        self._submit()
        otherClient = APIClient()
        otherClient.force_login(self.otherStudent)
        otherHomework = self._openHomework()
        otherHomework.teaching_class = self.otherTeachingClass
        otherHomework.save()
        otherClient.post('/api/submissions/', self._payload(homeworkId=otherHomework.id),
                         format='json')
        resp = self.client.get('/api/submissions/')
        self.assertEqual(resp.status_code, 200)
        results = resp.json()['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['problemTitle'], 'A+B 问题')
        # 超管可见全部
        superClient = APIClient()
        superClient.force_login(self.superUser)
        resp = superClient.get('/api/submissions/')
        self.assertEqual(len(resp.json()['results']), 2)

    def test_listWhitelistFields(self):
        """列表响应白名单：不含测试点/快照/哈希等内部字段"""
        self._submit()
        data = self.client.get('/api/submissions/').json()['results'][0]
        for forbidden in ('tests', 'judgeSnapshot', 'testsHash',
                          'problemRevision', 'fingerprint', 'spjCode'):
            self.assertNotIn(forbidden, data)

    def test_detailObjectLevel(self):
        """详情对象级：本人 200 含 code；他人 404；超管放行"""
        self._submit()
        submissionId = self._submit().json()['submissionId']
        mine = self.client.get(f'/api/submissions/{submissionId}/')
        self.assertEqual(mine.status_code, 200)
        self.assertIn('code', mine.json())
        otherClient = APIClient()
        otherClient.force_login(self.otherStudent)
        self.assertEqual(
            otherClient.get(f'/api/submissions/{submissionId}/').status_code, 404)
        self.assertEqual(
            APIClient().get(f'/api/submissions/{submissionId}/').status_code, 403)
        superClient = APIClient()
        superClient.force_login(self.superUser)
        self.assertEqual(
            superClient.get(f'/api/submissions/{submissionId}/').status_code, 200)

    def test_problemDeletedTitleNull(self):
        """题目删除后提交保留：列表 problemId/problemTitle 为 null"""
        self._submit()
        HomeworkProblem.objects.filter(problem=self.problem).delete()
        self.problem.delete()
        data = self.client.get('/api/submissions/').json()['results'][0]
        self.assertIsNone(data['problemId'])
        self.assertIsNone(data['problemTitle'])

    def test_practiceRejectedWithoutRecordsOrDispatch(self):
        for isPublic in (True, False):
            self.problem.is_public = isPublic
            self.problem.save()
            for payload in ({'submitType': 'practice', 'homeworkId': None},
                            {'submitType': 'practice'}, {'homeworkId': None}):
                self.assertEqual(self._submit(**payload).status_code, 400)
        self.assertFalse(Submission.objects.exists())
        self.assertFalse(JudgeTask.objects.exists())
        self.mockAsyncTask.assert_not_called()
