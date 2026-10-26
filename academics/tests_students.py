"""学生管理的功能权限、当前名单、转退班与教学访问联动回归。"""
import json
from datetime import date, timedelta

from django.contrib.auth.models import Group, Permission
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from academics.models import AdminClass, ClassMembership, Department, Major
from academics.student_api import (StudentDetailView, StudentLeaveView,
    StudentListView, StudentMembershipListView, StudentTransferView, filterStudents,
    studentProfiles)
from teaching.models import (ClassNotice, Homework, HomeworkProblem, Problem,
                             Submission, TeachingMaterial)
from teaching.tests import ObjectPermissionTestBase
from users.models import SchoolIdentity, StudentProfile, User


class StudentManagementTests(ObjectPermissionTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.office = User.objects.create_user(username='OFFICE_STUDENTS')
        cls.office.groups.add(Group.objects.get(name='教务'))
        cls.major = cls.myProfile.major
        cls.department = cls.major.department
        cls.thirdClass = AdminClass.objects.create(
            major=cls.major, name='第三行政班', enrollment_year=2023)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.office)
        self.studentUrl = f'/api/students/{self.student.pk}/'

    def callView(self, view, user, method='get', data=None, **kwargs):
        request = getattr(APIRequestFactory(), method)('/api/test/', data, format='json')
        if user is not None:
            force_authenticate(request, user=user)
        return view.as_view()(request, **kwargs)

    def transfer(self, target=None, **extra):
        payload = {'adminClassId': (target or self.otherAdminClass).pk, **extra}
        return self.client.post(self.studentUrl + 'transfer/', payload, format='json')

    def leave(self):
        return self.client.post(self.studentUrl + 'leave/', {}, format='json')

    def resultIds(self, **params):
        response = self.client.get('/api/students/', params)
        self.assertEqual(response.status_code, 200, response.data)
        return [row['id'] for row in response.data['results']]

    def test_student_fields_are_explicit_and_use_user_primary_key(self):
        response = self.client.get(self.studentUrl)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(response.data), {
            'id', 'username', 'name', 'schoolNumber', 'major', 'enrollmentYear',
            'academicStatus', 'currentMembership'})
        self.assertEqual(response.data['id'], self.student.pk)
        self.assertEqual(response.data['schoolNumber'], self.myProfile.identity.school_number)
        self.assertEqual(response.data['major']['department']['id'], self.department.pk)
        self.assertEqual(set(response.data['currentMembership']), {
            'id', 'adminClassId', 'adminClassName', 'joinedAt'})
        serialized = json.dumps(response.data)
        for secret in ('password', 'verification', 'email', 'is_superuser', 'permissions'):
            self.assertNotIn(secret, serialized)

    def test_all_views_require_class_manage(self):
        cases = (
            (StudentListView, 'get', None, {}),
            (StudentDetailView, 'get', None, {'studentId': self.student.pk}),
            (StudentMembershipListView, 'get', None, {'studentId': self.student.pk}),
            (StudentTransferView, 'post', {'adminClassId': self.otherAdminClass.pk},
             {'studentId': self.student.pk}),
            (StudentLeaveView, 'post', {}, {'studentId': self.student.pk}))
        for user in (self.teacher, self.courseTeam, self.student, None):
            for view, method, data, kwargs in cases:
                with self.subTest(user=user, view=view.__name__):
                    response = self.callView(view, user, method, data, **kwargs)
                    self.assertIn(response.status_code, (401, 403))
        self.assertEqual(self.myProfile.memberships.filter(left_at__isnull=True).count(), 1)

    def test_superuser_and_explicit_function_permission_are_allowed(self):
        bare = User.objects.create_user(username='STUDENT_MANAGE_ONLY')
        bare.user_permissions.add(Permission.objects.get(
            content_type__app_label='users', codename='class_manage'))
        for user in (bare, self.superUser):
            self.assertEqual(self.callView(StudentListView, user).status_code, 200)

    def test_missing_profile_or_nonstudent_identity_is_not_managed(self):
        noProfile = User.objects.create_user(username='NO_STUDENT_PROFILE')
        SchoolIdentity.objects.create(user=noProfile, school_number='NO_PROFILE',
                                     identity_type=SchoolIdentity.IDENTITY_STUDENT)
        wrong = SchoolIdentity.objects.create(user=self.teacher, school_number='WRONG_TYPE',
                                             identity_type=SchoolIdentity.IDENTITY_TEACHER)
        StudentProfile.objects.create(identity=wrong, major=self.major, enrollment_year=2023)
        self.assertEqual(set(self.resultIds()), {self.student.pk, self.student2.pk})
        missing = self.client.get('/api/students/99999999/')
        for user in (noProfile, self.teacher, self.office):
            response = self.client.get(f'/api/students/{user.pk}/')
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.data, missing.data)

    def test_first_student_identity_matches_student_permission_selection(self):
        later = SchoolIdentity.objects.create(user=self.student, school_code='SECOND',
            school_number='LATER_PROFILE', identity_type=SchoolIdentity.IDENTITY_STUDENT)
        StudentProfile.objects.create(identity=later, major=self.major, enrollment_year=2023)
        self.assertEqual(self.resultIds().count(self.student.pk), 1)
        self.assertEqual(self.client.get(self.studentUrl).data['schoolNumber'],
                         self.myProfile.identity.school_number)
        self.myProfile.delete()
        self.assertNotIn(self.student.pk, self.resultIds())
        self.assertEqual(self.client.get(self.studentUrl).status_code, 404)

    def test_search_by_name_username_and_school_number(self):
        self.student.name = '张同学'
        self.student.save(update_fields=['name'])
        for query in ('张同学', self.student.username, self.myProfile.identity.school_number):
            self.assertEqual(self.resultIds(q=query), [self.student.pk])

    def test_filters_use_current_membership_not_departed_history(self):
        ClassMembership.objects.create(student_profile=self.myProfile,
            admin_class=self.otherAdminClass, joined_at=date(2021, 9, 1),
            left_at=date(2022, 8, 1))
        self.assertEqual(self.resultIds(adminClass=self.otherAdminClass.pk), [self.student2.pk])
        self.assertEqual(set(self.resultIds(major=self.major.pk, department=self.department.pk)),
                         {self.student.pk, self.student2.pk})
        self.assertEqual(self.resultIds(inClass='false'), [])
        self.assertEqual(self.leave().status_code, 200)
        self.assertEqual(self.resultIds(inClass='false'), [self.student.pk])
        self.assertEqual(self.resultIds(inClass='true'), [self.student2.pk])
        self.assertEqual(self.resultIds(adminClass=self.myAdminClass.pk), [])

    def test_academic_status_filter_does_not_change_current_membership(self):
        self.myProfile.academic_status = StudentProfile.ACADEMIC_SUSPENDED
        self.myProfile.save(update_fields=['academic_status'])
        self.assertEqual(self.resultIds(academicStatus=StudentProfile.ACADEMIC_SUSPENDED),
                         [self.student.pk])
        self.assertEqual(self.resultIds(academicStatus=StudentProfile.ACADEMIC_ACTIVE),
                         [self.student2.pk])

    def test_malformed_filter_ids_status_boolean_and_search_return_400(self):
        for key in ('adminClass', 'major', 'department'):
            for value in ('0', '-1', '1.5', 'bad', '9' * 30, 'true'):
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.client.get('/api/students/', {key: value}).status_code, 400)
        for params in ({'inClass': '1'}, {'inClass': ''}, {'academicStatus': 'unknown'},
                       {'academicStatus': ''}, {'q': 'x' * 101}, {'q': '\x00'}):
            self.assertEqual(self.client.get('/api/students/', params).status_code, 400)
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            filterStudents(studentProfiles(), {'q': '\ud800'})

    def test_list_pagination_and_constant_query_count(self):
        for index in range(21):
            user = User.objects.create_user(username=f'BULK_STUDENT_{index:02}')
            identity = SchoolIdentity.objects.create(user=user, school_number=f'BULK_{index:02}',
                identity_type=SchoolIdentity.IDENTITY_STUDENT)
            profile = StudentProfile.objects.create(identity=identity, major=self.major,
                                                     enrollment_year=2023)
            ClassMembership.objects.create(student_profile=profile, admin_class=self.myAdminClass,
                                            joined_at=date(2023, 9, 1))
        self.office.get_all_permissions()
        with CaptureQueriesContext(connection) as queries:
            response = self.callView(StudentListView, self.office)
        self.assertEqual(response.data['count'], 23)
        self.assertEqual(len(response.data['results']), 20)
        self.assertIsNotNone(response.data['next'])
        self.assertLessEqual(len(queries), 4)
        self.assertEqual(len(self.client.get('/api/students/', {'page': 2}).data['results']), 3)

    def test_history_is_paginated_and_excludes_other_students(self):
        for index in range(21):
            ClassMembership.objects.create(student_profile=self.myProfile,
                admin_class=self.otherAdminClass, joined_at=date(2000 + index, 1, 1),
                left_at=date(2000 + index, 2, 1))
        response = self.client.get(self.studentUrl + 'memberships/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 22)
        self.assertEqual(len(response.data['results']), 20)
        self.assertEqual(set(response.data['results'][0]), {
            'id', 'adminClassId', 'adminClassName', 'joinedAt', 'leftAt'})
        self.assertIsNone(response.data['results'][0]['leftAt'])
        self.assertEqual(self.client.get(
            f'/api/students/{self.student2.pk}/memberships/').data['count'], 1)

    def test_transfer_closes_old_relation_today_and_is_idempotent(self):
        old = self.myProfile.memberships.get(left_at__isnull=True)
        originalStatus = self.myProfile.academic_status
        response = self.transfer()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['currentMembership']['adminClassId'], self.otherAdminClass.pk)
        old.refresh_from_db()
        self.assertEqual(old.left_at, timezone.localdate())
        current = self.myProfile.memberships.get(left_at__isnull=True)
        self.assertEqual(current.joined_at, timezone.localdate())
        self.assertEqual(self.transfer().status_code, 200)
        self.assertEqual(self.myProfile.memberships.count(), 2)
        self.myProfile.refresh_from_db()
        self.assertEqual(self.myProfile.academic_status, originalStatus)

    def test_leave_is_idempotent_and_can_reenroll(self):
        first = self.leave()
        self.assertEqual(first.status_code, 200, first.data)
        self.assertIsNone(first.data['currentMembership'])
        self.assertEqual(self.leave().status_code, 200)
        self.assertEqual(self.myProfile.memberships.count(), 1)
        self.assertTrue(User.objects.filter(pk=self.student.pk, is_active=True).exists())
        self.assertEqual(self.transfer().status_code, 200)
        self.assertEqual(self.myProfile.memberships.filter(left_at__isnull=True).count(), 1)

    def test_transfer_rejects_other_major_year_or_disabled_organization(self):
        otherDepartment = Department.objects.create(code='STU_OTHER', name='其他学院')
        otherMajor = Major.objects.create(department=otherDepartment, code='STU_OTHER', name='其他专业')
        for major, year in ((otherMajor, 2023), (self.major, 2024), (self.major, None)):
            target = AdminClass.objects.create(major=major, name=f'不能转入{year}', enrollment_year=year)
            self.assertEqual(self.transfer(target).status_code, 400)
        self.major.is_active = False
        self.major.save(update_fields=['is_active'])
        self.assertEqual(self.transfer().status_code, 400)
        self.major.is_active = True
        self.major.save(update_fields=['is_active'])
        self.department.is_active = False
        self.department.save(update_fields=['is_active'])
        self.assertEqual(self.transfer().status_code, 400)
        self.assertEqual(self.myProfile.memberships.get(left_at__isnull=True).admin_class,
                         self.myAdminClass)

    def test_transfer_rejects_unknown_fields_dates_and_bad_ids_without_change(self):
        invalid = ([], {}, {'adminClassId': 1, 'joinedAt': '2020-01-01'},
                   {'adminClassId': 1, 'leftAt': '2020-01-01'},
                   {'adminClassId': 1, 'academicStatus': StudentProfile.ACADEMIC_DROPPED})
        for payload in invalid:
            self.assertEqual(self.client.post(self.studentUrl + 'transfer/', payload,
                                              format='json').status_code, 400)
        for value in (True, 0, -1, 1.5, 'no', [], {}, '9' * 30, '\ud800'):
            payload = json.dumps({'adminClassId': value})
            response = self.client.post(self.studentUrl + 'transfer/', payload,
                                        content_type='application/json')
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.myProfile.memberships.count(), 1)

    def test_leave_rejects_nonobject_or_client_supplied_dates(self):
        for payload in ([], ['x'], {'leftAt': '2020-01-01'}, {'studentId': self.student2.pk},
                        {'academicStatus': StudentProfile.ACADEMIC_DROPPED}):
            self.assertEqual(self.client.post(self.studentUrl + 'leave/', payload,
                                              format='json').status_code, 400)
        self.assertEqual(self.myProfile.memberships.filter(left_at__isnull=True).count(), 1)

    def test_missing_students_are_consistent_for_all_operations(self):
        base = '/api/students/99999999/'
        for suffix, method, payload in (('', 'get', None), ('memberships/', 'get', None),
                ('transfer/', 'post', {'adminClassId': self.otherAdminClass.pk}),
                ('leave/', 'post', {})):
            response = getattr(self.client, method)(base + suffix, payload, format='json')
            self.assertEqual(response.status_code, 404)
            self.assertEqual(str(response.data['detail']), '学生不存在')
        self.assertEqual(self.transfer(target=AdminClass(pk=99999999)).status_code, 404)

    def test_future_current_membership_refuses_transfer_and_leave(self):
        current = self.myProfile.memberships.get(left_at__isnull=True)
        current.joined_at = timezone.localdate() + timedelta(days=1)
        current.save(update_fields=['joined_at'])
        self.assertEqual(self.transfer().status_code, 400)
        self.assertEqual(self.leave().status_code, 400)
        current.refresh_from_db()
        self.assertIsNone(current.left_at)

    def makeTeachingResources(self):
        now = timezone.now()
        problem = Problem.objects.create(title='转班权限测试题', owner=self.teacher,
            languages=['python'], tests=[{'input': '', 'output': '1', 'points': 100}])
        resources = []
        for teachingClass in (self.myTeachingClass, self.otherTeachingClass):
            homework = Homework.objects.create(teaching_class=teachingClass, title='转班作业',
                start_at=now - timedelta(days=1), deadline=now + timedelta(days=1),
                created_by=self.teacher)
            HomeworkProblem.objects.create(homework=homework, problem=problem, order=0,
                                            problem_revision=problem.revision)
            notice = ClassNotice.objects.create(teaching_class=teachingClass,
                title='转班通知', content='通知正文', created_by=self.teacher, updated_by=self.teacher)
            material = TeachingMaterial.objects.create(teaching_class=teachingClass,
                title='转班资料', storage_name='no-disk-read', original_name='讲义.txt', size=1,
                uploaded_by=self.teacher)
            resources.append((homework, notice, material))
        return problem, resources

    def assertStudentResources(self, resources, status):
        homework, notice, material = resources
        self.assertEqual(self.client.get(f'/api/homeworks/{homework.pk}/').status_code, status)
        self.assertEqual(self.client.get(f'/api/notices/{notice.pk}/').status_code, status)
        self.assertEqual(self.client.get(
            f'/api/teaching-classes/{material.teaching_class_id}/materials/').status_code, status)

    def test_transfer_immediately_updates_access_and_preserves_old_submission(self):
        problem, resources = self.makeTeachingResources()
        submission = Submission.objects.create(user=self.student, problem=problem,
            homework=resources[0][0], submit_type=Submission.SUBMIT_HOMEWORK,
            language='python', code='print(1)', status=Submission.STATUS_ACCEPTED,
            score=100, total_score=100, problem_revision=1, tests_hash='hash', judge_snapshot={})
        self.client.force_authenticate(self.student)
        self.assertStudentResources(resources[0], 200)
        self.assertStudentResources(resources[1], 404)
        self.client.force_authenticate(self.office)
        self.assertEqual(self.transfer().status_code, 200)
        self.client.force_authenticate(self.student)
        self.assertStudentResources(resources[0], 404)
        self.assertStudentResources(resources[1], 200)
        self.assertTrue(Submission.objects.filter(pk=submission.pk, score=100).exists())
        self.assertEqual(self.client.get(f'/api/submissions/{submission.pk}/').status_code, 200)
        self.client.force_authenticate(self.office)
        self.assertEqual(self.leave().status_code, 200)
        self.client.force_authenticate(self.student)
        self.assertStudentResources(resources[1], 404)
        self.assertEqual(self.client.get(f'/api/submissions/{submission.pk}/').status_code, 200)
