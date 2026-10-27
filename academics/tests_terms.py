"""学期关闭、归档与各既有写入口的业务回归。"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from academics.audit_service import recordAudit
from academics.models import AcademicTerm, AuditEvent, TeachingClassRecord
from academics.services import leaveStudentToday
from academics.term_service import archiveTerm, closeTerm
from teaching.models import (ClassNotice, Homework, HomeworkProblem, Problem, Submission,
                             SubmissionReview, TeachingMaterial)
from teaching.teacher_service import classDashboard, homeworkGradebook
from teaching.tests import ObjectPermissionTestBase


class TermLifecycleTests(ObjectPermissionTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.term, _ = AcademicTerm.objects.get_or_create(code=cls.myTeachingClass.term,
                                                        defaults={'name': '测试学期'})
        cls.problem = Problem.objects.create(title='结课题', owner=cls.teacher,
            languages=['python'], tests=[{'input': '', 'output': '1', 'points': 100}])
        cls.homework = Homework.objects.create(teaching_class=cls.myTeachingClass, title='结课作业',
            created_by=cls.teacher, start_at=timezone.now() - timedelta(days=2),
            deadline=timezone.now() - timedelta(days=1))
        HomeworkProblem.objects.create(homework=cls.homework, problem=cls.problem,
                                        order=1, problem_revision=1)
        cls.notice = ClassNotice.objects.create(teaching_class=cls.myTeachingClass,
            title='结课通知', content='正文', created_by=cls.teacher)
        cls.material = TeachingMaterial.objects.create(teaching_class=cls.myTeachingClass,
            title='讲义', original_name='a.pdf', storage_name='absent.pdf', size=1,
            uploaded_by=cls.teacher)

    def setUp(self):
        self.client = APIClient()
        self.client.force_login(self.superUser)
        self.termUrl = f'/api/office/terms/{self.term.pk}'

    def submission(self, status=Submission.STATUS_ACCEPTED, score=80):
        return Submission.objects.create(user=self.student, homework=self.homework,
            problem=self.problem, language='python', code='print(1)', fingerprint='x',
            submit_type=Submission.SUBMIT_HOMEWORK,
            status=status, score=score or 0, total_score=100, problem_revision=1, tests_hash='x',
            judge_snapshot={'tests': self.problem.tests})

    def close(self):
        return closeTerm(self.superUser, self.term.pk)

    def archive(self):
        self.close()
        return archiveTerm(self.superUser, self.term.pk)

    def test_only_office_and_superuser_can_manage_terms(self):
        for actor in (self.teacher, self.student, self.courseTeam):
            self.client.force_login(actor)
            self.assertEqual(self.client.get('/api/office/terms/').status_code, 403)
            self.assertEqual(self.client.post(self.termUrl + '/close/', {}, format='json').status_code, 403)

    def test_create_dates_current_switch_and_unique_code(self):
        response = self.client.post('/api/office/terms/', {
            'code': '2027-2028-1', 'name': '新学期', 'startDate': '2027-09-01',
            'endDate': '2028-01-20'}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        url = f"/api/office/terms/{response.data['id']}/current/"
        self.assertEqual(self.client.post(url, {}, format='json').status_code, 200)
        self.assertEqual(AcademicTerm.objects.filter(is_current=True).get().code, '2027-2028-1')
        self.assertEqual(self.client.post('/api/office/terms/', {
            'code': '2027-2028-1', 'name': '重复', 'startDate': '2027-01-01',
            'endDate': '2027-01-02'}, format='json').status_code, 400)

    def test_dates_and_server_owned_fields_are_validated(self):
        for fields in ({'startDate': '2027-01-01'}, {'startDate': 'x', 'endDate': '2027-01-02'},
                       {'startDate': '2027-02-01', 'endDate': '2027-01-02'},
                       {'status': 'archived'}, {'code': '换编号'}):
            with self.subTest(fields=fields):
                self.assertEqual(self.client.patch(self.termUrl + '/', fields, format='json').status_code, 400)
        self.assertEqual(self.client.patch(self.termUrl + '/', b'{"name":"\\ud800"}',
                                           content_type='application/json').status_code, 400)

    def test_current_term_is_unique_and_must_be_open(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            AcademicTerm.objects.create(code='another', name='另一个', is_current=True)
        self.close()
        self.assertEqual(self.client.post(self.termUrl + '/current/', {}, format='json').status_code, 400)
        self.assertFalse(AcademicTerm.objects.get(pk=self.term.pk).is_current)

    def test_unfinished_homework_blocks_close_without_partial_snapshot(self):
        Homework.objects.filter(pk=self.homework.pk).update(deadline=timezone.now() + timedelta(days=1))
        response = self.client.post(self.termUrl + '/close/', {}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(TeachingClassRecord.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action='term.close').exists())

    def test_close_freezes_roster_before_graduation_or_transfer(self):
        self.submission()
        self.close()
        leaveStudentToday(self.myProfile)
        self.student.name = '改名'
        self.student.save(update_fields=['name'])
        book = homeworkGradebook(self.homework)
        self.assertEqual(book['summary']['studentCount'], 1)
        self.assertEqual(book['students'][0]['name'], '')
        self.assertEqual(book['students'][0]['score'], 80)
        self.assertEqual(classDashboard(self.myTeachingClass)['studentCount'], 1)

    def test_departed_students_with_submissions_are_preserved_at_close(self):
        self.submission()
        leaveStudentToday(self.myProfile)
        self.close()
        self.assertEqual(homeworkGradebook(self.homework)['students'][0]['id'], self.student.pk)

    def test_close_is_idempotent_and_archive_requires_close(self):
        self.assertEqual(self.client.post(self.termUrl + '/archive/', {}, format='json').status_code, 400)
        self.close()
        self.close()
        self.assertEqual(TeachingClassRecord.objects.count(), 2)
        self.assertEqual(AuditEvent.objects.filter(action='term.close').count(), 1)

    def test_pending_judging_blocks_archive_and_can_finish_after_close(self):
        submission = self.submission(status=Submission.STATUS_RUNNING, score=None)
        self.close()
        self.assertEqual(self.client.post(self.termUrl + '/archive/', {}, format='json').status_code, 400)
        Submission.objects.filter(pk=submission.pk).update(status=Submission.STATUS_ACCEPTED, score=100)
        self.assertEqual(self.client.post(self.termUrl + '/archive/', {}, format='json').status_code, 200)
        self.assertEqual(homeworkGradebook(self.homework)['students'][0]['score'], 100)

    def test_archive_freezes_gradebook_and_retains_raw_submission(self):
        submission = self.submission()
        self.archive()
        Submission.objects.filter(pk=submission.pk).update(score=1)
        leaveStudentToday(self.myProfile)
        self.assertEqual(homeworkGradebook(self.homework)['students'][0]['score'], 80)
        self.assertEqual(classDashboard(self.myTeachingClass)['homeworks'][0]['averageScore'], 80)
        self.assertTrue(Submission.objects.filter(pk=submission.pk).exists())
        self.assertEqual(self.client.post(self.termUrl + '/archive/', {}, format='json').status_code, 200)

    def test_closed_allows_review_and_answer_then_archived_blocks_both(self):
        submission = self.submission()
        self.close()
        self.client.force_login(self.teacher)
        reviewUrl = f'/api/teacher/submissions/{submission.pk}/review/'
        answerUrl = f'/api/teacher/homeworks/{self.homework.pk}/answers/{self.problem.pk}/'
        self.assertEqual(self.client.put(reviewUrl, {'comment': '评阅收尾'}, format='json').status_code, 200)
        self.assertEqual(self.client.put(answerUrl, {'content': '参考解答'}, format='json').status_code, 200)
        self.assertEqual(self.client.post(f'/api/teacher/homeworks/{self.homework.pk}/answers/publish/',
                                          {}, format='json').status_code, 200)
        archiveTerm(self.superUser, self.term.pk)
        self.assertEqual(self.client.put(reviewUrl, {'comment': '篡改'}, format='json').status_code, 400)
        self.assertEqual(self.client.delete(reviewUrl).status_code, 400)
        self.assertEqual(self.client.delete(answerUrl).status_code, 400)
        self.assertEqual(SubmissionReview.objects.get(submission=submission).comment, '评阅收尾')

    def test_closed_blocks_existing_homework_notice_material_and_reminder_writes(self):
        self.close()
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.post(
            f'/api/teacher/teaching-classes/{self.myTeachingClass.pk}/homeworks/', {
                'title': '新作业', 'startAt': timezone.now().isoformat(),
                'deadline': (timezone.now() + timedelta(days=1)).isoformat(),
                'problemIds': [self.problem.pk]}, format='json').status_code, 400)
        self.assertEqual(self.client.delete(f'/api/teacher/homeworks/{self.homework.pk}/').status_code, 400)
        self.assertEqual(self.client.patch(f'/api/teacher/notices/{self.notice.pk}/',
                                           {'title': '改通知'}, format='json').status_code, 400)
        self.assertEqual(self.client.delete(f'/api/teacher/notices/{self.notice.pk}/').status_code, 400)
        self.assertEqual(self.client.patch(f'/api/teacher/materials/{self.material.pk}/',
                                           {'title': '改资料'}, format='json').status_code, 400)
        self.assertEqual(self.client.delete(f'/api/teacher/materials/{self.material.pk}/').status_code, 400)
        self.assertEqual(self.client.post(f'/api/teacher/homeworks/{self.homework.pk}/reminders/',
                                          {}, format='json').status_code, 400)

    def test_closed_blocks_composition_and_teacher_changes_including_reverse_m2m(self):
        self.close()
        response = self.client.post(f'/api/teaching-classes/{self.myTeachingClass.pk}/',
            {'action': 'removeTeacher', 'teacherId': self.teacher.pk}, format='json')
        self.assertEqual(response.status_code, 400)
        for operation in (lambda: self.myTeachingClass.teachers.clear(),
                          lambda: self.teacher.teaching_classes.clear(),
                          lambda: self.myTeachingClass.admin_classes.clear(),
                          lambda: self.myTeachingClass.save()):
            with self.assertRaises(ValidationError), transaction.atomic():
                operation()

    def test_closed_blocks_new_submission_before_queue_dispatch(self):
        self.close()
        self.client.force_login(self.student)
        response = self.client.post('/api/submissions/', {'homeworkId': self.homework.pk,
            'problemId': self.problem.pk, 'code': 'print(1)', 'language': 'python'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Submission.objects.exists())

    def test_archive_retains_teacher_read_access_and_class_roster_contract(self):
        self.archive()
        leaveStudentToday(self.myProfile)
        self.client.force_login(self.teacher)
        response = self.client.get(f'/api/teacher/teaching-classes/{self.myTeachingClass.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['studentCount'], 1)
        listing = self.client.get('/api/teacher/teaching-classes/').data
        self.assertEqual(next(row for row in listing if row['id'] == self.myTeachingClass.pk)['studentCount'], 1)
        self.client.force_login(self.teacher2)
        self.assertEqual(self.client.get(f'/api/teacher/teaching-classes/{self.myTeachingClass.pk}/').status_code, 404)

    def test_unknown_term_cannot_be_used_via_public_office_api(self):
        response = self.client.post('/api/teaching-classes/', {
            'courseCollegeId': self.myTeachingClass.course_college_id, 'name': '新班',
            'term': 'unknown', 'adminClassIds': [self.myAdminClass.pk]}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_audit_redaction_and_transaction_rollback(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            recordAudit(self.superUser, 'should.rollback', self.student, after={'name': 'x'})
            raise RuntimeError('rollback')
        self.assertFalse(AuditEvent.objects.filter(action='should.rollback').exists())
        event = recordAudit(self.superUser, 'safe', self.student, after={
            'name': '保留', 'password': 'SECRET', 'nested': {'token': 'SECRET', 'id': 1}})
        self.assertEqual(event.after, {'name': '保留', 'nested': {'id': 1}})

    def test_legacy_office_assignment_audits_before_after_and_rejects_without_event(self):
        url = f'/api/teaching-classes/{self.myTeachingClass.pk}/'
        response = self.client.post(url, {'action': 'removeTeacher', 'teacherId': self.teacher.pk}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        event = AuditEvent.objects.get()
        self.assertIn(self.teacher.pk, event.before['teachersIds'])
        self.assertNotIn(self.teacher.pk, event.after['teachersIds'])
        self.client.force_login(self.student)
        self.assertEqual(self.client.post(url, {'action': 'removeTeacher',
                                              'teacherId': self.teacher2.pk}, format='json').status_code, 403)
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_audit_query_permissions_filters_and_pagination(self):
        self.close()
        response = self.client.get('/api/office/audit-events/?action=term.close')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(self.client.get('/api/office/audit-events/?since=no').status_code, 400)
        self.assertEqual(self.client.delete('/api/office/audit-events/').status_code, 405)
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get('/api/office/audit-events/').status_code, 403)
