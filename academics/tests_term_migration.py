"""从上一版教务表迁移，既有组织和任课数据保留，旧学期自动登记。"""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class AcademicTermMigrationTests(TransactionTestCase):
    def test_existing_terms_and_organization_survive_upgrade(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        target = [('academics', '0010_membership_constraints')]
        try:
            executor.migrate(target)
            old = executor.loader.project_state(target).apps
            department = old.get_model('academics', 'Department').objects.create(code='LEGACY', name='原学院')
            major = old.get_model('academics', 'Major').objects.create(
                code='LEGACY', name='原专业', department=department)
            adminClass = old.get_model('academics', 'AdminClass').objects.create(
                name='原行政班', major=major, enrollment_year=2023)
            course = old.get_model('academics', 'Course').objects.create(code='LEGACY', name='原课程')
            cc = old.get_model('academics', 'CourseCollege').objects.create(course=course, department=department)
            cc.admin_classes.add(adminClass)
            tc = old.get_model('academics', 'TeachingClass').objects.create(
                name='原教学班', term='旧学期编码', course_college=cc)
            tc.admin_classes.add(adminClass)
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            new = executor.loader.project_state(latest).apps
            Term = new.get_model('academics', 'AcademicTerm')
            self.assertEqual(Term.objects.get(code='旧学期编码').status, 'open')
            self.assertIsNone(Term.objects.get(code='旧学期编码').start_date)
            self.assertEqual(Term.objects.filter(is_current=True).get().code, '2026-2027-1')
            self.assertTrue(new.get_model('academics', 'Course').objects.get(pk=course.pk).is_active)
            self.assertTrue(new.get_model('academics', 'AdminClass').objects.get(pk=adminClass.pk).is_active)
            current = new.get_model('academics', 'TeachingClass').objects.get(pk=tc.pk)
            self.assertEqual(current.term, '旧学期编码')
            self.assertEqual(list(current.admin_classes.values_list('pk', flat=True)), [adminClass.pk])
            self.assertFalse(new.get_model('academics', 'TeachingClassRecord').objects.exists())
        finally:
            MigrationExecutor(connection).migrate(latest)
