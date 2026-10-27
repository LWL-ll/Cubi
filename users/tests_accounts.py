"""账号管理不能越权维护高权限对象；重置密码保留强度和会话失效规则。"""
import json
from unittest.mock import patch

from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from academics.models import AuditEvent, Department, Major
from users.account_api import AccountDetailView, AccountListView, AccountPasswordResetView
from users.account_service import resetAccountPassword, updateAccount
from users.models import SchoolIdentity, StudentProfile, TeacherProfile, User


class AccountManagementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.office = User.objects.create_user(username='ACCOUNT_OFFICE')
        cls.office.groups.add(Group.objects.get(name='教务'))
        cls.superuser = User.objects.create_superuser(username='ACCOUNT_SUPER', password='SuperStrong!2026')
        cls.teacher = User.objects.create_user(username='ACCOUNT_TEACHER', name='原教师', password='Original!2026')
        cls.teacher.groups.add(Group.objects.get(name='教师'))
        cls.student = User.objects.create_user(username='ACCOUNT_STUDENT', name='原学生', password='Original!2026')
        cls.student.groups.add(Group.objects.get(name='学生'))
        cls.department = Department.objects.create(code='ACCOUNT', name='账号学院')
        major = Major.objects.create(department=cls.department, code='A', name='账号专业')
        cls.teacherIdentity = SchoolIdentity.objects.create(user=cls.teacher,
            school_number='TEACHER001', identity_type=SchoolIdentity.IDENTITY_TEACHER)
        cls.teacherProfile = TeacherProfile.objects.create(identity=cls.teacherIdentity,
            department=cls.department, title='讲师')
        studentIdentity = SchoolIdentity.objects.create(user=cls.student,
            school_number='STUDENT001', identity_type=SchoolIdentity.IDENTITY_STUDENT)
        StudentProfile.objects.create(identity=studentIdentity, major=major, enrollment_year=2023)

    def call(self, view, method='get', data=None, actor=None, **kwargs):
        request = getattr(APIRequestFactory(), method)('/api/test/', data, format='json')
        force_authenticate(request, user=actor or self.office)
        return view.as_view()(request, **kwargs)

    def detail(self, account=None, payload=None, actor=None):
        return self.call(AccountDetailView, 'get' if payload is None else 'patch', payload,
                         actor=actor, accountId=(account or self.teacher).pk)

    def reset(self, payload, actor=None, account=None):
        return self.call(AccountPasswordResetView, 'post', payload, actor=actor,
                         accountId=(account or self.teacher).pk)

    def test_ordinary_list_is_paginated_and_explicitly_safe(self):
        response = self.call(AccountListView)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 2)
        teacher = next(row for row in response.data['results'] if row['id'] == self.teacher.pk)
        self.assertEqual(teacher['identities'][0]['teacher']['title'], '讲师')
        self.assertEqual(set(teacher), {'id', 'username', 'name', 'email', 'isActive',
            'mustChangePassword', 'groups', 'identities'})
        serialized = json.dumps(teacher)
        for secret in ('pbkdf2', 'Original!', 'is_superuser', 'user_permissions', 'verification_status'):
            self.assertNotIn(secret, serialized)

    def test_role_active_and_school_number_search_filters(self):
        self.student.is_active = False
        self.student.save(update_fields=['is_active'])
        for query in ({'role': 'student'}, {'isActive': 'false'}, {'q': 'STUDENT001'}):
            response = self.call(AccountListView, data=query)
            self.assertEqual([row['id'] for row in response.data['results']], [self.student.pk])
        for query in ({'role': 'admin'}, {'isActive': '1'}, {'q': 'x' * 101}):
            self.assertEqual(self.call(AccountListView, data=query).status_code, 400)

    def test_teacher_and_student_cannot_access_management(self):
        for actor in (self.teacher, self.student):
            self.assertEqual(self.call(AccountListView, actor=actor).status_code, 403)
            self.assertEqual(self.detail(actor=actor).status_code, 403)
            self.assertEqual(self.reset({'password': 'NewStrong!2026'}, actor=actor).status_code, 403)
            with self.assertRaises(PermissionDenied):
                updateAccount(actor, self.teacher.pk, {'name': '越权'})

    def test_office_cannot_manage_staff_superuser_office_leader_or_custom_permissions(self):
        accounts = [self.office, self.superuser]
        for key in ('staff', 'leader', 'direct', 'custom'):
            account = User.objects.create_user(username=f'PRIV_{key}', is_staff=key == 'staff')
            account.groups.add(Group.objects.get(name='教师'))
            if key == 'leader':
                account.groups.add(Group.objects.get(name='课程组'))
            elif key == 'direct':
                account.user_permissions.add(Permission.objects.get(
                    content_type__app_label='users', codename='class_manage'))
            elif key == 'custom':
                group = Group.objects.create(name='自定义高权限')
                group.permissions.add(Permission.objects.get(
                    content_type__app_label='users', codename='change_user'))
                account.groups.add(group)
            accounts.append(account)
        for account in accounts:
            with self.subTest(account=account.username):
                self.assertEqual(self.detail(account).status_code, 404)
                self.assertEqual(self.detail(account, {'name': '篡改'}).status_code, 404)
        ids = {row['id'] for row in self.call(AccountListView).data['results']}
        self.assertEqual(ids, {self.teacher.pk, self.student.pk})

    def test_superuser_can_manage_privileged_target(self):
        response = self.detail(self.office, {'name': '更新教务'}, actor=self.superuser)
        self.assertEqual(response.status_code, 200, response.data)
        self.office.refresh_from_db()
        self.assertEqual(self.office.name, '更新教务')

    def test_valid_profile_edit_updates_only_whitelisted_fields_and_audits(self):
        response = self.detail(payload={'name': '新姓名', 'email': 'teacher@example.edu', 'title': '副教授'})
        self.assertEqual(response.status_code, 200, response.data)
        self.teacher.refresh_from_db()
        self.teacherProfile.refresh_from_db()
        self.assertEqual(self.teacher.name, '新姓名')
        self.assertEqual(self.teacherProfile.title, '副教授')
        self.assertTrue(self.teacher.check_password('Original!2026'))
        event = AuditEvent.objects.get(action='account.update')
        self.assertEqual(event.before['name'], '原教师')
        self.assertEqual(event.after['name'], '新姓名')

    def test_teacher_department_can_be_updated_or_cleared_but_not_disabled(self):
        other = Department.objects.create(code='ACCOUNT_OTHER', name='其他学院')
        self.assertEqual(self.detail(payload={'departmentId': other.pk}).status_code, 200)
        self.teacherProfile.refresh_from_db()
        self.assertEqual(self.teacherProfile.department_id, other.pk)
        self.assertEqual(self.detail(payload={'departmentId': None}).status_code, 200)
        other.is_active = False
        other.save(update_fields=['is_active'])
        self.assertEqual(self.detail(payload={'departmentId': other.pk}).status_code, 400)

    def test_student_teacher_fields_fail_atomically_without_creating_teacher_profile(self):
        response = self.detail(self.student, {'name': '不能提交', 'title': '教授'})
        self.assertEqual(response.status_code, 400)
        self.student.refresh_from_db()
        self.assertEqual(self.student.name, '原学生')
        self.assertFalse(TeacherProfile.objects.filter(identity__user=self.student).exists())

    def test_unknown_sensitive_fields_bad_types_and_email_are_rejected(self):
        for payload in ({'username': 'new'}, {'password': 'secret'}, {'groups': ['教务']},
                        {'isStaff': True}, {'isSuperuser': True}, {'schoolNumber': 'NEW'},
                        {'isActive': 1}, {'name': ''}, {'email': 'bad'},
                        {'name': '\x00'}, {'title': 'x' * 51}, [], {}):
            with self.subTest(payload=payload):
                self.assertEqual(self.detail(payload=payload).status_code, 400)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.username, 'ACCOUNT_TEACHER')
        self.assertTrue(self.teacher.is_active)

    def test_deactivate_reactivate_keeps_identity_profile_and_password(self):
        self.assertEqual(self.detail(payload={'isActive': False}).status_code, 200)
        self.teacher.refresh_from_db()
        self.assertFalse(self.teacher.is_active)
        self.assertTrue(self.teacher.check_password('Original!2026'))
        self.assertTrue(TeacherProfile.objects.filter(pk=self.teacherProfile.pk).exists())
        self.assertEqual(self.detail(payload={'isActive': True}).status_code, 200)

    def test_superuser_cannot_disable_self_from_management(self):
        self.assertEqual(self.detail(self.superuser, {'isActive': False}, actor=self.superuser).status_code, 400)

    def test_password_reset_only_superuser_and_no_weak_password(self):
        oldHash = self.teacher.password
        self.assertEqual(self.reset({'password': 'NewStrong!2026'}).status_code, 403)
        for password in ('12345678', 'short', 'password', 'ACCOUNT_TEACHER', True, None, 'x' * 1025):
            self.assertEqual(self.reset({'password': password}, actor=self.superuser).status_code, 400)
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.password, oldHash)
        self.assertFalse(AuditEvent.objects.filter(action='account.reset_password').exists())

    def test_reset_marks_change_required_invalidates_old_session_and_never_audits_password(self):
        sessionClient = Client()
        sessionClient.force_login(self.teacher)
        response = self.reset({'password': ' NewStrong!2026 '}, actor=self.superuser)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(response.data), {'id', 'mustChangePassword'})
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.check_password(' NewStrong!2026 '))
        self.assertTrue(self.teacher.must_change_password)
        self.assertIn(sessionClient.get('/api/auth/me/').status_code, (401, 403))
        event = AuditEvent.objects.get(action='account.reset_password')
        self.assertNotIn('NewStrong', json.dumps(event.before) + json.dumps(event.after))
        self.assertNotIn(self.teacher.password, json.dumps(event.before) + json.dumps(event.after))

    def test_reset_does_not_accept_flags_or_self_target(self):
        self.assertEqual(self.reset({'password': 'NewStrong!2026', 'mustChangePassword': False},
                                   actor=self.superuser).status_code, 400)
        self.assertEqual(self.reset({'password': 'NewStrong!2026'}, actor=self.superuser,
                                   account=self.superuser).status_code, 400)

    def test_audit_failure_rolls_back_password_and_profile(self):
        originalHash = self.teacher.password
        with patch('academics.audit_service.recordAudit', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                resetAccountPassword(self.superuser, self.teacher.pk, {'password': 'NewStrong!2026'})
            with self.assertRaises(RuntimeError):
                updateAccount(self.office, self.teacher.pk, {'name': '回滚'})
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.password, originalHash)
        self.assertEqual(self.teacher.name, '原教师')
