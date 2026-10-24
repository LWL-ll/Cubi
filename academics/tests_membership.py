"""成员唯一性、转班历史及所有组班入口的回归测试。"""
from datetime import date
from importlib import import_module

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from users.models import SchoolIdentity, StudentProfile, User
from .admin import TeachingClassForm
from .models import (AdminClass, ClassMembership, Course, CourseCollege,
                     Department, Major, TeachingClass)
from .services import createTeachingClass, enrollStudent


class MembershipTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        department = Department.objects.create(code='MEM', name='成员学院')
        major = Major.objects.create(department=department, code='M', name='专业')
        cls.a = AdminClass.objects.create(name='A', major=major, enrollment_year=2026)
        cls.b = AdminClass.objects.create(name='B', major=major, enrollment_year=2026)
        user = User.objects.create_user(username='member-student')
        identity = SchoolIdentity.objects.create(user=user, school_number='M1',
                                                identity_type='student')
        cls.profile = StudentProfile.objects.create(identity=identity, major=major,
                                                    enrollment_year=2026)
        cls.course = Course.objects.create(code='MEM', name='课程')
        cls.cc = CourseCollege.objects.create(course=cls.course, department=department)
        cls.cc.admin_classes.add(cls.a, cls.b)

    def test_transfer_is_atomic_and_preserves_history(self):
        first = enrollStudent(self.profile, self.a, date(2026, 9, 1))
        self.assertEqual(enrollStudent(self.profile, self.a).pk, first.pk)
        with self.assertRaises(ValidationError):
            enrollStudent(self.profile, self.b, date(2026, 8, 1))
        first.refresh_from_db()
        self.assertIsNone(first.left_at)
        second = enrollStudent(self.profile, self.b, date(2026, 10, 1))
        first.refresh_from_db()
        self.assertEqual(first.left_at, second.joined_at)
        self.assertEqual(self.profile.memberships.filter(left_at__isnull=True).count(), 1)
        self.assertEqual(self.profile.memberships.count(), 2)

    def test_database_rejects_duplicate_current_membership(self):
        enrollStudent(self.profile, self.a)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClassMembership.objects.create(student_profile=self.profile,
                                           admin_class=self.b, joined_at=date.today())

    def test_database_rejects_reversed_dates(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClassMembership.objects.create(student_profile=self.profile,
                                           admin_class=self.a, joined_at=date(2026, 10, 1),
                                           left_at=date(2026, 9, 1))

    def test_migration_preflight_preserves_valid_data(self):
        migration = import_module('academics.migrations.0010_membership_constraints')
        enrollStudent(self.profile, self.a)
        class SchemaEditor:
            pass
        editor = SchemaEditor()
        editor.connection = connection
        migration.checkMemberships(apps, editor)
        self.assertEqual(ClassMembership.objects.count(), 1)

    def test_grouping_conflict_rolls_back_new_class(self):
        createTeachingClass(self.cc, '一班', 'T1', [self.a])
        with self.assertRaises(ValidationError):
            createTeachingClass(self.cc, '重复', 'T1', [self.a, self.b])
        self.assertFalse(TeachingClass.objects.filter(name='重复').exists())
        # 同一行政班跨学期或跨课程可再次组班。
        createTeachingClass(self.cc, '下学期', 'T2', [self.a])
        otherCourse = Course.objects.create(code='MEM2', name='另一课程')
        otherCc = CourseCollege.objects.create(course=otherCourse, department=self.cc.department)
        otherCc.admin_classes.add(self.a)
        createTeachingClass(otherCc, '另一课', 'T1', [self.a])

    def test_direct_and_reverse_m2m_cannot_bypass(self):
        first = createTeachingClass(self.cc, '一班', 'T1', [self.a])
        second = createTeachingClass(self.cc, '二班', 'T1', [self.b])
        with self.assertRaises(ValidationError), transaction.atomic():
            second.admin_classes.add(self.a)
        with self.assertRaises(ValidationError), transaction.atomic():
            self.a.teaching_classes.add(second)
        self.assertEqual(list(first.admin_classes.all()), [self.a])
        self.assertEqual(list(second.admin_classes.all()), [self.b])

    def test_changing_term_cannot_bypass(self):
        createTeachingClass(self.cc, '一班', 'T1', [self.a])
        second = createTeachingClass(self.cc, '二班', 'T2', [self.a])
        second.term = 'T1'
        with self.assertRaises(ValidationError), transaction.atomic():
            second.save()
        second.refresh_from_db()
        self.assertEqual(second.term, 'T2')

    def test_admin_form_reports_duplicate_grouping(self):
        createTeachingClass(self.cc, '一班', 'T1', [self.a])
        form = TeachingClassForm(data={'name': '二班', 'term': 'T1',
                                      'course_college': self.cc.pk,
                                      'admin_classes': [self.a.pk]})
        self.assertFalse(form.is_valid())
        self.assertIn('重复', str(form.errors))
