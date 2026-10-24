"""页面与 API 权限一致性、转班和角色撤销后的访问边界。"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from academics.models import (AdminClass, Course, CourseCollege, Department,
                              Major, TeachingClass)
from academics.services import enrollStudent
from teaching.models import Homework, HomeworkProblem, JudgeTask, Problem, Submission
from users.models import SchoolIdentity, StudentProfile, User


class PermissionLifecycleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(username='lifecycle-teacher')
        cls.teacher.groups.add(Group.objects.get(name='教师'))
        cls.student = User.objects.create_user(username='lifecycle-student')
        cls.student.groups.add(Group.objects.get(name='学生'))
        department = Department.objects.create(code='LIFE', name='权限学院')
        major = Major.objects.create(department=department, code='L', name='专业')
        cls.a = AdminClass.objects.create(name='A', major=major)
        cls.b = AdminClass.objects.create(name='B', major=major)
        identity = SchoolIdentity.objects.create(user=cls.student, school_number='LIFE1',
                                                identity_type='student')
        cls.profile = StudentProfile.objects.create(identity=identity, major=major,
                                                    enrollment_year=2026)
        enrollStudent(cls.profile, cls.a)
        course = Course.objects.create(code='LIFE', name='权限课程')
        cc = CourseCollege.objects.create(course=course, department=department)
        cc.admin_classes.add(cls.a, cls.b)
        cls.tcA = TeachingClass.objects.create(name='A班', term='T1', course_college=cc)
        cls.tcB = TeachingClass.objects.create(name='B班', term='T1', course_college=cc)
        cls.tcA.admin_classes.add(cls.a)
        cls.tcB.admin_classes.add(cls.b)
        cls.tcA.teachers.add(cls.teacher)
        cls.problem = Problem.objects.create(owner=cls.teacher, title='教师个人题',
            statement_md='题面', languages=['c'],
            tests=[{'input': '1', 'output': '1', 'points': 100}])
        cls.unassigned = Problem.objects.create(owner=cls.teacher, title='未布置的个人题')
        cls.hwA = cls.makeHomework(cls.tcA, 'A作业')
        cls.hwB = cls.makeHomework(cls.tcB, 'B作业')

    @classmethod
    def makeHomework(cls, teachingClass, title):
        homework = Homework.objects.create(teaching_class=teachingClass, title=title,
            start_at=timezone.now() - timedelta(hours=1),
            deadline=timezone.now() + timedelta(days=1), created_by=cls.teacher)
        HomeworkProblem.objects.create(homework=homework, problem=cls.problem, problem_revision=1)
        return homework

    def setUp(self):
        self.client = APIClient()
        self.client.force_login(self.student)

    def submit(self, homework=None, problemId=None):
        return self.client.post('/api/submissions/',
            {'homeworkId': (homework or self.hwA).pk,
             'problemId': self.problem.pk if problemId is None else problemId,
             'submitType': 'homework', 'language': 'c', 'code': 'int main(){return 0;}'},
            format='json')

    def assertHomeworkAccess(self, homework, status):
        urls = [f'/student/homeworks/{homework.pk}/',
                f'/api/homeworks/{homework.pk}/',
                f'/api/problems/{self.problem.pk}/?homeworkId={homework.pk}']
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, status)

    def test_student_page_matches_object_and_time_checks(self):
        self.assertHomeworkAccess(self.hwA, 200)
        self.assertHomeworkAccess(self.hwB, 404)
        self.assertEqual(self.client.get('/student/homeworks/999999/').status_code, 404)
        Homework.objects.filter(pk=self.hwA.pk).update(start_at=timezone.now() + timedelta(hours=1))
        self.assertHomeworkAccess(self.hwA, 404)

    def test_student_page_matches_feature_permission_check(self):
        Group.objects.get(name='学生').permissions.remove(
            Permission.objects.get(content_type__app_label='users', codename='homework_view'))
        self.assertHomeworkAccess(self.hwA, 403)

    def test_teacher_bank_page_matches_feature_permission_check(self):
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get('/teacher/question-bank/').status_code, 200)
        Group.objects.get(name='教师').permissions.remove(
            Permission.objects.get(content_type__app_label='users', codename='homework_bank'))
        self.assertEqual(self.client.get('/teacher/question-bank/').status_code, 403)
        self.assertEqual(self.client.get('/api/teacher/problems/').status_code, 403)

    @patch('teaching.submission_service.async_task', return_value='lifecycle-judge-task')
    def test_transfer_revokes_old_access_and_preserves_own_history(self, dispatch):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.submit()
        self.assertEqual(response.status_code, 200)
        submissionId = response.json()['submissionId']
        dispatch.reset_mock()
        enrollStudent(self.profile, self.b)
        self.assertHomeworkAccess(self.hwA, 404)
        self.assertHomeworkAccess(self.hwB, 200)
        listed = self.client.get('/api/homeworks/').json()
        self.assertEqual([item['id'] for item in listed], [self.hwB.pk])
        self.assertEqual(self.submit().status_code, 404)
        self.assertEqual(Submission.objects.count(), 1)
        dispatch.assert_not_called()
        self.assertEqual(self.client.get(f'/api/submissions/{submissionId}/').status_code, 200)
        self.assertEqual(self.client.get('/api/submissions/').json()['count'], 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.submit(self.hwB).status_code, 200)
        dispatch.assert_called_once()

    @patch('teaching.submission_service.async_task')
    def test_leaving_class_revokes_access_without_creating_judge_tasks(self, dispatch):
        self.profile.memberships.filter(left_at__isnull=True).update(left_at=timezone.localdate())
        self.assertHomeworkAccess(self.hwA, 404)
        self.assertEqual(self.client.get('/api/homeworks/').json(), [])
        self.assertEqual(self.submit().status_code, 404)
        self.assertFalse(Submission.objects.exists())
        self.assertFalse(JudgeTask.objects.exists())
        dispatch.assert_not_called()

    def test_removing_teacher_assignment_revokes_class_operations(self):
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get(f'/api/teacher/homeworks/{self.hwA.pk}/').status_code, 200)
        self.tcA.teachers.remove(self.teacher)
        urls = [f'/teacher/teaching-classes/{self.tcA.pk}/',
                f'/api/teacher/teaching-classes/{self.tcA.pk}/',
                f'/api/teacher/teaching-classes/{self.tcA.pk}/homeworks/',
                f'/api/teacher/homeworks/{self.hwA.pk}/']
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.put(f'/api/teacher/homeworks/{self.hwA.pk}/',
                                        {}, format='json').status_code, 404)
        self.assertEqual(self.client.delete(f'/api/teacher/homeworks/{self.hwA.pk}/').status_code, 404)
        self.assertEqual(self.client.post(
            f'/api/teacher/teaching-classes/{self.tcA.pk}/homeworks/', {}, format='json').status_code, 404)
        self.assertTrue(Homework.objects.filter(pk=self.hwA.pk).exists())
        self.assertEqual(self.client.get(f'/api/teacher/problems/{self.problem.pk}/').status_code, 200)

    def test_role_revocation_takes_effect_in_existing_sessions(self):
        self.student.groups.remove(Group.objects.get(name='学生'))
        self.assertHomeworkAccess(self.hwA, 403)
        self.assertEqual(self.submit().status_code, 403)
        self.client.force_login(self.teacher)
        self.teacher.groups.remove(Group.objects.get(name='教师'))
        for url in ('/teacher/', '/teacher/question-bank/', '/api/teacher/teaching-classes/',
                    '/api/teacher/problems/', f'/api/teacher/homeworks/{self.hwA.pk}/'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    @patch('teaching.submission_service.async_task')
    def test_submission_does_not_reveal_unassigned_problem_existence(self, dispatch):
        existing = self.submit(problemId=self.unassigned.pk)
        missing = self.submit(problemId=999999)
        self.assertEqual(existing.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(existing.json(), missing.json())
        self.assertFalse(Submission.objects.exists())
        dispatch.assert_not_called()

    @patch('teaching.submission_service.async_task')
    def test_foreign_homework_does_not_reveal_problem_existence(self, dispatch):
        existing = self.submit(self.hwB)
        missing = self.submit(self.hwB, problemId=999999)
        self.assertEqual(existing.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(existing.json(), missing.json())
        self.assertFalse(Submission.objects.exists())
        dispatch.assert_not_called()
