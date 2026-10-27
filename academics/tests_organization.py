"""教务组织调整的删除保护、M2M边界与事务回归。"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from teaching.models import ClassNotice, Homework, TeachingMaterial
from users.models import SchoolIdentity, StudentProfile
from .admin import TeachingClassForm
from .api import (AdminClassDetailView, CourseCollegeView, CourseDetailView,
                  DepartmentListView, TeachingClassDetailView, TeachingClassListView)
from .models import AdminClass, ClassMembership, CourseCollege, TeachingClass
from .services import (createTeachingClass, deleteAdminClass, deleteCourseCollege,
                       deleteTeachingClass, removeCandidateAdminClass,
                       removeTeachingAdminClass)
from .tests import OfficeApiTestBase


class OrganizationTests(OfficeApiTestBase):
    def addClass(self, name='另一行政班'):
        adminClass = AdminClass.objects.create(
            name=name, major=self.major, enrollment_year=2023)
        self.cc.admin_classes.add(adminClass)
        return adminClass

    def addHomework(self, teachingClass=None):
        return Homework.objects.create(
            teaching_class=teachingClass or self.teachingClass,
            title='历史作业', start_at=timezone.now() - timedelta(days=1),
            deadline=timezone.now() + timedelta(days=1), created_by=self.teacher)

    def test_teaching_class_safe_delete_keeps_accounts_and_admin_class(self):
        deleteTeachingClass(self.teachingClass)
        self.assertFalse(TeachingClass.objects.filter(pk=self.teachingClass.pk).exists())
        self.assertTrue(AdminClass.objects.filter(pk=self.adminClass.pk).exists())
        self.assertTrue(self.cc.teachers.filter(pk=self.teacher.pk).exists())

    def test_each_teaching_record_prevents_teaching_class_deletion(self):
        for index, kind in enumerate(('homework', 'material', 'notice')):
            with self.subTest(kind=kind):
                teachingClass = createTeachingClass(
                    self.cc, f'受保护教学班{index}', f'历史学期{index}', [self.adminClass])
                if kind == 'homework':
                    self.addHomework(teachingClass)
                elif kind == 'material':
                    TeachingMaterial.objects.create(
                        teaching_class=teachingClass, title='私有资料', storage_name='unused.pdf',
                        original_name='资料.pdf', size=1, uploaded_by=self.teacher)
                else:
                    ClassNotice.objects.create(teaching_class=teachingClass,
                                               title='历史通知', content='正文',
                                               created_by=self.teacher, updated_by=self.teacher)
                with self.assertRaises(ValidationError):
                    deleteTeachingClass(teachingClass)
                self.assertTrue(TeachingClass.objects.filter(pk=teachingClass.pk).exists())
                self.assertTrue(teachingClass.admin_classes.filter(pk=self.adminClass.pk).exists())

    def test_course_college_failure_rolls_back_all_classes_and_leader(self):
        self.cc.leader = self.teacher
        self.cc.save()
        empty = createTeachingClass(self.cc, '空教学班', '另一学期', [self.adminClass])
        self.addHomework()
        beforeIds = set(self.cc.teaching_classes.values_list('pk', flat=True))
        with self.assertRaises(ValidationError):
            deleteCourseCollege(self.cc)
        self.assertTrue(CourseCollege.objects.filter(pk=self.cc.pk).exists())
        self.assertEqual(set(self.cc.teaching_classes.values_list('pk', flat=True)), beforeIds)
        self.assertTrue(empty.admin_classes.filter(pk=self.adminClass.pk).exists())
        self.assertTrue(self.teacher.groups.filter(name='课程组').exists())
        self.assertTrue(self.cc.teachers.filter(pk=self.teacher.pk).exists())

    def test_course_college_safe_delete_removes_only_teaching_organization(self):
        self.cc.leader = self.teacher
        self.cc.save()
        deleteCourseCollege(self.cc)
        self.assertFalse(CourseCollege.objects.filter(pk=self.cc.pk).exists())
        self.assertFalse(TeachingClass.objects.filter(pk=self.teachingClass.pk).exists())
        self.assertTrue(AdminClass.objects.filter(pk=self.adminClass.pk).exists())
        self.assertTrue(self.teacher.groups.filter(name='教师').exists())
        self.assertFalse(self.teacher.groups.filter(name='课程组').exists())

    def test_idle_admin_class_can_be_deleted_with_candidate_membership(self):
        idle = self.addClass()
        pk = idle.pk
        deleteAdminClass(idle)
        self.assertFalse(AdminClass.objects.filter(pk=pk).exists())
        self.assertFalse(self.cc.admin_classes.filter(pk=pk).exists())

    def test_admin_class_in_teaching_class_cannot_be_deleted_by_any_entry(self):
        for call in (lambda: deleteAdminClass(self.adminClass),
                     lambda: self.adminClass.delete(),
                     lambda: AdminClass.objects.filter(pk=self.adminClass.pk).delete()):
            with self.assertRaises(ValidationError), transaction.atomic():
                call()
            self.assertTrue(self.teachingClass.admin_classes.filter(pk=self.adminClass.pk).exists())

    def test_current_or_historical_members_prevent_admin_class_hard_delete(self):
        identity = SchoolIdentity.objects.create(
            user=self.student, school_number='ORG-STUDENT', identity_type='student')
        profile = StudentProfile.objects.create(identity=identity, major=self.major,
                                                 enrollment_year=2023)
        today = timezone.localdate()
        for leftAt in (None, today):
            with self.subTest(left_at=leftAt):
                idle = self.addClass(f'历史班{leftAt}')
                membership = ClassMembership.objects.create(
                    student_profile=profile, admin_class=idle, joined_at=today, left_at=leftAt)
                for call in (lambda: deleteAdminClass(idle), lambda: idle.delete()):
                    with self.assertRaises(ValidationError), transaction.atomic():
                        call()
                self.assertTrue(ClassMembership.objects.filter(pk=membership.pk).exists())
                if leftAt is None:
                    membership.left_at = today
                    membership.save(update_fields=['left_at'])

    def test_candidate_pool_cannot_remove_used_class_from_either_side(self):
        calls = (
            lambda: removeCandidateAdminClass(self.cc, self.adminClass),
            lambda: self.cc.admin_classes.remove(self.adminClass),
            lambda: self.cc.admin_classes.clear(),
            lambda: self.adminClass.course_colleges.remove(self.cc),
            lambda: self.adminClass.course_colleges.clear(),
        )
        for call in calls:
            with self.assertRaises(ValidationError), transaction.atomic():
                call()
            self.assertTrue(self.cc.admin_classes.filter(pk=self.adminClass.pk).exists())
            self.assertTrue(self.teachingClass.admin_classes.filter(pk=self.adminClass.pk).exists())

    def test_unused_candidate_can_be_removed_idempotently(self):
        idle = self.addClass()
        removeCandidateAdminClass(self.cc, idle)
        removeCandidateAdminClass(self.cc, idle)
        self.assertFalse(self.cc.admin_classes.filter(pk=idle.pk).exists())
        self.assertTrue(self.cc.admin_classes.filter(pk=self.adminClass.pk).exists())
        self.assertTrue(AdminClass.objects.filter(pk=idle.pk).exists())

    def test_teaching_class_cannot_lose_its_last_member_from_either_side(self):
        calls = (
            lambda: removeTeachingAdminClass(self.teachingClass, self.adminClass),
            lambda: self.teachingClass.admin_classes.remove(self.adminClass),
            lambda: self.teachingClass.admin_classes.clear(),
            lambda: self.adminClass.teaching_classes.remove(self.teachingClass),
            lambda: self.adminClass.teaching_classes.clear(),
        )
        for call in calls:
            with self.assertRaises(ValidationError), transaction.atomic():
                call()
            self.assertEqual(self.teachingClass.admin_classes.count(), 1)

    def test_member_removal_rechecks_remaining_members_for_stale_class(self):
        second = self.addClass()
        self.teachingClass.admin_classes.add(second)
        stale = TeachingClass.objects.get(pk=self.teachingClass.pk)
        removeTeachingAdminClass(self.teachingClass, self.adminClass)
        with self.assertRaises(ValidationError):
            removeTeachingAdminClass(stale, second)
        self.assertEqual(list(stale.admin_classes.all()), [second])
        removeCandidateAdminClass(self.cc, self.adminClass)
        self.assertFalse(self.cc.admin_classes.filter(pk=self.adminClass.pk).exists())

    def test_reverse_bulk_clear_failure_leaves_all_relations_unchanged(self):
        second = self.addClass()
        self.teachingClass.admin_classes.add(second)
        other = createTeachingClass(self.cc, '跨学期班', '另一学期', [self.adminClass])
        with self.assertRaises(ValidationError), transaction.atomic():
            self.adminClass.teaching_classes.clear()
        self.assertTrue(self.teachingClass.admin_classes.filter(pk=self.adminClass.pk).exists())
        self.assertTrue(other.admin_classes.filter(pk=self.adminClass.pk).exists())

    def test_removed_unused_candidate_does_not_block_other_college_usage(self):
        idle = self.addClass()
        other = CourseCollege.objects.create(course=self.course, department=self.otherDepartment)
        # 历史错误候选池关联不会改变“本课程×学院正在使用”的判断范围。
        CourseCollege.admin_classes.through.objects.create(coursecollege=other, adminclass=idle)
        idle.course_colleges.remove(other)
        self.assertTrue(self.cc.admin_classes.filter(pk=idle.pk).exists())

    def test_admin_form_replaces_composing_class_without_leaving_empty_class(self):
        target = self.addClass()
        form = TeachingClassForm(instance=self.teachingClass, data={
            'name': self.teachingClass.name, 'term': self.teachingClass.term,
            'course_college': self.cc.pk, 'admin_classes': [target.pk],
            'teachers': [self.teacher.pk]})
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(list(self.teachingClass.admin_classes.all()), [target])


class OrganizationApiTests(OfficeApiTestBase):
    def test_protected_deletions_return_business_error_and_keep_relations(self):
        homework = Homework.objects.create(
            teaching_class=self.teachingClass, title='保留的作业',
            start_at=timezone.now(), deadline=timezone.now(), created_by=self.teacher)
        response = self._call(TeachingClassDetailView, self.adminUser, method='delete',
                              teachingClassId=self.teachingClass.pk)
        self.assertEqual(response.status_code, 400, response.data)
        response = self._call(CourseDetailView, self.adminUser, method='post',
                              courseId=self.course.pk,
                              data={'action': 'removeDepartment', 'departmentId': self.department.pk})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertTrue(CourseCollege.objects.filter(pk=self.cc.pk).exists())
        self.assertTrue(Homework.objects.filter(pk=homework.pk).exists())
        self.assertTrue(self.teachingClass.admin_classes.filter(pk=self.adminClass.pk).exists())

    def test_used_candidate_and_last_composing_class_cannot_be_removed(self):
        response = self._call(CourseCollegeView, self.adminUser, method='post',
                              courseId=self.course.pk, departmentId=self.department.pk,
                              data={'action': 'removeAdminClass', 'adminClassId': self.adminClass.pk})
        self.assertEqual(response.status_code, 400, response.data)
        response = self._call(TeachingClassDetailView, self.adminUser, method='post',
                              teachingClassId=self.teachingClass.pk,
                              data={'action': 'removeAdminClass', 'adminClassId': self.adminClass.pk})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertTrue(self.cc.admin_classes.filter(pk=self.adminClass.pk).exists())
        self.assertEqual(self.teachingClass.admin_classes.count(), 1)

    def test_grouping_rejects_malformed_ids_before_writing(self):
        payload = {'courseCollegeId': self.cc.pk, 'name': '不能创建的班',
                   'term': '2027-2028-1', 'adminClassIds': [self.adminClass.pk]}
        invalid = (None, 1, True, {}, '1', [None], [True], [{}], [0], [2 ** 63],
                   [self.adminClass.pk, str(self.adminClass.pk)])
        for value in invalid:
            with self.subTest(value=value):
                response = self._call(TeachingClassListView, self.adminUser, method='post',
                                      data={**payload, 'adminClassIds': value})
                self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(TeachingClass.objects.filter(name=payload['name']).exists())

    def test_grouping_rejects_non_object_unknown_fields_and_invalid_text(self):
        payload = {'courseCollegeId': self.cc.pk, 'name': '不能创建的班',
                   'term': '2027-2028-1', 'adminClassIds': [self.adminClass.pk]}
        invalid = ([], {**payload, 'name': None}, {**payload, 'name': True},
                   {**payload, 'name': 'a' * 51}, {**payload, 'name': 'a\x00b'},
                   {**payload, 'term': None}, {**payload, 'term': 'a' * 21},
                   {**payload, 'createdById': self.teacher.pk},
                   {**payload, 'courseCollegeId': {}})
        for value in invalid:
            with self.subTest(value=value):
                response = self._call(TeachingClassListView, self.adminUser, method='post', data=value)
                self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(TeachingClass.objects.filter(name=payload['name']).exists())

    def test_teaching_class_filters_term_and_validates_course_filter(self):
        another = createTeachingClass(self.cc, '下学期班', '2027-2028-1', [self.adminClass])
        response = self._call(TeachingClassListView, self.adminUser, data={'term': another.term})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([item['id'] for item in response.data], [another.pk])
        for query in ({'cc': 'bad'}, {'cc': '9223372036854775808'}, {'term': 'a' * 21}, {'term': ''}):
            response = self._call(TeachingClassListView, self.adminUser, data=query)
            self.assertEqual(response.status_code, 400, response.data)

    def test_action_ids_and_payloads_cannot_raise_unhandled_model_errors(self):
        for data in ([], {'action': 'addTeacher', 'teacherId': {}},
                     {'action': 'removeTeacher', 'teacherId': True},
                     {'action': 'addAdminClass', 'adminClassId': 'bad'},
                     {'action': 'addTeacher', 'teacherNo': self.teacher.username,
                      'teacherId': self.teacher.pk}):
            with self.subTest(data=data):
                response = self._call(TeachingClassDetailView, self.adminUser, method='post',
                                      teachingClassId=self.teachingClass.pk, data=data)
                self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self.teachingClass.teachers.count(), 1)

    def test_current_counts_ignore_previous_memberships_and_include_department_classes(self):
        today = timezone.localdate()
        identity = SchoolIdentity.objects.create(
            user=self.student, school_number='COUNT-STUDENT', identity_type='student')
        profile = StudentProfile.objects.create(identity=identity, major=self.major, enrollment_year=2023)
        ClassMembership.objects.create(student_profile=profile, admin_class=self.adminClass,
                                       joined_at=today - timedelta(days=2), left_at=today)
        ClassMembership.objects.create(student_profile=profile, admin_class=self.adminClass, joined_at=today)
        AdminClass.objects.create(name='未开课行政班', major=self.major, enrollment_year=2023)
        response = self._call(DepartmentListView, self.adminUser)
        department = next(item for item in response.data if item['id'] == self.department.pk)
        self.assertEqual(department['adminClassCount'], 2)
        response = self._call(CourseCollegeView, self.adminUser, courseId=self.course.pk,
                              departmentId=self.department.pk)
        self.assertEqual(response.data['admin_classes'][0]['studentCount'], 1)
        response = self._call(TeachingClassDetailView, self.adminUser,
                              teachingClassId=self.teachingClass.pk)
        self.assertEqual(response.data['studentCount'], 1)
        self.assertEqual(response.data['admin_classes'][0]['studentCount'], 1)
        response = self._call(AdminClassDetailView, self.adminUser, adminClassId=self.adminClass.pk)
        self.assertEqual(response.data['studentCount'], 1)
        self.assertEqual(len(response.data['students']), 1)
