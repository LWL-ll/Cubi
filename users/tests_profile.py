"""个人资料白名单、权限、原子更新及身份保护。"""
import json
from django.contrib.auth.models import Group
from django.test import TestCase
from rest_framework.test import APIClient

from users.models import SchoolIdentity, TeacherProfile, User


class ProfileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user('profile-teacher', name='教师', password='Test12345')
        cls.teacher.groups.add(Group.objects.get(name='教师'))
        identity = SchoolIdentity.objects.create(
            user=cls.teacher, school_number='T100', identity_type=SchoolIdentity.IDENTITY_TEACHER)
        cls.profile = TeacherProfile.objects.create(identity=identity, title='讲师')
        cls.student = User.objects.create_user('profile-student', name='学生')
        cls.student.groups.add(Group.objects.get(name='学生'))

    def setUp(self):
        self.client = APIClient()
        self.client.force_login(self.teacher)
        self.url = '/api/auth/profile/'

    def test_get_own_readonly_identity_and_update_allowed_fields(self):
        data = self.client.get(self.url).json()
        self.assertEqual(data['identities'][0]['school_number'], 'T100')
        self.assertEqual(data['teacher']['title'], '讲师')
        response = self.client.patch(self.url, {
            'name': '新姓名', 'email': 'teacher@example.com', 'title': '副教授'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.teacher.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual((self.teacher.name, self.teacher.email, self.profile.title),
                         ('新姓名', 'teacher@example.com', '副教授'))
        self.assertTrue(self.teacher.check_password('Test12345'))

    def test_privileged_and_identity_fields_rejected_without_partial_write(self):
        for field, value in (
                ('username', 'forged'), ('is_superuser', True), ('is_staff', True),
                ('groups', []), ('password', 'new'), ('schoolNumber', 'T999'),
                ('departmentId', 999), ('must_change_password', False)):
            with self.subTest(field=field):
                response = self.client.patch(self.url, {'name': '不应保存', field: value}, format='json')
                self.assertEqual(response.status_code, 400)
                self.teacher.refresh_from_db()
                self.assertEqual(self.teacher.name, '教师')
                self.assertFalse(self.teacher.is_superuser)
        self.assertEqual(self.teacher.identities.get().school_number, 'T100')

    def test_invalid_types_lengths_email_and_unicode(self):
        for payload in ([], 'text', {}, {'name': 1}, {'name': ''}, {'name': 'x' * 51},
                        {'email': 'invalid'}, {'title': 'x' * 51}):
            with self.subTest(payload=repr(payload)):
                self.assertEqual(self.client.patch(self.url, payload, format='json').status_code, 400)
        response = self.client.patch(self.url, json.dumps({'name': '\ud800'}),
                                     content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_student_updates_own_profile_and_cannot_forge_teacher_profile(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.patch(self.url, {
            'name': '学生新名', 'email': ''}, format='json').status_code, 200)
        self.assertEqual(self.client.patch(self.url, {
            'name': '不应保存', 'title': '教授'}, format='json').status_code, 400)
        self.student.refresh_from_db()
        self.assertEqual(self.student.name, '学生新名')
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.name, '教师')

    def test_anonymous_revoked_permission_and_forced_password_change(self):
        self.assertEqual(APIClient().get(self.url).status_code, 403)
        self.teacher.groups.clear()
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.teacher.groups.add(Group.objects.get(name='教师'))
        self.teacher.must_change_password = True
        self.teacher.save(update_fields=['must_change_password'])
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.patch(self.url, {'name': '绕过改密'}, format='json').status_code, 403)
