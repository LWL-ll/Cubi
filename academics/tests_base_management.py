"""教务基础维护和学籍异动的权限、历史、事务回归。"""
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from academics.base_api import (OfficeAdminClassListView, OfficeDepartmentDetailView,
    OfficeDepartmentListView, OfficeMajorListView)
from academics.base_service import deleteBase, saveBase, updateStudentAcademic
from academics.models import (AcademicTerm, AdminClass, AuditEvent, ClassMembership,
    Course, CourseCollege, Department, Major, TeachingClass)
from academics.student_api import StudentAcademicView, StudentLeaveView, StudentTransferView
from users.models import SchoolIdentity, StudentProfile, User


class BaseManagementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.office = User.objects.create_user(username='BASE_OFFICE')
        cls.office.groups.add(Group.objects.get(name='教务'))
        cls.teacher = User.objects.create_user(username='BASE_TEACHER')
        cls.teacher.groups.add(Group.objects.get(name='教师'))
        cls.student = User.objects.create_user(username='BASE_STUDENT')
        cls.student.groups.add(Group.objects.get(name='学生'))
        cls.department = Department.objects.create(code='BASE', name='基础学院')
        cls.major = Major.objects.create(department=cls.department, code='M', name='基础专业')
        cls.adminClass = AdminClass.objects.create(major=cls.major, name='一班', enrollment_year=2023)
        identity = SchoolIdentity.objects.create(user=cls.student, school_number='BASE0001',
            identity_type=SchoolIdentity.IDENTITY_STUDENT)
        cls.profile = StudentProfile.objects.create(identity=identity, major=cls.major,
                                                     enrollment_year=2023)
        cls.member = ClassMembership.objects.create(student_profile=cls.profile,
            admin_class=cls.adminClass, joined_at=date(2023, 9, 1))

    def call(self, view, method='get', data=None, actor=None, **kwargs):
        request = getattr(APIRequestFactory(), method)('/api/test/', data, format='json')
        force_authenticate(request, user=actor or self.office)
        return view.as_view()(request, **kwargs)

    def test_department_major_course_and_class_create_without_student_number_parser(self):
        dep = saveBase(self.office, 'departments', {'code': 'NEW', 'name': '新学院'})
        major = saveBase(self.office, 'majors', {'code': 'N', 'name': '新专业', 'departmentId': dep.pk})
        course = saveBase(self.office, 'courses', {'code': 'NEW01', 'name': '新课程'})
        adminClass = saveBase(self.office, 'admin-classes', {
            'name': '手工班', 'majorId': major.pk, 'enrollmentYear': 2025})
        self.assertTrue(course.is_active)
        self.assertEqual(adminClass.major, major)
        self.assertEqual(adminClass.code, '')
        self.assertEqual(AuditEvent.objects.filter(action='base.create').count(), 4)

    def test_crud_permissions_and_explicit_permission_are_enforced(self):
        for actor in (self.teacher, self.student):
            self.assertEqual(self.call(OfficeDepartmentListView, actor=actor).status_code, 403)
            self.assertEqual(self.call(OfficeAdminClassListView, actor=actor).status_code, 403)
            with self.assertRaises(PermissionDenied):
                saveBase(actor, 'departments', {'code': 'DENY', 'name': '拒绝'})
        classOnly = User.objects.create_user(username='BASE_CLASS_ONLY')
        classOnly.user_permissions.add(Permission.objects.get(
            content_type__app_label='users', codename='class_manage'))
        self.assertEqual(self.call(OfficeDepartmentListView, actor=classOnly).status_code, 403)
        self.assertEqual(self.call(OfficeAdminClassListView, actor=classOnly).status_code, 200)

    def test_list_filter_is_active_and_department_are_paginated(self):
        disabled = Department.objects.create(code='OFF', name='停用', is_active=False)
        response = self.call(OfficeDepartmentListView, data={'isActive': 'false'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['id'], disabled.pk)
        response = self.call(OfficeMajorListView, data={'departmentId': self.department.pk})
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(self.call(OfficeDepartmentListView, data={'isActive': '1'}).status_code, 400)

    def test_malformed_text_boolean_unknown_fields_and_ids_do_not_write(self):
        for payload in ([], {}, {'code': 'X'}, {'name': 'X'},
                        {'code': 'X', 'name': 'x', 'extra': True},
                        {'code': '\x00', 'name': 'x'}, {'code': 'X', 'name': 'x', 'isActive': 1},
                        {'code': 'X' * 33, 'name': 'x'}):
            with self.subTest(payload=payload):
                self.assertEqual(self.call(OfficeDepartmentListView, 'post', payload).status_code, 400)
        for identifier in (0, -1, True, 'bad', '9' * 30):
            self.assertEqual(self.call(OfficeDepartmentDetailView, objectId=identifier).status_code, 400)
        self.assertEqual(Department.objects.count(), 1)

    def test_unique_conflicts_return_business_error_and_do_not_leave_audit(self):
        response = self.call(OfficeDepartmentListView, 'post', {'code': 'BASE', 'name': '重复'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AuditEvent.objects.exists())

    def test_disabled_parent_rejects_new_major_and_class(self):
        self.department.is_active = False
        self.department.save(update_fields=['is_active'])
        with self.assertRaises(ValidationError):
            saveBase(self.office, 'majors', {'code': 'NEW', 'name': '新专业', 'departmentId': self.department.pk})
        with self.assertRaises(ValidationError):
            saveBase(self.office, 'admin-classes', {'name': '新班', 'majorId': self.major.pk, 'enrollmentYear': 2024})

    def test_deactivate_does_not_delete_members_or_accounts(self):
        saveBase(self.office, 'admin-classes', {'isActive': False}, self.adminClass.pk)
        self.assertTrue(ClassMembership.objects.filter(pk=self.member.pk, left_at__isnull=True).exists())
        self.assertTrue(User.objects.filter(pk=self.student.pk, is_active=True).exists())
        self.assertFalse(AdminClass.objects.get(pk=self.adminClass.pk).is_active)

    def test_related_major_department_and_class_year_cannot_change(self):
        other = Department.objects.create(code='OTHER', name='其他学院')
        for kind, obj, payload in (
                ('majors', self.major, {'departmentId': other.pk}),
                ('admin-classes', self.adminClass, {'enrollmentYear': 2024})):
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                saveBase(self.office, kind, payload, obj.pk)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.enrollment_year, 2023)

    def test_unused_class_structure_is_editable_and_related_objects_protect_delete(self):
        idle = AdminClass.objects.create(major=self.major, name='空班', enrollment_year=2024)
        edited = saveBase(self.office, 'admin-classes', {'enrollmentYear': 2025}, idle.pk)
        self.assertEqual(edited.enrollment_year, 2025)
        for kind, obj in (('departments', self.department), ('majors', self.major),
                          ('admin-classes', self.adminClass)):
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                deleteBase(self.office, kind, obj.pk)
        deleteBase(self.office, 'admin-classes', idle.pk)
        self.assertFalse(AdminClass.objects.filter(pk=idle.pk).exists())

    def test_course_association_protects_code_and_deletion(self):
        course = Course.objects.create(code='REL', name='关联课')
        CourseCollege.objects.create(course=course, department=self.department)
        with self.assertRaises(ValidationError):
            saveBase(self.office, 'courses', {'code': 'CHANGED'}, course.pk)
        with self.assertRaises(ValidationError):
            deleteBase(self.office, 'courses', course.pk)

    def test_archived_names_are_frozen_but_enabled_flag_can_change(self):
        term = AcademicTerm.objects.create(code='BASE-TERM', name='归档学期', status=AcademicTerm.ARCHIVED,
            start_date=date(2025, 1, 1), end_date=date(2025, 6, 30))
        course = Course.objects.create(code='HIST', name='历史课')
        cc = CourseCollege.objects.create(course=course, department=self.department)
        cc.admin_classes.add(self.adminClass)
        tc = TeachingClass.objects.create(course_college=cc, term='BEFORE-ARCHIVE', name='历史教学班')
        tc.admin_classes.add(self.adminClass)
        TeachingClass.objects.filter(pk=tc.pk).update(term=term.code)
        with self.assertRaises(ValidationError):
            saveBase(self.office, 'admin-classes', {'name': '更名'}, self.adminClass.pk)
        saveBase(self.office, 'admin-classes', {'isActive': False}, self.adminClass.pk)

    def test_audit_failure_rolls_back_base_change(self):
        with patch('academics.audit_service.recordAudit', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                saveBase(self.office, 'departments', {'name': '未提交'}, self.department.pk)
        self.department.refresh_from_db()
        self.assertEqual(self.department.name, '基础学院')

    def academic(self, payload):
        return self.call(StudentAcademicView, 'patch', payload, studentId=self.student.pk)

    def test_nonactive_statuses_close_membership_but_keep_account_and_history(self):
        for status in (StudentProfile.ACADEMIC_SUSPENDED, StudentProfile.ACADEMIC_GRADUATED,
                       StudentProfile.ACADEMIC_DROPPED):
            with self.subTest(status=status):
                response = self.academic({'academicStatus': status})
                self.assertEqual(response.status_code, 200, response.data)
                self.assertIsNone(response.data['currentMembership'])
                self.assertEqual(response.data['academicStatus'], status)
        self.member.refresh_from_db()
        self.assertEqual(self.member.left_at, timezone.localdate())
        self.assertTrue(User.objects.get(pk=self.student.pk).is_active)

    def test_restore_active_does_not_automatically_restore_membership(self):
        self.academic({'academicStatus': StudentProfile.ACADEMIC_SUSPENDED})
        response = self.academic({'academicStatus': StudentProfile.ACADEMIC_ACTIVE})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data['currentMembership'])
        self.assertEqual(self.profile.memberships.count(), 1)

    def test_cross_major_and_year_change_closes_membership_and_preserves_school_number(self):
        major = Major.objects.create(department=self.department, code='NEW', name='转入专业')
        response = self.academic({'majorId': major.pk, 'enrollmentYear': 2024})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data['currentMembership'])
        self.assertEqual(response.data['major']['id'], major.pk)
        self.assertEqual(response.data['schoolNumber'], 'BASE0001')
        self.assertEqual(response.data['username'], 'BASE_STUDENT')

    def test_cross_major_change_can_explicitly_enroll_matching_new_class(self):
        major = Major.objects.create(department=self.department, code='NEXT', name='新专业')
        target = AdminClass.objects.create(major=major, name='新班', enrollment_year=2024)
        response = self.academic({'majorId': major.pk, 'enrollmentYear': 2024, 'adminClassId': target.pk})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['currentMembership']['adminClassId'], target.pk)
        self.assertEqual(self.profile.memberships.count(), 2)

    def test_bad_target_rolls_back_status_major_and_membership(self):
        major = Major.objects.create(department=self.department, code='BAD', name='目标专业')
        response = self.academic({'majorId': major.pk, 'adminClassId': self.adminClass.pk})
        self.assertEqual(response.status_code, 400)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.major_id, self.major.pk)
        self.assertTrue(self.profile.memberships.filter(pk=self.member.pk, left_at__isnull=True).exists())

    def test_academic_invalid_types_and_identity_fields_are_rejected(self):
        for payload in ({'academicStatus': []}, {'academicStatus': None},
                        {'academicStatus': '未知'}, {'enrollmentYear': True},
                        {'enrollmentYear': 0}, {'username': 'RENAME'}, {'schoolNumber': 'NEW'},
                        {'majorId': 0}, {'adminClassId': None}, {}):
            with self.subTest(payload=payload):
                self.assertEqual(self.academic(payload).status_code, 400)

    def test_nonactive_student_cannot_be_assigned_class_in_same_request(self):
        response = self.academic({'academicStatus': StudentProfile.ACADEMIC_GRADUATED,
                                  'adminClassId': self.adminClass.pk})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(self.profile.memberships.filter(left_at__isnull=True).exists())

    def test_future_member_refuses_academic_change_without_partial_write(self):
        self.member.joined_at = timezone.localdate() + timedelta(days=1)
        self.member.save(update_fields=['joined_at'])
        self.assertEqual(self.academic({'academicStatus': StudentProfile.ACADEMIC_SUSPENDED}).status_code, 400)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.academic_status, StudentProfile.ACADEMIC_ACTIVE)

    def test_academic_audit_failure_rolls_back_status_and_membership(self):
        with patch('academics.audit_service.recordAudit', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                updateStudentAcademic(self.office, self.profile, {'academicStatus': StudentProfile.ACADEMIC_DROPPED})
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.academic_status, StudentProfile.ACADEMIC_ACTIVE)
        self.assertTrue(self.profile.memberships.filter(left_at__isnull=True).exists())

    def test_transfer_and_leave_audit_member_change_in_same_transaction(self):
        target = AdminClass.objects.create(major=self.major, name='转入班', enrollment_year=2023)
        response = self.call(StudentTransferView, 'post', {'adminClassId': target.pk},
                             studentId=self.student.pk)
        self.assertEqual(response.status_code, 200)
        event = AuditEvent.objects.get(action='student.transfer')
        self.assertEqual(event.before['currentMembership']['adminClassId'], self.adminClass.pk)
        self.assertEqual(event.after['currentMembership']['adminClassId'], target.pk)
        response = self.call(StudentLeaveView, 'post', {}, studentId=self.student.pk)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(AuditEvent.objects.get(action='student.leave').after['currentMembership'])

    def test_leave_audit_failure_rolls_back_member_change(self):
        with patch('academics.audit_service.recordAudit', side_effect=RuntimeError('audit failed')):
            with self.assertRaises(RuntimeError):
                self.call(StudentLeaveView, 'post', {}, studentId=self.student.pk)
        self.assertTrue(self.profile.memberships.filter(left_at__isnull=True).exists())
