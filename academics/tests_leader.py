"""组长更换、撤销和级联删除后的课程组权限生命周期。"""
from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from users.models import User
from teaching.models import Homework
from .models import Course, CourseCollege, Department, TeachingClass
from .views import setCourseCollegeLeader, unsetCourseCollegeLeader


class CourseLeaderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = User.objects.create_user(username='leader-a')
        cls.b = User.objects.create_user(username='leader-b')
        teacherGroup = Group.objects.get(name='教师')
        cls.a.groups.add(teacherGroup)
        cls.b.groups.add(teacherGroup)
        cls.department = Department.objects.create(code='LEADER', name='学院')
        cls.course = Course.objects.create(code='LEADER', name='课程')
        cls.cc = CourseCollege.objects.create(course=cls.course, department=cls.department)

    def hasCourseTeam(self, user):
        return user.groups.filter(name='课程组').exists()

    def test_replace_reclaims_previous_leader_without_affecting_teacher(self):
        setCourseCollegeLeader(self.cc, self.a)
        setCourseCollegeLeader(self.cc, self.b)
        self.assertFalse(self.hasCourseTeam(self.a))
        self.assertTrue(self.hasCourseTeam(self.b))
        self.assertTrue(self.a.groups.filter(name='教师').exists())
        self.assertTrue(self.cc.teachers.filter(pk=self.b.pk).exists())

    def test_other_appointment_retains_membership(self):
        other = CourseCollege.objects.create(
            course=Course.objects.create(code='LEADER2', name='另一课程'),
            department=self.department, leader=self.a)
        setCourseCollegeLeader(self.cc, self.a)
        setCourseCollegeLeader(self.cc, self.b)
        self.assertTrue(self.hasCourseTeam(self.a))
        unsetCourseCollegeLeader(other)
        self.assertFalse(self.hasCourseTeam(self.a))

    def test_model_save_covers_admin_changes(self):
        self.cc.leader = self.a
        self.cc.save()
        self.assertTrue(self.hasCourseTeam(self.a))
        self.cc.leader = self.b
        self.cc.save()
        self.assertFalse(self.hasCourseTeam(self.a))
        self.cc.leader = None
        self.cc.save()
        self.assertFalse(self.hasCourseTeam(self.b))

    def test_unrelated_update_does_not_change_leader(self):
        setCourseCollegeLeader(self.cc, self.a)
        self.cc.leader = self.b
        self.cc.save(update_fields=['department'])
        self.assertTrue(self.hasCourseTeam(self.a))
        self.assertFalse(self.hasCourseTeam(self.b))

    def test_queryset_delete_and_cascade_reclaim_membership(self):
        setCourseCollegeLeader(self.cc, self.a)
        CourseCollege.objects.filter(pk=self.cc.pk).delete()
        self.assertFalse(self.hasCourseTeam(self.a))
        CourseCollege.objects.create(course=self.course, department=self.department, leader=self.b)
        self.course.delete()
        self.assertFalse(self.hasCourseTeam(self.b))

    def test_delete_keeps_other_appointment(self):
        setCourseCollegeLeader(self.cc, self.a)
        CourseCollege.objects.create(
            course=Course.objects.create(code='LEADER2', name='另一课程'),
            department=self.department, leader=self.a)
        self.cc.delete()
        self.assertTrue(self.hasCourseTeam(self.a))

    def test_delete_stale_instance_reclaims_actual_leader(self):
        setCourseCollegeLeader(self.cc, self.a)
        fresh = CourseCollege.objects.get(pk=self.cc.pk)
        setCourseCollegeLeader(fresh, self.b)
        self.cc.delete()
        self.assertFalse(self.hasCourseTeam(self.b))

    def test_protected_delete_does_not_revoke(self):
        setCourseCollegeLeader(self.cc, self.a)
        tc = TeachingClass.objects.create(name='教学班', term='T1', course_college=self.cc)
        Homework.objects.create(teaching_class=tc, title='作业',
                                start_at=timezone.now(), deadline=timezone.now())
        with self.assertRaises(ProtectedError), transaction.atomic():
            self.cc.delete()
        self.assertTrue(self.hasCourseTeam(self.a))

    def test_transaction_rollback_restores_appointment_and_group(self):
        setCourseCollegeLeader(self.cc, self.a)
        with self.assertRaises(RuntimeError), transaction.atomic():
            setCourseCollegeLeader(self.cc, self.b)
            raise RuntimeError('rollback')
        self.cc.refresh_from_db()
        self.assertEqual(self.cc.leader_id, self.a.pk)
        self.assertTrue(self.hasCourseTeam(self.a))
        self.assertFalse(self.hasCourseTeam(self.b))
