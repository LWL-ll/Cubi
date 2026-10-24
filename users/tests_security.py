"""登录、后台授权和强制改密的安全边界回归。"""
import re

from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from rest_framework.test import APIClient

from users.models import User
from users.permissions import hasPerm


PASSWORD = 'SecurityTest123!'


class LoginCsrfTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='security-login', password=PASSWORD)

    def setUp(self):
        self.client = APIClient(enforce_csrf_checks=True)

    def token(self):
        page = self.client.get('/login/')
        return re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"',
                         page.content.decode()).group(1)

    def login(self, **headers):
        return self.client.post('/api/auth/login/',
            {'username': self.user.username, 'password': PASSWORD},
            format='json', **headers)

    def test_anonymous_login_requires_csrf(self):
        self.assertEqual(self.login().status_code, 403)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_cookie_without_token_and_invalid_token_are_rejected(self):
        self.token()
        self.assertEqual(self.login().status_code, 403)
        self.assertEqual(self.login(HTTP_X_CSRFTOKEN='invalid').status_code, 403)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_valid_token_logs_in_and_cross_origin_is_rejected(self):
        token = self.token()
        self.assertEqual(self.login(HTTP_X_CSRFTOKEN=token,
            HTTP_ORIGIN='https://untrusted.example').status_code, 403)
        self.assertEqual(self.login(HTTP_X_CSRFTOKEN=token).status_code, 200)
        self.assertEqual(self.client.get('/api/auth/me/').json()['username'], self.user.username)


class AdminAuthorizationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(username='security-admin', password=PASSWORD)
        cls.staff = User.objects.create_user(username='security-staff', password=PASSWORD,
                                             is_staff=True)
        cls.target = User.objects.create_user(username='security-target', password=PASSWORD)
        cls.staff.user_permissions.set(Permission.objects.filter(
            content_type__app_label='users', content_type__model='user'))
        cls.staff.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='auth', content_type__model='group'))

    def test_staff_with_native_permissions_cannot_manage_users_or_groups(self):
        self.client.force_login(self.staff)
        group = Group.objects.get(name='教师')
        urls = ['/admin/users/user/', '/admin/users/user/add/',
                f'/admin/users/user/{self.target.pk}/change/',
                f'/admin/users/user/{self.target.pk}/password/',
                f'/admin/users/user/{self.target.pk}/delete/',
                '/admin/auth/group/', '/admin/auth/group/add/',
                f'/admin/auth/group/{group.pk}/change/',
                f'/admin/auth/group/{group.pk}/delete/']
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_forged_posts_cannot_elevate_reset_or_delete(self):
        self.client.force_login(self.staff)
        group = Group.objects.get(name='教师')
        originalPermissions = set(group.permissions.values_list('pk', flat=True))
        requests = [
            (f'/admin/users/user/{self.staff.pk}/change/',
             {'username': self.staff.username, 'is_staff': 'on', 'is_superuser': 'on'}),
            ('/admin/users/user/add/', {'username': 'forged-admin',
             'usable_password': 'true', 'password1': PASSWORD, 'password2': PASSWORD}),
            (f'/admin/users/user/{self.target.pk}/password/',
             {'usable_password': 'true', 'password1': 'ForgedPass123!',
              'password2': 'ForgedPass123!'}),
            (f'/admin/users/user/{self.target.pk}/delete/', {'post': 'yes'}),
            (f'/admin/auth/group/{group.pk}/change/',
             {'name': group.name, 'permissions': list(self.staff.user_permissions.values_list('pk', flat=True))}),
            ('/admin/auth/group/add/', {'name': '伪造角色'}),
            (f'/admin/auth/group/{group.pk}/delete/', {'post': 'yes'}),
        ]
        for url, data in requests:
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, data).status_code, 403)
        self.staff.refresh_from_db()
        self.target.refresh_from_db()
        self.assertFalse(self.staff.is_superuser)
        self.assertTrue(self.target.check_password(PASSWORD))
        self.assertFalse(User.objects.filter(username='forged-admin').exists())
        self.assertFalse(Group.objects.filter(name='伪造角色').exists())
        self.assertEqual(set(group.permissions.values_list('pk', flat=True)), originalPermissions)

    def test_superuser_can_still_manage_users_and_groups(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(f'/admin/users/user/{self.target.pk}/change/').status_code, 200)
        self.assertEqual(self.client.get('/admin/users/user/add/').status_code, 200)
        response = self.client.post('/admin/auth/group/add/', {'name': '管理员创建角色'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Group.objects.filter(name='管理员创建角色').exists())

    def test_unknown_permission_codes_are_rejected_for_every_role(self):
        for user in (self.admin, self.staff, self.target):
            for code in ('unknown:permission', '', None, ['homework:view']):
                with self.subTest(user=user.username, code=code):
                    self.assertFalse(hasPerm(user, code))
        self.assertTrue(hasPerm(self.admin, 'homework:view'))

    def test_forced_password_change_also_blocks_admin_until_completed(self):
        for user in (self.admin, self.staff):
            with self.subTest(user=user.username):
                User.objects.filter(pk=user.pk).update(must_change_password=True)
                client = APIClient()
                client.force_login(user)
                for url in ('/admin/', '/admin/password_change/', '/admin/users/user/'):
                    response = client.get(url)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response['Location'], '/change-password/')
                response = client.post(f'/admin/users/user/{self.target.pk}/password/',
                    {'password1': 'BlockedPass123!', 'password2': 'BlockedPass123!'})
                self.assertEqual(response['Location'], '/change-password/')
                self.target.refresh_from_db()
                self.assertTrue(self.target.check_password(PASSWORD))
                self.assertEqual(client.get('/change-password/').status_code, 200)
                self.assertEqual(client.get('/api/auth/me/').status_code, 200)
                response = client.post('/api/auth/change-password/',
                    {'oldPassword': PASSWORD, 'newPassword': 'NewSecurityPass123!'}, format='json')
                self.assertEqual(response.status_code, 200)
                user.refresh_from_db()
                self.assertFalse(user.must_change_password)
                self.assertEqual(client.get('/admin/').status_code, 200)
