"""OJ 五张表的模型测试：默认值、约束、索引、快照往返、外键行为

模型规格来自 docs/design/OJ对接设计.md 第九章：
- 默认值（revision=1 / status=queued / progress=0 等）是受理与
  租约状态机的起点，不可漂移
- Check 约束「挂作业的提交必须是作业类型」是 DB 层防非法组合的最后防线
- PROTECT/SET_NULL 策略承载业务规则（有提交删不掉作业、删题保留提交）
"""
from django.db import IntegrityError
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from academics.models import (AdminClass, Course, CourseCollege, Department,
                              Major, TeachingClass)
from teaching.models import (Homework, HomeworkProblem, JudgeTask, Problem,
                             Submission)
from users.models import User

PASSWORD = 'Test12345'


class OjModelTests(TestCase):
    """OJ 模型基础行为与约束"""

    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(username='T9001', password=PASSWORD)
        cls.student = User.objects.create_user(username='S9001', password=PASSWORD)
        # 最小教学班数据（题目/作业外键依赖）
        department = Department.objects.create(code='D95', name='测试学院')
        major = Major.objects.create(department=department, code='95', name='测试专业')
        adminClass = AdminClass.objects.create(
            name='测试2301', major=major, enrollment_year=2023)
        course = Course.objects.create(code='TEST01', name='测试课程')
        courseCollege = CourseCollege.objects.create(
            course=course, department=department)
        courseCollege.admin_classes.add(adminClass)
        cls.teachingClass = TeachingClass.objects.create(
            name='测试一班', term='2026-2027-1', course_college=courseCollege)
        cls.teachingClass.admin_classes.add(adminClass)

        cls.problem = Problem.objects.create(
            title='A+B 问题',
            statement_md='计算两个整数的和。',
            samples=[{'input': '1 2', 'output': '3'}],
            tests=[{'input': '1 2', 'output': '3', 'points': 100}],
            languages=['c', 'python'],
            owner=cls.teacher,
        )

    def _makeSubmission(self, **kwargs):
        """构造最小合法提交（快照字段按题目真实数据填）"""
        defaults = dict(
            user=self.student, problem=self.problem, language=Problem.LANGUAGE_C,
            code='int main() { return 0; }',
            fingerprint='a' * 64,
            problem_revision=self.problem.revision,
            tests_hash='b' * 64,
            judge_snapshot={
                'tests': self.problem.tests,
                'spj_code': self.problem.spj_code,
                'tier': self.problem.tier,
                'compare_strategy': self.problem.compare_strategy,
            },
        )
        defaults.update(kwargs)
        return Submission.objects.create(**defaults)

    def _makeHomework(self):
        return Homework.objects.create(
            teaching_class=self.teachingClass, title='第一次作业',
            start_at='2026-10-18T08:00:00+08:00',
            deadline='2026-10-25T23:59:59+08:00',
            created_by=self.teacher)

    # ---------- 默认值 ----------

    def test_problemDefaults(self):
        """题目默认值：revision=1、空样例/测试点、无 spj、个人题库、基础档"""
        fresh = Problem.objects.create(title='新题', statement_md='')
        self.assertEqual(fresh.revision, 1)
        self.assertEqual(fresh.samples, [])
        self.assertEqual(fresh.tests, [])
        self.assertFalse(fresh.has_spj)
        self.assertFalse(fresh.is_public)
        self.assertEqual(fresh.tier, Problem.TIER_BASIC)

    def test_submissionDefaults(self):
        """提交默认值：queued 状态、practice 类型、零分（受理起点）"""
        submission = self._makeSubmission()
        self.assertEqual(submission.status, Submission.STATUS_QUEUED)
        self.assertEqual(submission.submit_type, Submission.SUBMIT_PRACTICE)
        self.assertEqual(submission.score, 0)
        self.assertIsNone(submission.judged_at)

    def test_judgeTaskDefaults(self):
        """判题任务默认值：pending 状态、0 进度、0 尝试号（租约起点）"""
        task = JudgeTask.objects.create(submission=self._makeSubmission())
        self.assertEqual(task.status, JudgeTask.STATUS_PENDING)
        self.assertEqual(task.progress, 0)
        self.assertEqual(task.attempt_id, 0)

    # ---------- 约束 ----------

    def test_practiceWithHomeworkRejectedByDb(self):
        """Check 约束：练习提交挂作业 → DB 层拒绝（防非法组合）"""
        homework = self._makeHomework()
        with self.assertRaises(IntegrityError):
            self._makeSubmission(homework=homework)  # submit_type 默认 practice

    def test_homeworkSubmitWithHomeworkOk(self):
        """作业类型提交挂作业 → 合法（约束不误伤）"""
        homework = self._makeHomework()
        submission = self._makeSubmission(
            homework=homework, submit_type=Submission.SUBMIT_HOMEWORK)
        self.assertEqual(submission.submit_type, Submission.SUBMIT_HOMEWORK)

    def test_homeworkProblemUnique(self):
        """同一题目在一份作业中只出现一次（唯一约束）"""
        homework = self._makeHomework()
        HomeworkProblem.objects.create(
            homework=homework, problem=self.problem, order=1,
            problem_revision=self.problem.revision)
        with self.assertRaises(IntegrityError):
            HomeworkProblem.objects.create(
                homework=homework, problem=self.problem, order=2,
                problem_revision=self.problem.revision)

    # ---------- 索引 ----------

    def test_submissionIndexesExist(self):
        """频率检测 (user, created_at) 与重复检测 (problem, user) 索引存在"""
        indexFields = [list(i.fields) for i in Submission._meta.indexes]
        self.assertIn(['user', 'created_at'], indexFields)
        self.assertIn(['problem', 'user'], indexFields)

    def test_judgeTaskStatusIndexExists(self):
        """租约回收扫描依赖 status 索引"""
        indexFields = [list(i.fields) for i in JudgeTask._meta.indexes]
        self.assertIn(['status'], indexFields)

    # ---------- 快照 ----------

    def test_judgeSnapshotRoundTrip(self):
        """judge_snapshot 完整往返：受理复制的判题数据与题目一致"""
        submission = self._makeSubmission()
        snapshot = submission.judge_snapshot
        self.assertEqual(snapshot['tests'], self.problem.tests)
        self.assertEqual(snapshot['tier'], self.problem.tier)
        self.assertEqual(snapshot['compare_strategy'],
                         self.problem.compare_strategy)
        self.assertEqual(snapshot['spj_code'], self.problem.spj_code)

    def test_snapshotIsolatedFromProblemEdit(self):
        """快照独立于题目：改题不影响已受理提交的判题数据（结果可复现）"""
        submission = self._makeSubmission()
        self.problem.tests = [{'input': '9 9', 'output': '18', 'points': 100}]
        self.problem.save()
        # 提交里的快照仍是受理时的旧测试点
        self.assertEqual(
            submission.judge_snapshot['tests'],
            [{'input': '1 2', 'output': '3', 'points': 100}])

    # ---------- 外键行为 ----------

    def test_deleteProblemKeepsSubmission(self):
        """删题后提交保留（SET_NULL），判题时按 7.3 置 system_error"""
        submission = self._makeSubmission()
        self.problem.delete()
        submission.refresh_from_db()
        self.assertIsNone(submission.problem)

    def test_deleteTeacherKeepsProblem(self):
        """删教师账号题目保留（owner SET_NULL）"""
        self.teacher.delete()
        self.problem.refresh_from_db()
        self.assertIsNone(self.problem.owner)

    def test_deleteTeachingClassProtectedByHomework(self):
        """有作业的教学班不可删除（PROTECT，保护历史数据）"""
        self._makeHomework()
        with self.assertRaises(ProtectedError):
            self.teachingClass.delete()

    def test_deleteHomeworkProtectedBySubmission(self):
        """有提交的作业不可删除（PROTECT）"""
        homework = self._makeHomework()
        self._makeSubmission(
            homework=homework, submit_type=Submission.SUBMIT_HOMEWORK)
        with self.assertRaises(ProtectedError):
            homework.delete()

    def test_deleteProblemReferencedByHomeworkProtected(self):
        """被作业引用的题目不可删除（PROTECT）"""
        homework = self._makeHomework()
        HomeworkProblem.objects.create(
            homework=homework, problem=self.problem, order=1,
            problem_revision=self.problem.revision)
        with self.assertRaises(ProtectedError):
            self.problem.delete()
