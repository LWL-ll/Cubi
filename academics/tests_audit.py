"""后台角色、账号操作也写入可查询审计，原生管理日志继续保留。"""
import json

from django.contrib.auth.models import Group, Permission
from django.test import TestCase

from academics.models import AuditEvent
from users.models import User


class NativeAdminAuditTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser('audit-admin', password='AuditPassword123!')
        cls.target = User.objects.create_user('audit-target', password='BeforePassword123!')

    def setUp(self):
        self.client.force_login(self.admin)

    def test_group_create_and_permission_changes_have_before_after(self):
        self.assertEqual(self.client.post('/admin/auth/group/add/', {'name': '审计测试角色'}).status_code, 302)
        group = Group.objects.get(name='审计测试角色')
        created = AuditEvent.objects.get(action='admin.create')
        self.assertEqual(created.object_type, 'auth.group')
        permission = Permission.objects.filter(codename='class_manage').get()
        response = self.client.post(f'/admin/auth/group/{group.pk}/change/', {
            'name': group.name, 'permissions': [permission.pk]})
        self.assertEqual(response.status_code, 302)
        event = AuditEvent.objects.get(action='admin.update')
        self.assertEqual(event.before['permissionsIds'], [])
        self.assertEqual(event.after['permissionsIds'], [permission.pk])

    def test_admin_password_reset_never_records_password_or_hash(self):
        secret = 'NewAuditPassword456!'
        response = self.client.post(f'/admin/users/user/{self.target.pk}/password/', {
            'usable_password': 'true', 'password1': secret, 'password2': secret})
        self.assertEqual(response.status_code, 302)
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password(secret))
        self.assertTrue(self.target.must_change_password)
        event = AuditEvent.objects.get(action='account.reset_password')
        self.assertEqual(event.actor_id, self.admin.pk)
        serialized = json.dumps(list(AuditEvent.objects.values('before', 'after')))
        self.assertNotIn(secret, serialized)
        self.assertNotIn(self.target.password, serialized)

    def test_admin_deletion_retains_target_id_and_snapshot(self):
        targetId = self.target.pk
        response = self.client.post(f'/admin/users/user/{targetId}/delete/', {'post': 'yes'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(pk=targetId).exists())
        event = AuditEvent.objects.get(action='admin.delete')
        self.assertEqual(event.object_id, str(targetId))
        self.assertEqual(event.before['username'], 'audit-target')

    def test_native_identity_maintenance_rejects_non_superuser_staff(self):
        staff = User.objects.create_user('audit-staff', is_staff=True)
        staff.user_permissions.set(Permission.objects.filter(content_type__app_label='users'))
        self.client.force_login(staff)
        for model in ('schoolidentity', 'studentprofile', 'teacherprofile'):
            self.assertEqual(self.client.get(f'/admin/users/{model}/').status_code, 403)

    def test_legacy_course_assignment_records_department_ids(self):
        from academics.models import Course, Department
        course = Course.objects.create(code='AUDIT', name='审计课程')
        department = Department.objects.create(code='AUDIT', name='审计学院')
        response = self.client.post(f'/api/courses/{course.pk}/', {
            'action': 'addDepartment', 'departmentId': department.pk}, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        event = AuditEvent.objects.get()
        self.assertEqual(event.before['departmentIds'], [])
        self.assertEqual(event.after['departmentIds'], [department.pk])

    def test_admin_academic_status_change_ends_membership_and_preserves_account(self):
        from datetime import date
        from academics.models import AdminClass, ClassMembership, Department, Major
        from users.models import SchoolIdentity, StudentProfile
        department = Department.objects.create(code='STATUS', name='状态学院')
        major = Major.objects.create(code='STATUS', name='状态专业', department=department)
        adminClass = AdminClass.objects.create(name='状态班', major=major, enrollment_year=2023)
        identity = SchoolIdentity.objects.create(user=self.target, school_number='STATUS01',
                                                  identity_type='student')
        profile = StudentProfile.objects.create(identity=identity, major=major, enrollment_year=2023)
        member = ClassMembership.objects.create(student_profile=profile, admin_class=adminClass,
                                                joined_at=date(2023, 9, 1))
        response = self.client.post(f'/admin/users/studentprofile/{profile.pk}/change/', {
            'major': major.pk, 'enrollment_year': 2023, 'academic_status': '休学'})
        self.assertEqual(response.status_code, 302)
        member.refresh_from_db()
        self.assertIsNotNone(member.left_at)
        self.assertTrue(User.objects.filter(pk=self.target.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action='student.academic_update').exists())
