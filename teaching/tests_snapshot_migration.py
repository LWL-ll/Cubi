"""实际执行快照升级，验证存量回填及历史提交保持不变。"""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from academics.models import Course, CourseCollege, Department, TeachingClass
from users.models import User
from .models import Homework, HomeworkProblem, Problem, Submission


class SnapshotMigrationTests(TransactionTestCase):
    def test_backfill_current_problem_without_rewriting_submissions(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [node for node in latest if node[0] != 'teaching'] + [
            ('teaching', '0003_problem_source_code_problem_source_hash')]
        try:
            executor.migrate(before)
            oldApps = executor.loader.project_state(before).apps
            user = User.objects.create_user(username='migration-student')
            department = Department.objects.create(code='MIG', name='学院')
            course = Course.objects.create(code='MIG', name='课程')
            cc = CourseCollege.objects.create(course=course, department=department)
            tc = TeachingClass.objects.create(name='班级', term='T1', course_college=cc)
            homework = Homework.objects.create(teaching_class=tc, title='历史作业',
                                                start_at=timezone.now(), deadline=timezone.now())
            problem = Problem.objects.create(
                title='当前题面', statement_md='当前正文', revision=5,
                languages=['python'], samples=[{'input': 'a', 'output': 'b'}],
                tests=[{'input': 'secret', 'output': 'expected', 'points': 100}],
                tier='advanced', compare_strategy='loose', has_spj=True, spj_code='pass')
            item = oldApps.get_model('teaching', 'HomeworkProblem').objects.create(
                homework_id=homework.pk, problem_id=problem.pk, order=0, problem_revision=1)
            original = {'tests': [{'input': 'old', 'output': 'old', 'points': 50}],
                        'spj_code': '', 'tier': 'basic', 'compare_strategy': 'exact'}
            submission = Submission.objects.create(
                user=user, problem=problem, homework=homework, submit_type='homework',
                language='c', code='old code', fingerprint='f' * 64,
                tests_hash='h' * 64, problem_revision=1, judge_snapshot=original)
            MigrationExecutor(connection).migrate(latest)
            frozen = HomeworkProblem.objects.get(pk=item.pk)
            self.assertEqual(frozen.problem_revision, 5)
            for field in ('title', 'statement_md', 'samples', 'languages', 'tests',
                          'tier', 'compare_strategy', 'has_spj', 'spj_code'):
                self.assertEqual(frozen.problem_snapshot[field], getattr(problem, field))
            submission.refresh_from_db()
            self.assertEqual(submission.problem_revision, 1)
            self.assertEqual(submission.judge_snapshot, original)
            self.assertEqual(submission.tests_hash, 'h' * 64)
        finally:
            MigrationExecutor(connection).migrate(latest)
