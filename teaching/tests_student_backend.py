"""学生本人聚合、历史边界、冻结计分和功能撤权的真实接口回归。"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group, Permission
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from academics.models import AcademicTerm
from academics.services import enrollStudent, leaveStudentToday
from academics.term_service import archiveTerm, closeTerm
from teaching.models import (ClassNotice, Homework, HomeworkProblem, HomeworkReminder,
    Problem, Submission, SubmissionReview, TeachingMaterial)
from teaching.tests import ObjectPermissionTestBase
from users.models import SchoolIdentity, StudentProfile, User


class StudentBackendTests(ObjectPermissionTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.teacher.name = '张老师'
        cls.teacher.save(update_fields=['name'])
        cls.term, _ = AcademicTerm.objects.get_or_create(code=cls.myTeachingClass.term,
                                                        defaults={'name': '本学期'})
        cls.problem = Problem.objects.create(title='冻结题名', owner=cls.teacher,
            languages=['python'], tests=[{'input': 'HIDDEN_INPUT', 'output': 'HIDDEN_OUTPUT', 'points': 100}])
        cls.homework = cls.makeHomework(cls.myTeachingClass, '本人作业')
        cls.otherHomework = cls.makeHomework(cls.otherTeachingClass, '其他班作业')
        cls.upcoming = cls.makeHomework(cls.myTeachingClass, '未来作业', upcoming=True)
        cls.notice = ClassNotice.objects.create(teaching_class=cls.myTeachingClass,
            title='本人班通知', content='通知正文', created_by=cls.teacher)
        cls.otherNotice = ClassNotice.objects.create(teaching_class=cls.otherTeachingClass,
            title='其他班通知', content='其他班正文', created_by=cls.teacher2)
        cls.material = TeachingMaterial.objects.create(teaching_class=cls.myTeachingClass,
            title='本人资料', original_name='a.pdf', storage_name='not-opened.pdf', size=1)
        cls.otherMaterial = TeachingMaterial.objects.create(teaching_class=cls.otherTeachingClass,
            title='其他班资料', original_name='b.pdf', storage_name='not-opened-b.pdf', size=1)

    @classmethod
    def makeHomework(cls, tc, title, *, upcoming=False):
        now = timezone.now()
        homework = Homework.objects.create(teaching_class=tc, title=title, created_by=cls.teacher,
            start_at=now + timedelta(days=1) if upcoming else now - timedelta(days=2),
            deadline=now + timedelta(days=3))
        HomeworkProblem.objects.create(homework=homework, problem=cls.problem, order=1, problem_revision=1)
        return homework

    def setUp(self):
        self.client = APIClient()
        self.client.force_login(self.student)

    def submit(self, *, user=None, homework=None, score=80, status=Submission.STATUS_WRONG_ANSWER):
        return Submission.objects.create(user=user or self.student, homework=homework or self.homework,
            problem=self.problem, language='python', code='STUDENT_PRIVATE_CODE', fingerprint='test',
            submit_type=Submission.SUBMIT_HOMEWORK, status=status, score=score, total_score=100,
            problem_revision=1, tests_hash='test', judge_snapshot={'tests': self.problem.tests})

    def finishTerm(self, *, archive=False):
        now = timezone.now()
        Homework.objects.filter(teaching_class__term=self.term.code).update(
            start_at=now - timedelta(days=2), deadline=now - timedelta(days=1))
        closeTerm(self.superUser, self.term.pk)
        if archive:
            archiveTerm(self.superUser, self.term.pk)

    def grades(self):
        return self.client.get(f'/api/student/homeworks/{self.homework.pk}/grades/')

    def test_context_is_self_readonly_and_not_a_class_roster(self):
        data = self.client.get('/api/student/context/').json()
        self.assertEqual(data['user']['id'], self.student.pk)
        self.assertEqual(data['identity']['schoolNumber'], '20239001')
        self.assertEqual(data['student']['major']['id'], self.myProfile.major_id)
        self.assertEqual(data['adminClass']['id'], self.myAdminClass.pk)
        self.assertNotIn('students', str(data))
        self.assertEqual(self.client.patch('/api/student/context/', {'academicStatus': '毕业'},
                                          format='json').status_code, 405)

    def test_context_and_courses_handle_missing_identity_without_inventing_data(self):
        user = User.objects.create_user('no-student-profile')
        user.groups.add(Group.objects.get(name='学生'))
        self.client.force_login(user)
        data = self.client.get('/api/student/context/').json()
        self.assertIsNone(data['identity'])
        self.assertIsNone(data['student'])
        self.assertIsNone(data['adminClass'])
        self.assertEqual(self.client.get('/api/student/teaching-classes/').json()['count'], 0)

    def test_first_student_identity_without_profile_never_falls_back(self):
        self.myProfile.delete()
        second = SchoolIdentity.objects.create(user=self.student, school_code='SECOND',
            school_number='OTHER-SCHOOL', identity_type='student')
        profile = StudentProfile.objects.create(identity=second, major=self.otherProfile.major, enrollment_year=2023)
        enrollStudent(profile, self.otherAdminClass)
        self.assertIsNone(self.client.get('/api/student/context/').json()['student'])
        self.assertEqual(self.client.get('/api/student/homeworks/').json()['count'], 0)

    def test_class_summaries_do_not_contain_other_students(self):
        data = self.client.get('/api/student/teaching-classes/').json()
        self.assertEqual(data['count'], 1)
        row = data['results'][0]
        self.assertEqual(row['id'], self.myTeachingClass.pk)
        self.assertEqual(row['teachers'][0]['name'], '张老师')
        self.assertNotIn('students', row)
        self.assertNotIn('studentCount', row)
        self.assertTrue(row['contentAvailable'])

    def test_new_work_list_and_legacy_array_are_compatible(self):
        data = self.client.get('/api/student/homeworks/').json()
        self.assertEqual(data['count'], 2)
        row = next(row for row in data['results'] if row['id'] == self.homework.pk)
        self.assertEqual(row['courseId'], self.myTeachingClass.course_college.course_id)
        self.assertEqual(row['teacherName'], '张老师')
        self.assertEqual(row['totalScore'], 100)
        self.assertEqual(row['submittedProblemCount'], 0)
        self.assertTrue(row['canSubmit'])
        legacy = self.client.get('/api/homeworks/').json()
        self.assertIsInstance(legacy, list)
        self.assertEqual(len(legacy), 2)
        self.assertEqual(legacy[0]['teacherName'], '张老师')

    def test_pending_homework_has_only_summary_and_no_grade_detail(self):
        data = self.client.get('/api/student/homeworks/?status=pending_start').json()
        self.assertEqual(data['count'], 1)
        row = data['results'][0]
        self.assertFalse(row['contentAvailable'])
        self.assertFalse(row['canSubmit'])
        self.assertNotIn('problems', row)
        self.assertEqual(self.client.get(f'/api/student/homeworks/{self.upcoming.pk}/grades/').status_code, 404)

    def test_homework_filters_use_own_submissions_and_count_distinct_problems(self):
        self.submit()
        self.submit(score=100, status=Submission.STATUS_ACCEPTED)
        data = self.client.get('/api/student/homeworks/?submissionState=submitted&status=ongoing').json()
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['results'][0]['submittedProblemCount'], 1)
        self.assertEqual(data['results'][0]['mySubmitCount'], 2)
        self.assertEqual(self.client.get('/api/student/homeworks/?q=不存在').json()['count'], 0)
        self.assertEqual(self.client.get(f'/api/student/homeworks/?teachingClassId={self.otherTeachingClass.pk}').json()['count'], 0)

    def test_partial_submission_filter_is_separate_from_score(self):
        problem = Problem.objects.create(title='第二题', owner=self.teacher,
            tests=[{'input': '', 'output': '', 'points': 100}])
        HomeworkProblem.objects.create(homework=self.homework, problem=problem, order=2, problem_revision=1)
        self.submit(status=Submission.STATUS_SYSTEM_ERROR, score=99)
        data = self.client.get('/api/student/homeworks/?submissionState=partial').json()
        self.assertEqual(data['count'], 1)
        self.assertIsNone(data['results'][0]['score'])
        self.assertFalse(data['results'][0]['gradingComplete'])

    def test_grades_keep_best_valid_result_and_latest_feedback(self):
        old = self.submit()
        SubmissionReview.objects.create(submission=old, comment='早期批语', reviewed_by=self.teacher)
        best = self.submit(score=100, status=Submission.STATUS_ACCEPTED)
        review = SubmissionReview.objects.create(submission=best, comment='最新批语', reviewed_by=self.teacher)
        pending = self.submit(score=0, status=Submission.STATUS_RUNNING)
        self.submit(user=self.student2, score=1)
        data = self.grades().json()
        self.assertEqual(data['studentId'], self.student.pk)
        self.assertEqual(data['score'], 100)
        self.assertEqual(data['pendingCount'], 1)
        point = data['problems'][0]
        self.assertEqual(point['latestSubmission']['id'], pending.pk)
        self.assertEqual(point['feedbackSubmissionId'], best.pk)
        self.assertEqual(point['teacherReview']['comment'], review.comment)
        for forbidden in ('HIDDEN_INPUT', 'HIDDEN_OUTPUT', 'STUDENT_PRIVATE_CODE', 'judge_snapshot', 'students'):
            self.assertNotIn(forbidden, str(data))

    def test_grade_detail_foreign_and_missing_are_identical_404(self):
        foreign = self.client.get(f'/api/student/homeworks/{self.otherHomework.pk}/grades/')
        missing = self.client.get('/api/student/homeworks/999999/grades/')
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.data, missing.data)

    def test_transfer_keeps_only_owned_history_summary_and_denies_old_content(self):
        submission = self.submit()
        SubmissionReview.objects.create(submission=submission, comment='本人历史反馈', reviewed_by=self.teacher)
        enrollStudent(self.myProfile, self.otherAdminClass)
        data = self.client.get('/api/student/homeworks/?scope=history').json()
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['results'][0]['id'], self.homework.pk)
        self.assertFalse(data['results'][0]['contentAvailable'])
        self.assertFalse(data['results'][0]['canSubmit'])
        self.assertEqual(self.grades().json()['problems'][0]['teacherReview']['comment'], '本人历史反馈')
        for url in (f'/api/homeworks/{self.homework.pk}/',
                    f'/api/problems/{self.problem.pk}/?homeworkId={self.homework.pk}',
                    f'/api/notices/{self.notice.pk}/', f'/api/materials/{self.material.pk}/download/'):
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(f'/api/submissions/{submission.pk}/').status_code, 200)

    def test_non_active_academic_status_cannot_use_stale_current_membership(self):
        self.submit()
        StudentProfile.objects.filter(pk=self.myProfile.pk).update(academic_status='休学')
        self.assertEqual(self.client.get('/api/homeworks/').json(), [])
        self.assertIsNone(self.client.get('/api/student/context/').json()['adminClass'])
        self.assertEqual(self.grades().status_code, 200)
        self.assertFalse(self.grades().json()['canSubmit'])

    def test_archive_uses_frozen_own_grade_for_new_and_legacy_lists(self):
        submission = self.submit()
        self.finishTerm(archive=True)
        Submission.objects.filter(pk=submission.pk).update(score=1)
        self.assertEqual(self.grades().json()['score'], 80)
        self.assertEqual(self.grades().json()['scoreSource'], 'archive')
        self.assertFalse(self.grades().json()['canSubmit'])
        row = next(row for row in self.client.get('/api/student/homeworks/').json()['results']
                   if row['id'] == self.homework.pk)
        self.assertEqual(row['score'], 80)
        legacy = next(row for row in self.client.get('/api/homeworks/').json() if row['id'] == self.homework.pk)
        self.assertEqual(legacy['myBestScore'], 80)
        leaveStudentToday(self.myProfile)
        self.assertEqual(self.grades().json()['score'], 80)

    def test_frozen_roster_student_can_see_own_unsubmitted_archive_after_leaving(self):
        self.finishTerm(archive=True)
        leaveStudentToday(self.myProfile)
        data = self.grades().json()
        self.assertIsNone(data['score'])
        self.assertEqual(data['submissionState'], 'not_submitted')
        self.assertFalse(data['contentAvailable'])

    def test_new_member_cannot_inherit_closed_term_content(self):
        self.finishTerm(archive=True)
        enrollStudent(self.otherProfile, self.myAdminClass)
        self.client.force_login(self.student2)
        for url in (f'/api/homeworks/{self.homework.pk}/',
                    f'/api/student/homeworks/{self.homework.pk}/grades/',
                    f'/api/notices/{self.notice.pk}/',
                    f'/api/teaching-classes/{self.myTeachingClass.pk}/materials/'):
            self.assertEqual(self.client.get(url).status_code, 404)
        rows = self.client.get('/api/student/homeworks/').json()['results']
        self.assertNotIn(self.homework.pk, [row['id'] for row in rows])

    def test_closed_term_without_freeze_record_fails_closed(self):
        AcademicTerm.objects.filter(pk=self.term.pk).update(status='closed', is_current=False)
        self.assertEqual(self.client.get(f'/api/homeworks/{self.homework.pk}/').status_code, 404)

    def test_teacher_renaming_question_does_not_change_own_submission_title(self):
        submission = self.submit()
        Problem.objects.filter(pk=self.problem.pk).update(title='后改题名')
        listed = self.client.get('/api/submissions/').json()['results'][0]
        detail = self.client.get(f'/api/submissions/{submission.pk}/').json()
        self.assertEqual(listed['problemTitle'], '冻结题名')
        self.assertEqual(detail['problemTitle'], '冻结题名')

    def test_notice_aggregation_unread_count_and_current_scope(self):
        response = self.client.get('/api/student/notices/?unread=true').json()
        self.assertEqual((response['count'], response['unreadCount']), (1, 1))
        self.assertEqual(response['results'][0]['id'], self.notice.pk)
        self.client.post(f'/api/notices/{self.notice.pk}/read/', {}, format='json')
        self.assertEqual(self.client.get('/api/student/notices/?unread=true').json()['count'], 0)
        self.assertEqual(self.client.get('/api/student/notices/?unread=false').json()['count'], 1)
        leaveStudentToday(self.myProfile)
        self.assertEqual(self.client.get('/api/student/notices/').json()['count'], 0)

    def test_material_aggregation_never_opens_files_or_returns_paths(self):
        with mock.patch('teaching.material_service.materialStorage') as storage:
            data = self.client.get('/api/student/materials/').json()
            storage.assert_not_called()
        self.assertEqual(data['count'], 1)
        self.assertNotIn('not-opened.pdf', str(data))
        self.assertNotIn('storage_name', str(data))

    def test_reminder_unread_filter_preserves_existing_protocol(self):
        reminder = HomeworkReminder.objects.create(homework=self.homework, recipient=self.student, sent_by=self.teacher)
        self.assertEqual(self.client.get('/api/reminders/?unread=true').json()['unreadCount'], 1)
        self.client.post(f'/api/reminders/{reminder.pk}/read/', {}, format='json')
        self.assertEqual(self.client.get('/api/reminders/?unread=true').json()['count'], 0)
        self.assertEqual(self.client.get('/api/reminders/?unread=false').json()['count'], 1)
        self.assertEqual(self.client.get('/api/reminders/?unread=bad').status_code, 400)

    def test_dashboard_captures_asof_window_and_self_counts(self):
        data = self.client.get('/api/student/dashboard/').json()
        self.assertIn('asOf', data)
        self.assertEqual(data['homeworks']['upcomingCount'], 1)
        self.assertEqual(data['homeworks']['pendingCount'], 1)
        self.assertEqual(data['homeworks']['deadlineWindowHours'], 72)
        self.assertEqual(data['notices']['unreadNoticeCount'], 1)
        self.assertEqual(data['materials']['count'], 1)

    def test_dashboard_does_not_bypass_revoked_feature_permissions(self):
        group = Group.objects.get(name='学生')
        group.permissions.remove(*Permission.objects.filter(codename__in=(
            'homework_view', 'notice_view', 'material_view')))
        data = self.client.get('/api/student/dashboard/').json()
        for field in ('homeworks', 'notices', 'materials'):
            self.assertFalse(data['sections'][field])
            self.assertIsNone(data[field])

    def test_analysis_only_counts_self_and_reports_null_for_no_grades(self):
        empty = self.client.get('/api/student/analysis/').json()
        self.assertIsNone(empty['summary']['averagePercent'])
        self.submit()
        self.submit(user=self.student2, score=100, status=Submission.STATUS_ACCEPTED)
        data = self.client.get('/api/student/analysis/?days=30').json()
        self.assertEqual(data['summary']['homeworkCount'], 1)
        self.assertEqual(data['summary']['averagePercent'], 80)
        self.assertEqual(data['summary']['completionRate'], 1)
        self.assertEqual(sum(row['submissionCount'] for row in data['submissionTrend']), 1)
        self.assertNotIn('students', str(data))
        self.assertNotIn('rank', str(data))

    def test_all_new_routes_require_student_role_and_explicit_feature(self):
        routes = ('context/', 'teaching-classes/', 'homeworks/', 'notices/', 'materials/', 'dashboard/', 'analysis/')
        for user in (self.teacher, self.courseTeam):
            self.client.force_login(user)
            for path in routes:
                self.assertEqual(self.client.get('/api/student/' + path).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get('/api/student/context/').status_code, 403)

    def test_new_routes_reject_forged_identity_and_invalid_filters(self):
        for url in ('/api/student/homeworks/?studentId=2', '/api/student/context/?userId=2',
                    '/api/student/homeworks/?status=bad', '/api/student/homeworks/?scope=bad',
                    '/api/student/homeworks/?submissionState=bad', '/api/student/homeworks/?courseId=true',
                    '/api/student/homeworks/?term=', '/api/student/homeworks/?q=' + 'x' * 101,
                    '/api/student/homeworks/?courseId=1&courseId=2',
                    '/api/student/analysis/?days=367', '/api/student/analysis/?days=0',
                    '/api/student/notices/?unread=bad'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 400)

    def test_homework_pagination_has_bounded_query_count(self):
        with CaptureQueriesContext(connection) as small:
            self.client.get('/api/student/homeworks/')
        for index in range(24):
            self.makeHomework(self.myTeachingClass, f'分页作业{index}')
        with CaptureQueriesContext(connection) as large:
            data = self.client.get('/api/student/homeworks/').json()
        self.assertEqual(data['count'], 26)
        self.assertEqual(len(data['results']), 20)
        self.assertTrue(data['next'])
        self.assertLessEqual(len(large), len(small) + 2)
        self.assertEqual(len(self.client.get('/api/student/homeworks/?page=2').json()['results']), 6)
