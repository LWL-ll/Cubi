"""作业站内提醒：未交齐判定、一小时去重和接收者当前班级权限。"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group
from django.http import Http404
from django.utils import timezone
from rest_framework.test import APIClient

from teaching.communication_service import markReminderRead, sendHomeworkReminders
from teaching import communication_service
from teaching.models import Homework, HomeworkProblem, HomeworkReminder, Problem, Submission
from teaching.tests import ObjectPermissionTestBase
from users.models import User


class HomeworkReminderTests(ObjectPermissionTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.problem = Problem.objects.create(title='题一', owner=cls.teacher)
        cls.problem2 = Problem.objects.create(title='题二', owner=cls.teacher)
        cls.homework = Homework.objects.create(
            teaching_class=cls.myTeachingClass, title='两题作业', created_by=cls.teacher,
            start_at=timezone.now() - timedelta(hours=3),
            deadline=timezone.now() + timedelta(hours=3))
        for order, problem in enumerate((cls.problem, cls.problem2)):
            HomeworkProblem.objects.create(homework=cls.homework, problem=problem,
                                            order=order, problem_revision=1)
        cls.completed = User.objects.create_user('S9013', name='已交齐学生')
        cls.completed.groups.add(Group.objects.get(name='学生'))
        cls._makeStudent(cls.completed, '20239003', cls.myProfile.major, cls.myAdminClass)

    def setUp(self):
        self.client = APIClient()
        self.client.force_login(self.teacher)
        self.url = f'/api/teacher/homeworks/{self.homework.pk}/reminders/'

    def submit(self, user=None, problem=None, status=Submission.STATUS_QUEUED):
        return Submission.objects.create(
            homework=self.homework, user=user or self.student, problem=problem or self.problem,
            submit_type=Submission.SUBMIT_HOMEWORK, language='python', code='x',
            fingerprint='test', status=status, score=0, total_score=100,
            problem_revision=1, tests_hash='test', judge_snapshot={})

    def test_only_current_students_missing_a_problem_receive_reminders(self):
        self.submit()
        self.submit(user=self.completed, status=Submission.STATUS_SYSTEM_ERROR)
        self.submit(user=self.completed, problem=self.problem2, status=Submission.STATUS_WRONG_ANSWER)
        self.submit(user=self.student2)
        preview = self.client.get(self.url)
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data, {
            'homeworkId': self.homework.pk, 'eligibleCount': 1, 'readyCount': 1, 'skippedCount': 0})
        response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['sentCount'], 1)
        reminder = HomeworkReminder.objects.get()
        self.assertEqual(reminder.recipient, self.student)
        self.assertEqual(reminder.sent_by, self.teacher)
        self.assertIsNone(reminder.read_at)

    def test_one_hour_dedup_window_includes_co_teacher_and_exact_boundary(self):
        now = timezone.now()
        with mock.patch('teaching.communication_service.timezone.now', return_value=now):
            first = self.client.post(self.url, {}, format='json')
        self.assertEqual(first.data['sentCount'], 2)
        self.myTeachingClass.teachers.add(self.teacher2)
        self.client.force_login(self.teacher2)
        with mock.patch('teaching.communication_service.timezone.now',
                        return_value=now + timedelta(minutes=59, seconds=59)):
            preview = self.client.get(self.url).data
            repeated = self.client.post(self.url, {}, format='json').data
        self.assertEqual(preview['readyCount'], 0)
        self.assertEqual(preview['skippedCount'], 2)
        self.assertEqual(repeated['sentCount'], 0)
        self.assertEqual(repeated['skippedCount'], 2)
        with mock.patch('teaching.communication_service.timezone.now',
                        return_value=now + timedelta(hours=1)):
            later = self.client.post(self.url, {}, format='json').data
        self.assertEqual(later['sentCount'], 2)
        self.assertEqual(HomeworkReminder.objects.count(), 4)
        self.assertEqual(HomeworkReminder.objects.filter(sent_by=self.teacher2).count(), 2)

    def test_filling_last_problem_removes_student_from_next_reminder_batch(self):
        self.client.post(self.url, {}, format='json')
        HomeworkReminder.objects.update(created_at=timezone.now() - timedelta(hours=2))
        self.submit()
        self.submit(problem=self.problem2)
        response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.data['eligibleCount'], 1)
        self.assertEqual(response.data['sentCount'], 1)
        self.assertEqual(HomeworkReminder.objects.filter(recipient=self.student).count(), 1)

    def test_student_only_lists_own_reminders_and_marks_read_idempotently(self):
        self.client.post(self.url, {}, format='json')
        self.client.force_login(self.student)
        listing = self.client.get('/api/reminders/')
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertEqual(listing.data['count'], 1)
        reminderId = listing.data['results'][0]['id']
        self.assertIsNone(listing.data['results'][0]['readAt'])
        url = f'/api/reminders/{reminderId}/read/'
        self.assertEqual(self.client.post(url, {}, format='json').status_code, 200)
        first = HomeworkReminder.objects.get(pk=reminderId).read_at
        self.assertIsNotNone(first)
        self.assertEqual(self.client.post(url, {}, format='json').status_code, 200)
        self.assertEqual(HomeworkReminder.objects.get(pk=reminderId).read_at, first)
        self.client.force_login(self.completed)
        foreign = self.client.post(url, {}, format='json')
        missing = self.client.post('/api/reminders/99999999/read/', {}, format='json')
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.data, missing.data)

    def test_departed_students_do_not_receive_or_access_reminders(self):
        self.client.post(self.url, {}, format='json')
        reminderId = HomeworkReminder.objects.get(recipient=self.student).pk
        member = self.myProfile.memberships.get(left_at__isnull=True)
        member.left_at = timezone.now().date()
        member.save()
        self.assertEqual(self.client.get(self.url).data['eligibleCount'], 1)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get('/api/reminders/').data['count'], 0)
        self.assertEqual(self.client.post(
            f'/api/reminders/{reminderId}/read/', {}, format='json').status_code, 404)

    def test_mark_read_rechecks_recipient_and_current_membership_for_stale_object(self):
        reminder = HomeworkReminder.objects.create(
            homework=self.homework, recipient=self.student, sent_by=self.teacher)
        with self.assertRaises(Http404):
            markReminderRead(self.completed, reminder)
        self.assertIsNone(HomeworkReminder.objects.get(pk=reminder.pk).read_at)
        member = self.myProfile.memberships.get(left_at__isnull=True)
        member.left_at = timezone.now().date()
        member.save()
        with self.assertRaises(Http404):
            markReminderRead(self.student, reminder)
        self.assertIsNone(HomeworkReminder.objects.get(pk=reminder.pk).read_at)

    def test_reminder_endpoints_respect_function_and_class_permissions(self):
        self.client.force_login(self.teacher2)
        foreign = self.client.post(self.url, {}, format='json')
        missing = self.client.post('/api/teacher/homeworks/99999999/reminders/', {}, format='json')
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.data, missing.data)
        for user in (self.student, self.courseTeam):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.client.post(self.url, {}, format='json').status_code, 403)
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get('/api/reminders/').status_code, 403)
        self.assertFalse(HomeworkReminder.objects.exists())

    def test_payload_cannot_choose_recipient_sender_or_message(self):
        for payload in ([], {'recipientId': self.student2.pk},
                        {'sentById': self.teacher2.pk}, {'content': '自定义消息'}):
            self.assertEqual(self.client.post(self.url, payload, format='json').status_code, 400)
        self.assertFalse(HomeworkReminder.objects.exists())
        self.client.post(self.url, {}, format='json')
        reminderId = HomeworkReminder.objects.get(recipient=self.student).pk
        self.client.force_login(self.student)
        url = f'/api/reminders/{reminderId}/read/'
        for payload in ([], {'recipientId': self.completed.pk}, {'readAt': '2020-01-01'}):
            self.assertEqual(self.client.post(url, payload, format='json').status_code, 400)
        self.assertIsNone(HomeworkReminder.objects.get(pk=reminderId).read_at)

    def test_only_ongoing_homework_allows_preview_and_send(self):
        now = timezone.now()
        for start, deadline in ((now + timedelta(hours=1), now + timedelta(hours=2)),
                                (now - timedelta(hours=2), now - timedelta(hours=1))):
            Homework.objects.filter(pk=self.homework.pk).update(start_at=start, deadline=deadline)
            self.assertEqual(self.client.get(self.url).status_code, 400)
            self.assertEqual(self.client.post(self.url, {}, format='json').status_code, 400)
        self.assertFalse(HomeworkReminder.objects.exists())

    def test_start_and_deadline_boundaries_are_inclusive(self):
        for now in (self.homework.start_at, self.homework.deadline):
            with mock.patch('teaching.communication_service.timezone.now', return_value=now):
                # 截止边界在三小时后，须在模拟时间登录以避免两小时会话先过期。
                self.client.force_login(self.teacher)
                self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_reminders_for_different_homeworks_have_separate_windows(self):
        self.client.post(self.url, {}, format='json')
        other = Homework.objects.create(
            teaching_class=self.myTeachingClass, title='另一作业', created_by=self.teacher,
            start_at=self.homework.start_at, deadline=self.homework.deadline)
        HomeworkProblem.objects.create(homework=other, problem=self.problem,
                                        order=0, problem_revision=1)
        response = self.client.post(
            f'/api/teacher/homeworks/{other.pk}/reminders/', {}, format='json')
        self.assertEqual(response.data['sentCount'], 2)
        self.assertEqual(HomeworkReminder.objects.count(), 4)

    def test_send_reloads_homework_after_lock_instead_of_using_stale_times(self):
        Homework.objects.filter(pk=self.homework.pk).update(
            deadline=timezone.now() - timedelta(seconds=1))
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            sendHomeworkReminders(self.teacher, self.homework, {})
        self.assertFalse(HomeworkReminder.objects.exists())

    def test_stale_homework_does_not_authorize_removed_teacher(self):
        self.myTeachingClass.teachers.remove(self.teacher)
        with self.assertRaises(Http404):
            sendHomeworkReminders(self.teacher, self.homework, {})
        self.assertFalse(HomeworkReminder.objects.exists())

    def test_recipients_and_cooldown_are_computed_only_after_homework_lock(self):
        realLock = communication_service.lockHomework
        realRecipients = communication_service.reminderRecipients
        calls = []

        def lock(homeworkId):
            homework = realLock(homeworkId)
            calls.append('lock')
            # 模拟本次发送等待锁时，前一事务已交齐作业并发送了另一学生的提醒。
            self.submit()
            self.submit(problem=self.problem2)
            HomeworkReminder.objects.create(homework=homework, recipient=self.completed,
                                             sent_by=self.teacher2)
            return homework

        def recipients(homework, now):
            self.assertEqual(calls, ['lock'])
            calls.append('recipients')
            return realRecipients(homework, now)

        with mock.patch('teaching.communication_service.lockHomework', side_effect=lock) as locked:
            with mock.patch('teaching.communication_service.reminderRecipients',
                            side_effect=recipients):
                result = sendHomeworkReminders(self.teacher, self.homework, {})
        locked.assert_called_once_with(self.homework.pk)
        self.assertEqual(calls, ['lock', 'recipients'])
        self.assertEqual(result, {'homeworkId': self.homework.pk, 'eligibleCount': 1,
                                  'sentCount': 0, 'skippedCount': 1})
        self.assertEqual(HomeworkReminder.objects.count(), 1)

    def test_all_submitted_creates_no_reminders(self):
        for user in (self.student, self.completed):
            for problem in (self.problem, self.problem2):
                self.submit(user=user, problem=problem)
        response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.data, {
            'homeworkId': self.homework.pk, 'eligibleCount': 0, 'sentCount': 0, 'skippedCount': 0})
        self.assertFalse(HomeworkReminder.objects.exists())

    def test_submissions_for_questions_outside_homework_do_not_count(self):
        unrelated = Problem.objects.create(title='作业外题目', owner=self.teacher)
        self.submit(problem=unrelated)
        self.assertEqual(self.client.get(self.url).data['eligibleCount'], 2)
