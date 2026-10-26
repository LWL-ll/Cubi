"""班级通知真实 API：授课边界、当前成员、阅读留痕与数据白名单。"""
import json

from django.utils import timezone
from rest_framework.test import APIClient

from teaching.models import ClassNotice, NoticeRead
from teaching.tests import ObjectPermissionTestBase


class ClassNoticeTests(ObjectPermissionTestBase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_login(self.teacher)
        self.url = f'/api/teacher/teaching-classes/{self.myTeachingClass.pk}/notices/'
        self.studentUrl = f'/api/teaching-classes/{self.myTeachingClass.pk}/notices/'

    def create(self, **extra):
        return self.client.post(self.url, {
            'title': '第一周通知', 'content': '请准备课堂资料。', **extra}, format='json')

    def test_create_and_student_explicit_read_are_separate(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        notice = ClassNotice.objects.get(pk=response.data['id'])
        self.assertEqual(notice.created_by, self.teacher)
        self.assertEqual(notice.updated_by, self.teacher)
        self.client.force_login(self.student)
        listing = self.client.get(self.studentUrl).json()
        self.assertEqual(listing['count'], 1)
        self.assertIsNone(listing['results'][0]['myReadAt'])
        detailUrl = f'/api/notices/{notice.pk}/'
        self.assertEqual(self.client.get(detailUrl).data['content'], '请准备课堂资料。')
        self.assertFalse(NoticeRead.objects.exists())
        read = self.client.post(detailUrl + 'read/', {}, format='json')
        self.assertEqual(read.status_code, 200, read.data)
        receipt = NoticeRead.objects.get(notice=notice, user=self.student)
        firstRead = receipt.read_at
        self.assertTrue(self.client.get(detailUrl).data['myReadAt'])
        self.assertTrue(self.client.get(self.studentUrl).data['results'][0]['myReadAt'])
        self.assertEqual(self.client.post(detailUrl + 'read/', {}, format='json').status_code, 200)
        receipt.refresh_from_db()
        self.assertEqual(NoticeRead.objects.count(), 1)
        self.assertEqual(receipt.read_at, firstRead)

    def test_cross_class_and_missing_notice_return_identical_404(self):
        noticeId = self.create().data['id']
        for user, prefix in ((self.teacher2, '/api/teacher'), (self.student2, '/api')):
            self.client.force_login(user)
            foreign = self.client.get(f'{prefix}/notices/{noticeId}/')
            missing = self.client.get(f'{prefix}/notices/99999999/')
            self.assertEqual(foreign.status_code, 404)
            self.assertEqual(foreign.data, missing.data)
        self.assertEqual(self.client.get(self.studentUrl).status_code, 404)
        self.assertEqual(self.client.post(
            f'/api/notices/{noticeId}/read/', {}, format='json').status_code, 404)
        self.assertFalse(NoticeRead.objects.exists())

    def test_co_teacher_can_edit_delete_but_read_timestamp_is_preserved(self):
        noticeId = self.create().data['id']
        self.client.force_login(self.student)
        self.client.post(f'/api/notices/{noticeId}/read/', {}, format='json')
        firstRead = NoticeRead.objects.get().read_at
        self.myTeachingClass.teachers.add(self.teacher2)
        self.client.force_login(self.teacher2)
        url = f'/api/teacher/notices/{noticeId}/'
        response = self.client.patch(url, {'content': '通知修订。'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        notice = ClassNotice.objects.get(pk=noticeId)
        self.assertEqual(notice.created_by, self.teacher)
        self.assertEqual(notice.updated_by, self.teacher2)
        self.assertEqual(NoticeRead.objects.get().read_at, firstRead)
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertFalse(ClassNotice.objects.exists())
        self.assertFalse(NoticeRead.objects.exists())

    def test_readership_only_counts_current_students(self):
        noticeId = self.create().data['id']
        self.client.force_login(self.student)
        self.client.post(f'/api/notices/{noticeId}/read/', {}, format='json')
        self.client.force_login(self.teacher)
        url = f'/api/teacher/notices/{noticeId}/readers/'
        readers = self.client.get(url).data
        self.assertEqual(readers['summary'], {
            'studentCount': 1, 'readCount': 1, 'unreadCount': 0})
        self.assertEqual(readers['results'][0]['id'], self.student.pk)
        self.assertTrue(readers['results'][0]['readAt'])
        member = self.myProfile.memberships.get(left_at__isnull=True)
        member.left_at = timezone.now().date()
        member.save()
        readers = self.client.get(url).data
        self.assertEqual(readers['summary'], {
            'studentCount': 0, 'readCount': 0, 'unreadCount': 0})
        self.assertEqual(readers['results'], [])
        self.assertEqual(NoticeRead.objects.count(), 1)

    def test_departed_student_loses_listing_detail_and_read_access(self):
        noticeId = self.create().data['id']
        member = self.myProfile.memberships.get(left_at__isnull=True)
        member.left_at = timezone.now().date()
        member.save()
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(self.studentUrl).status_code, 404)
        self.assertEqual(self.client.get(f'/api/notices/{noticeId}/').status_code, 404)
        self.assertEqual(self.client.post(
            f'/api/notices/{noticeId}/read/', {}, format='json').status_code, 404)

    def test_student_cannot_manage_or_read_teacher_readership(self):
        noticeId = self.create().data['id']
        self.client.force_login(self.student)
        url = f'/api/teacher/notices/{noticeId}/'
        self.assertEqual(self.create().status_code, 403)
        self.assertEqual(self.client.patch(url, {'title': '越权'}, format='json').status_code, 403)
        self.assertEqual(self.client.delete(url).status_code, 403)
        self.assertEqual(self.client.get(url + 'readers/').status_code, 403)
        self.client.force_login(self.courseTeam)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_mutation_whitelists_and_text_validation(self):
        for payload in ({}, [], {'title': 'a'}, {'title': '', 'content': 'a'},
                        {'title': 'a', 'content': ''}, {'title': 2, 'content': 'a'},
                        {'title': 'a' * 101, 'content': 'a'},
                        {'title': 'a', 'content': 'a' * 10001},
                        {'title': 'a', 'content': 'b', 'createdById': self.teacher2.pk}):
            with self.subTest(payload=str(payload)[:100]):
                self.assertEqual(self.client.post(self.url, payload, format='json').status_code, 400)
        self.assertFalse(ClassNotice.objects.exists())
        noticeId = self.create().data['id']
        url = f'/api/teacher/notices/{noticeId}/'
        for payload in ({}, [], {'teachingClassId': self.otherTeachingClass.pk},
                        {'updatedById': self.teacher2.pk}, {'content': None}):
            self.assertEqual(self.client.patch(url, payload, format='json').status_code, 400)
        response = self.client.patch(url, json.dumps({'content': '\ud800'}),
                                     content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ClassNotice.objects.get(pk=noticeId).content, '请准备课堂资料。')

    def test_read_endpoint_rejects_forged_user_and_non_object(self):
        noticeId = self.create().data['id']
        self.client.force_login(self.student)
        url = f'/api/notices/{noticeId}/read/'
        for payload in ({'userId': self.student2.pk}, [], {'readAt': '2000-01-01'}):
            self.assertEqual(self.client.post(url, payload, format='json').status_code, 400)
        self.assertFalse(NoticeRead.objects.exists())

    def test_teacher_list_paginates_and_never_contains_other_class(self):
        ClassNotice.objects.bulk_create([
            ClassNotice(teaching_class=self.myTeachingClass, title=f'通知{i}',
                        content='正文', created_by=self.teacher, updated_by=self.teacher)
            for i in range(21)])
        ClassNotice.objects.create(teaching_class=self.otherTeachingClass,
                                   title='他班通知', content='正文', created_by=self.teacher2,
                                   updated_by=self.teacher2)
        listing = self.client.get(self.url).data
        self.assertEqual(listing['count'], 21)
        self.assertEqual(len(listing['results']), 20)
        self.assertEqual(len(self.client.get(self.url + '?page=2').data['results']), 1)

    def test_removing_teacher_from_class_revokes_notice_mutations(self):
        noticeId = self.create().data['id']
        self.myTeachingClass.teachers.remove(self.teacher)
        url = f'/api/teacher/notices/{noticeId}/'
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.patch(url, {'content': '越权'}, format='json').status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
