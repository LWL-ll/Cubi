"""参考答案的公开边界、对象权限与作业锁定回归。"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from academics.models import ClassMembership
from teaching.answer_service import (deleteAnswer, listStudentAnswers,
                                     listTeacherAnswers, publishAnswers,
                                     saveAnswer)
from teaching.homework_service import deleteHomework, updateHomework
from teaching.models import Homework, HomeworkAnswer, HomeworkProblem, Problem
from teaching.tests import ObjectPermissionTestBase


class HomeworkAnswerTests(ObjectPermissionTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.now = timezone.now()
        cls.problem = Problem.objects.create(
            title='题一', owner=cls.teacher, languages=['python'],
            tests=[{'input': 'HIDDEN_INPUT', 'output': 'HIDDEN_OUTPUT', 'points': 100}],
            spj_code='HIDDEN_SPJ')
        cls.problem2 = Problem.objects.create(
            title='题二', owner=cls.teacher, languages=['python'])
        cls.homework = Homework.objects.create(
            teaching_class=cls.myTeachingClass, title='答案测试',
            start_at=cls.now - timedelta(days=2),
            deadline=cls.now - timedelta(days=1), created_by=cls.teacher)
        cls.item = HomeworkProblem.objects.create(
            homework=cls.homework, problem=cls.problem, order=0, problem_revision=1)
        cls.item2 = HomeworkProblem.objects.create(
            homework=cls.homework, problem=cls.problem2, order=1, problem_revision=1)

    def setUp(self):
        self.client = APIClient()
        self.client.force_login(self.teacher)
        self.teacherUrl = f'/api/teacher/homeworks/{self.homework.pk}/answers/'
        self.studentUrl = f'/api/homeworks/{self.homework.pk}/answers/'
        self.answerUrl = self.teacherUrl + f'{self.problem.pk}/'
        self.publishUrl = self.teacherUrl + 'publish/'

    def save(self, problem=None, content='教师参考解答'):
        return saveAnswer(self.teacher, self.homework.pk,
                          (problem or self.problem).pk, {'content': content})

    def publish(self):
        return publishAnswers(self.teacher, self.homework.pk, {})

    def homeworkPayload(self, **changes):
        payload = {
            'title': self.homework.title,
            'startAt': self.homework.start_at.isoformat(),
            'deadline': self.homework.deadline.isoformat(),
            'problemIds': [self.problem.pk, self.problem2.pk],
        }
        payload.update(changes)
        return payload

    def test_teacher_can_save_order_update_and_delete_answers(self):
        self.save(self.problem2, '第二题')
        result = self.save()
        self.assertEqual(result['problemId'], self.problem.pk)
        self.assertEqual(result['updatedById'], self.teacher.pk)
        rows = listTeacherAnswers(self.teacher, self.homework.pk)
        self.assertIsNone(rows['releasedAt'])
        self.assertEqual([r['problemId'] for r in rows['answers']],
                         [self.problem.pk, self.problem2.pk])
        self.save(content='修改答案')
        self.assertEqual(HomeworkAnswer.objects.count(), 2)
        deleteAnswer(self.teacher, self.homework.pk, self.problem.pk)
        self.assertEqual(HomeworkAnswer.objects.count(), 1)

    def test_content_must_be_only_nonempty_valid_text(self):
        invalid = (None, [], {}, {'content': ''}, {'content': '  '},
                   {'content': 1}, {'content': 'x' * 50001},
                   {'content': '\ud800'}, {'content': '正文', 'updatedById': self.teacher2.pk})
        for payload in invalid:
            with self.subTest(payload=repr(payload)[:80]):
                with self.assertRaises(ValidationError):
                    saveAnswer(self.teacher, self.homework.pk, self.problem.pk, payload)
        self.assertFalse(HomeworkAnswer.objects.exists())

    def test_answer_writes_never_change_frozen_problem_or_judge_data(self):
        original = self.item.problem_snapshot
        self.save(content='print(42)')
        self.publish()
        self.item.refresh_from_db()
        self.problem.refresh_from_db()
        self.assertEqual(self.item.problem_snapshot, original)
        self.assertEqual(self.problem.tests[0]['output'], 'HIDDEN_OUTPUT')
        data = listStudentAnswers(self.student, self.homework.pk)
        encoded = json.dumps(data, default=str)
        for hidden in ('HIDDEN_INPUT', 'HIDDEN_OUTPUT', 'HIDDEN_SPJ', 'judge_snapshot', 'tests'):
            self.assertNotIn(hidden, encoded)
        self.assertEqual(set(data['answers'][0]),
                         {'problemId', 'content', 'updatedById', 'createdAt', 'updatedAt'})

    def test_student_cannot_read_draft_even_after_deadline(self):
        self.save()
        with self.assertRaisesMessage(Http404, '参考答案不存在'):
            listStudentAnswers(self.student, self.homework.pk)

    def test_student_cannot_read_even_published_answers_before_deadline(self):
        self.save()
        Homework.objects.filter(pk=self.homework.pk).update(
            deadline=self.now + timedelta(hours=1), answers_released_at=self.now)
        with self.assertRaisesMessage(Http404, '参考答案不存在'):
            listStudentAnswers(self.student, self.homework.pk)

    def test_publish_requires_strictly_after_deadline(self):
        self.save()
        for now in (self.homework.deadline - timedelta(microseconds=1), self.homework.deadline):
            with self.subTest(now=now), patch('teaching.answer_service.timezone.now', return_value=now):
                with self.assertRaises(ValidationError):
                    self.publish()
        self.homework.refresh_from_db()
        self.assertIsNone(self.homework.answers_released_at)

    def test_publish_requires_an_answer_and_empty_object(self):
        with self.assertRaises(ValidationError):
            self.publish()
        self.save()
        for payload in (None, [], {'releasedAt': self.now.isoformat()}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                publishAnswers(self.teacher, self.homework.pk, payload)
        self.homework.refresh_from_db()
        self.assertIsNone(self.homework.answers_released_at)

    def test_partial_publication_is_idempotent_and_student_can_read(self):
        self.save()
        first = self.publish()
        second = self.publish()
        self.assertIsNotNone(first['releasedAt'])
        self.assertEqual(first['releasedAt'], second['releasedAt'])
        self.assertEqual(len(first['answers']), 1)
        self.assertEqual(listStudentAnswers(self.student, self.homework.pk), first)

    def test_published_answers_can_be_corrected_or_deleted_without_clearing_latch(self):
        self.save()
        released = self.publish()['releasedAt']
        self.myTeachingClass.teachers.add(self.teacher2)
        result = saveAnswer(self.teacher2, self.homework.pk, self.problem.pk,
                            {'content': '修正后的公开答案'})
        self.assertEqual(result['updatedById'], self.teacher2.pk)
        self.assertEqual(listStudentAnswers(self.student, self.homework.pk)['answers'][0]['content'],
                         '修正后的公开答案')
        deleteAnswer(self.teacher2, self.homework.pk, self.problem.pk)
        result = listStudentAnswers(self.student, self.homework.pk)
        self.assertEqual(result['releasedAt'], released)
        self.assertEqual(result['answers'], [])
        self.assertEqual(self.publish()['releasedAt'], released)

    def test_all_teacher_operations_reject_foreign_or_missing_homework(self):
        self.save()
        calls = (
            lambda user, pk: listTeacherAnswers(user, pk),
            lambda user, pk: saveAnswer(user, pk, self.problem.pk, {'content': '越权'}),
            lambda user, pk: deleteAnswer(user, pk, self.problem.pk),
            lambda user, pk: publishAnswers(user, pk, {}),
        )
        for call in calls:
            for pk in (self.homework.pk, 999999):
                with self.subTest(call=call, pk=pk), self.assertRaisesMessage(Http404, '作业不存在'):
                    call(self.teacher2, pk)
        self.assertEqual(HomeworkAnswer.objects.get().content, '教师参考解答')

    def test_answer_cannot_target_a_problem_outside_the_homework(self):
        outside = Problem.objects.create(title='未布置题', owner=self.teacher)
        for problemId in (outside.pk, 999999):
            with self.subTest(problemId=problemId):
                with self.assertRaisesMessage(Http404, '题目不存在'):
                    saveAnswer(self.teacher, self.homework.pk, problemId, {'content': '答案'})
                with self.assertRaisesMessage(Http404, '题目不存在'):
                    deleteAnswer(self.teacher, self.homework.pk, problemId)

    def test_foreign_missing_and_departed_student_answers_have_same_404(self):
        self.save()
        self.publish()
        for user, pk in ((self.student2, self.homework.pk), (self.student, 999999)):
            with self.subTest(user=user, pk=pk), self.assertRaisesMessage(Http404, '参考答案不存在'):
                listStudentAnswers(user, pk)
        ClassMembership.objects.filter(student_profile=self.myProfile,
                                       left_at__isnull=True).update(left_at=self.now.date())
        with self.assertRaisesMessage(Http404, '参考答案不存在'):
            listStudentAnswers(self.student, self.homework.pk)

    def test_stale_homework_cannot_extend_deadline_after_answers_published(self):
        stale = Homework.objects.get(pk=self.homework.pk)
        self.save()
        self.publish()
        with self.assertRaisesMessage(ValidationError, '不能延长截止时间'):
            updateHomework(self.teacher, stale, self.homeworkPayload(
                deadline=(self.now + timedelta(days=1)).isoformat()))
        self.homework.refresh_from_db()
        self.assertLess(self.homework.deadline, self.now)

    def test_published_homework_cannot_replace_or_reorder_questions(self):
        self.save()
        self.publish()
        for problems in ([self.problem.pk], [self.problem2.pk, self.problem.pk]):
            with self.subTest(problems=problems), self.assertRaisesMessage(ValidationError, '题目不可变更'):
                updateHomework(self.teacher, self.homework,
                               self.homeworkPayload(problemIds=problems))
        self.assertEqual(HomeworkAnswer.objects.count(), 1)

    def test_published_homework_still_allows_title_correction(self):
        self.save()
        self.publish()
        result = updateHomework(self.teacher, self.homework, self.homeworkPayload(title='更正标题'))
        self.assertEqual(result.title, '更正标题')
        self.assertIsNotNone(result.answers_released_at)

    def test_draft_removed_when_questions_replaced_and_old_problem_cannot_be_saved(self):
        self.save()
        updateHomework(self.teacher, self.homework,
                       self.homeworkPayload(problemIds=[self.problem2.pk]))
        self.assertFalse(HomeworkAnswer.objects.exists())
        with self.assertRaisesMessage(Http404, '题目不存在'):
            self.save()
        with self.assertRaises(ValidationError):
            self.publish()

    def test_deleting_homework_removes_draft_and_publish_then_fails(self):
        self.save()
        deleteHomework(self.homework)
        self.assertFalse(HomeworkAnswer.objects.exists())
        with self.assertRaisesMessage(Http404, '作业不存在'):
            self.publish()

    def test_publish_reads_extended_deadline_from_database(self):
        self.save()
        updateHomework(self.teacher, self.homework, self.homeworkPayload(
            deadline=(self.now + timedelta(days=1)).isoformat()))
        with self.assertRaisesMessage(ValidationError, '作业截止后'):
            self.publish()

    def test_teacher_and_student_api_round_trip(self):
        response = self.client.put(self.answerUrl, {'content': 'API答案'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['content'], 'API答案')
        self.assertEqual(self.client.get(self.teacherUrl).data['answers'][0]['content'], 'API答案')
        self.assertEqual(self.client.post(self.publishUrl, {}, format='json').status_code, 200)
        self.client.force_login(self.student)
        response = self.client.get(self.studentUrl)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['answers'][0]['content'], 'API答案')
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.delete(self.answerUrl).status_code, 204)

    def test_api_draft_never_appears_in_student_homework_or_problem_detail(self):
        self.save(content='PRIVATE_DRAFT_BODY')
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(self.studentUrl).status_code, 404)
        for url in (f'/api/homeworks/{self.homework.pk}/',
                    f'/api/problems/{self.problem.pk}/?homeworkId={self.homework.pk}'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'PRIVATE_DRAFT_BODY')

    def test_homework_answer_flags_follow_release_and_deadline(self):
        self.save()
        teacherDetail = f'/api/teacher/homeworks/{self.homework.pk}/'
        studentDetail = f'/api/homeworks/{self.homework.pk}/'
        self.assertIsNone(self.client.get(teacherDetail).data['answersReleasedAt'])
        self.client.force_login(self.student)
        self.assertFalse(self.client.get(studentDetail).data['answersAvailable'])
        released = self.publish()['releasedAt']
        self.assertTrue(self.client.get(studentDetail).data['answersAvailable'])
        row = next(row for row in self.client.get('/api/homeworks/').data
                   if row['id'] == self.homework.pk)
        self.assertTrue(row['answersAvailable'])
        self.client.force_login(self.teacher)
        self.assertIsNotNone(self.client.get(teacherDetail).data['answersReleasedAt'])
        Homework.objects.filter(pk=self.homework.pk).update(deadline=self.now + timedelta(hours=1))
        self.client.force_login(self.student)
        self.assertFalse(self.client.get(studentDetail).data['answersAvailable'])
        self.assertEqual(self.client.get(self.studentUrl).status_code, 404)
        self.homework.refresh_from_db()
        self.assertEqual(self.homework.answers_released_at, released)

    def test_api_roles_and_cross_class_access(self):
        self.save()
        self.publish()
        for user in (self.student, self.courseTeam):
            self.client.force_login(user)
            for url in (self.teacherUrl, self.answerUrl, self.publishUrl):
                response = (self.client.post(url, {}, format='json') if url == self.publishUrl
                            else self.client.put(url, {'content': '越权'}, format='json')
                            if url == self.answerUrl else self.client.get(url))
                self.assertEqual(response.status_code, 403)
        self.client.force_login(self.teacher2)
        self.assertEqual(self.client.get(self.teacherUrl).status_code, 404)
        self.assertEqual(self.client.delete(self.answerUrl).status_code, 404)
        self.assertEqual(self.client.post(self.publishUrl, {}, format='json').status_code, 404)
        self.client.force_login(self.student2)
        foreign = self.client.get(self.studentUrl)
        missing = self.client.get('/api/homeworks/999999/answers/')
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.data, missing.data)

    def test_api_rejects_invalid_unicode_without_partial_write(self):
        response = self.client.put(self.answerUrl,
            json.dumps({'content': '\ud800'}, ensure_ascii=True), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(HomeworkAnswer.objects.exists())
