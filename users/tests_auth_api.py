"""纯 API 会话引导、登录 token 轮换与不可信请求校验。"""
import json
from django.contrib.auth.models import Group
from django.test import TestCase
from rest_framework.test import APIClient
from .models import User


class AuthApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = 'InitialStudentPass123!'
        cls.user = User.objects.create_user('student-api-login', password=cls.password,
                                            must_change_password=True)
        cls.user.groups.add(Group.objects.get(name='学生'))

    def setUp(self):
        self.client = APIClient(enforce_csrf_checks=True)

    def token(self):
        response = self.client.get('/api/auth/csrf/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertTrue(response.cookies['csrftoken']['httponly'])
        return response.data['csrfToken']

    def test_json_only_login_rotation_force_change_and_session(self):
        token = self.token()
        response = self.client.post('/api/auth/login/', {'username': self.user.username,
            'password': self.password}, format='json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['mustChangePassword'])
        self.assertNotEqual(token, response.data['csrfToken'])
        self.assertEqual(self.client.get('/api/homeworks/').status_code, 403)
        fresh = self.token()
        self.assertEqual(self.client.post('/api/auth/change-password/', {
            'oldPassword': self.password, 'newPassword': 'ChangedStudentPass456!'},
            format='json', HTTP_X_CSRFTOKEN=token).status_code, 403)
        self.assertEqual(self.client.post('/api/auth/change-password/', {
            'oldPassword': self.password, 'newPassword': 'ChangedStudentPass456!'},
            format='json', HTTP_X_CSRFTOKEN=fresh).status_code, 200)
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 200)
        self.assertEqual(self.client.get('/api/homeworks/').status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.must_change_password)

    def test_cross_origin_and_missing_csrf_still_rejected(self):
        token = self.token()
        data = {'username': self.user.username, 'password': self.password}
        self.assertEqual(self.client.post('/api/auth/login/', data, format='json').status_code, 403)
        self.assertEqual(self.client.post('/api/auth/login/', data, format='json',
            HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN='https://untrusted.invalid').status_code, 403)
        self.assertEqual(self.client.post('/api/auth/csrf/', {}, format='json',
                                         HTTP_X_CSRFTOKEN=token).status_code, 405)

    def test_invalid_login_payload_never_creates_session(self):
        token = self.token()
        for data in ([], 'bad', {}, {'username': 1, 'password': 'x'},
                     {'username': 'x', 'password': {}}, {'username': ' '},
                     {'username': 'x' * 151, 'password': 'x'},
                     {'username': 'x', 'password': 'x' * 1025},
                     {'username': 'x', 'password': 'x', 'is_superuser': True},
                     {'username': '\ud800', 'password': 'x'}):
            with self.subTest(data=str(data)):
                response = self.client.post('/api/auth/login/', json.dumps(data),
                    content_type='application/json', HTTP_X_CSRFTOKEN=token)
                self.assertEqual(response.status_code, 400)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_invalid_change_payload_has_no_password_side_effect(self):
        self.client.force_login(self.user)
        token = self.token()
        for data in ([], {}, {'oldPassword': 1, 'newPassword': 'x'},
                     {'oldPassword': self.password, 'newPassword': {}},
                     {'oldPassword': self.password, 'newPassword': '\ud800'}):
            response = self.client.post('/api/auth/change-password/', json.dumps(data),
                content_type='application/json', HTTP_X_CSRFTOKEN=token)
            self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.password))

    def test_password_spaces_are_preserved(self):
        self.user.set_password('  PasswordWithSpaces123!  ')
        self.user.save(update_fields=['password'])
        token = self.token()
        response = self.client.post('/api/auth/login/', {'username': self.user.username,
            'password': '  PasswordWithSpaces123!  '}, format='json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
