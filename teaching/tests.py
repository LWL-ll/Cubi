"""教学业务对象级权限测试矩阵（V2 教学班版）

对象级校验是防越权的核心（教师边界=执教教学班、学生边界=所在教学班），
任何教师/学生业务接口（通知、作业、资料…）开发时都必须通过这套矩阵：
- getTeacherTeachingClass：教师只能取自己执教的教学班，越界/不存在一律 404
- getStudentTeachingClass：学生只能取自己所在的教学班（3 跳推导），
  离班成员（left_at 非空）同样 404
- IsStudentGroup / IsCourseTeamGroup：API 入口分组校验（与 IsTeacherGroup 同构）
- 教师 API 视图：列表只含自己执教的教学班，详情越界 404

与 users/tests.py（功能级权限矩阵）互补：那边管「有没有权限码」，
这边管「能不能对这个教学班用」。
"""
from datetime import date

from django.contrib.auth.models import AnonymousUser, Group
from django.http import Http404
from django.test import TestCase
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory, force_authenticate

from academics.models import (AdminClass, ClassMembership, Course,
                              CourseCollege, Department, Major, TeachingClass)
from users.models import SchoolIdentity, StudentProfile, User
from users.permissions import IsCourseTeamGroup, IsStudentGroup, IsTeacherGroup

from .api import TeacherTeachingClassDetailView, TeacherTeachingClassListView
from .helpers import getStudentTeachingClass, getTeacherTeachingClass

PASSWORD = 'Test12345'
factory = APIRequestFactory()


class ObjectPermissionTestBase(TestCase):
    """公共测试数据：T9011 执教 myTeachingClass；S9011 是 myTeachingClass 学生

    V2 数据链：
    - 教师 → TeachingClass.teachers（1 跳）
    - 学生 → SchoolIdentity(student) → StudentProfile → ClassMembership
      → AdminClass → TeachingClass（3 跳推导）
    """

    @classmethod
    def setUpTestData(cls):
        cls.teacher = User.objects.create_user(username='T9011', password=PASSWORD)
        cls.teacher.groups.add(Group.objects.get(name='教师'))
        cls.teacher2 = User.objects.create_user(username='T9012', password=PASSWORD)
        cls.teacher2.groups.add(Group.objects.get(name='教师'))
        cls.student = User.objects.create_user(username='S9011', password=PASSWORD)
        cls.student.groups.add(Group.objects.get(name='学生'))
        cls.student2 = User.objects.create_user(username='S9012', password=PASSWORD)
        cls.student2.groups.add(Group.objects.get(name='学生'))
        cls.courseTeam = User.objects.create_user(username='C9011', password=PASSWORD)
        cls.courseTeam.groups.add(Group.objects.get(name='课程组'))
        cls.superUser = User.objects.create_superuser(username='SU9011', password=PASSWORD)

        department = Department.objects.create(code='D90', name='对象级测试学院')
        major = Major.objects.create(
            department=department, code='90', name='测试专业')
        cls.myAdminClass = AdminClass.objects.create(
            name='对象测试2301', major=major, enrollment_year=2023)
        cls.otherAdminClass = AdminClass.objects.create(
            name='对象测试2302', major=major, enrollment_year=2023)
        course = Course.objects.create(code='OBJ01', name='对象测试课程')
        cc = CourseCollege.objects.create(course=course, department=department)
        cc.admin_classes.add(cls.myAdminClass, cls.otherAdminClass)

        # 两个教学班：T9011 教一班（myAdminClass），T9012 教二班（otherAdminClass）
        cls.myTeachingClass = TeachingClass.objects.create(
            name='对象测试一班', term='2026-2027-1', course_college=cc)
        cls.myTeachingClass.admin_classes.add(cls.myAdminClass)
        cls.myTeachingClass.teachers.add(cls.teacher)
        cls.otherTeachingClass = TeachingClass.objects.create(
            name='对象测试二班', term='2026-2027-1', course_college=cc)
        cls.otherTeachingClass.admin_classes.add(cls.otherAdminClass)
        cls.otherTeachingClass.teachers.add(cls.teacher2)

        # 学生三连：账号 → 学校身份 → 学生资料 → 班级成员关系
        cls.myProfile = cls._makeStudent(
            cls.student, '20239001', major, cls.myAdminClass)
        cls.otherProfile = cls._makeStudent(
            cls.student2, '20239002', major, cls.otherAdminClass)

    @staticmethod
    def _makeStudent(user, studentNo, major, adminClass):
        """建学生三连（账号已存在），返回 StudentProfile"""
        identity = SchoolIdentity.objects.create(
            user=user, school_number=studentNo,
            identity_type=SchoolIdentity.IDENTITY_STUDENT)
        profile = StudentProfile.objects.create(
            identity=identity, major=major, enrollment_year=2023)
        ClassMembership.objects.create(
            student_profile=profile, admin_class=adminClass,
            joined_at=date(2023, 9, 1))
        return profile


class TeacherObjectPermissionTests(ObjectPermissionTestBase):
    """教师对象级：getTeacherTeachingClass 只认执教教学班"""

    def test_own_teaching_class_allowed(self):
        """执教教学班：正常返回教学班对象"""
        self.assertEqual(
            getTeacherTeachingClass(self.teacher, self.myTeachingClass.id),
            self.myTeachingClass)

    def test_other_teaching_class_blocked(self):
        """非执教教学班（存在但不教的班）：404，不暴露存在性"""
        with self.assertRaises(Http404):
            getTeacherTeachingClass(self.teacher, self.otherTeachingClass.id)

    def test_other_teacher_teaching_class_blocked(self):
        """别的教师执教的教学班：404"""
        with self.assertRaises(Http404):
            getTeacherTeachingClass(self.teacher2, self.myTeachingClass.id)

    def test_missing_teaching_class_blocked(self):
        """不存在的教学班：同样 404（与无权访问不可区分，防探测）"""
        with self.assertRaises(Http404):
            getTeacherTeachingClass(self.teacher, 99999)

    def test_non_teacher_group_blocked(self):
        """学生组用户：即使教学班真实存在也 404"""
        with self.assertRaises(Http404):
            getTeacherTeachingClass(self.student, self.myTeachingClass.id)

    def test_superuser_bypass(self):
        """超级用户放行（管理员排查问题用）"""
        self.assertEqual(
            getTeacherTeachingClass(self.superUser, self.otherTeachingClass.id),
            self.otherTeachingClass)


class StudentObjectPermissionTests(ObjectPermissionTestBase):
    """学生对象级：getStudentTeachingClass 只认所在教学班（3 跳推导）"""

    def test_own_teaching_class_allowed(self):
        """所在教学班：正常返回教学班对象"""
        self.assertEqual(
            getStudentTeachingClass(self.student, self.myTeachingClass.id),
            self.myTeachingClass)

    def test_other_teaching_class_blocked(self):
        """非所在教学班（存在但不是我的班）：404"""
        with self.assertRaises(Http404):
            getStudentTeachingClass(self.student, self.otherTeachingClass.id)

    def test_other_student_teaching_class_blocked(self):
        """别的学生所在教学班：404"""
        with self.assertRaises(Http404):
            getStudentTeachingClass(self.student2, self.myTeachingClass.id)

    def test_missing_teaching_class_blocked(self):
        """不存在的教学班：同样 404（防探测）"""
        with self.assertRaises(Http404):
            getStudentTeachingClass(self.student, 99999)

    def test_non_student_group_blocked(self):
        """教师组用户：404（教师没有学生身份，3 跳第一跳就断）"""
        with self.assertRaises(Http404):
            getStudentTeachingClass(self.teacher, self.myTeachingClass.id)

    def test_left_membership_blocked(self):
        """已离班学生（left_at 非空的历史成员关系）不能访问原教学班"""
        ClassMembership.objects.create(
            student_profile=self.otherProfile, admin_class=self.myAdminClass,
            joined_at=date(2023, 9, 1), left_at=date(2024, 9, 1))
        with self.assertRaises(Http404):
            getStudentTeachingClass(self.student2, self.myTeachingClass.id)

    def test_multi_class_teaching_class(self):
        """合班语义：教学班加入学生所在行政班后，该学生即可访问"""
        self.otherTeachingClass.admin_classes.remove(self.otherAdminClass)
        self.myTeachingClass.admin_classes.add(self.otherAdminClass)
        self.assertEqual(
            getStudentTeachingClass(self.student2, self.myTeachingClass.id),
            self.myTeachingClass)
        # 撤出行政班后恢复 404
        self.myTeachingClass.admin_classes.remove(self.otherAdminClass)
        with self.assertRaises(Http404):
            getStudentTeachingClass(self.student2, self.myTeachingClass.id)

    def test_superuser_bypass(self):
        """超级用户放行"""
        self.assertEqual(
            getStudentTeachingClass(self.superUser, self.otherTeachingClass.id),
            self.otherTeachingClass)


class ApiGroupPermissionClassTests(ObjectPermissionTestBase):
    """API 入口权限类：IsTeacherGroup / IsStudentGroup / IsCourseTeamGroup"""

    @staticmethod
    def _check(permClass, user, expectAllowed):
        """用 APIRequestFactory 构造请求，直接测权限类的 has_permission"""
        request = APIRequestFactory().get('/api/test/')
        # 已登录用户用 force_authenticate 注入身份；None 表示匿名用户
        if user is None:
            user = AnonymousUser()
        else:
            force_authenticate(request, user=user)
        # 包成 DRF 的 Request，才有 .user 属性（权限类内部会读它）
        drfRequest = Request(request)
        return permClass().has_permission(drfRequest, None) is expectAllowed

    def test_teacher_group(self):
        # 教师组放行、学生/课程组拒绝、超管放行、未登录拒绝
        self.assertTrue(self._check(IsTeacherGroup, self.teacher, True))
        self.assertTrue(self._check(IsTeacherGroup, self.student, False))
        self.assertTrue(self._check(IsTeacherGroup, self.superUser, True))
        self.assertTrue(self._check(IsTeacherGroup, None, False))

    def test_student_group(self):
        self.assertTrue(self._check(IsStudentGroup, self.student, True))
        self.assertTrue(self._check(IsStudentGroup, self.teacher, False))
        self.assertTrue(self._check(IsStudentGroup, self.superUser, True))

    def test_course_team_group(self):
        self.assertTrue(self._check(IsCourseTeamGroup, self.courseTeam, True))
        self.assertTrue(self._check(IsCourseTeamGroup, self.teacher, False))
        self.assertTrue(self._check(IsCourseTeamGroup, self.student, False))
        self.assertTrue(self._check(IsCourseTeamGroup, self.superUser, True))


class TeacherApiTests(ObjectPermissionTestBase):
    """教师 API 视图：列表只含执教教学班，详情越界 404"""

    def _call(self, viewClass, user, **urlKwargs):
        """构造已认证 GET 请求并执行视图（走 DRF 完整权限链）"""
        view = viewClass.as_view()
        request = factory.get('/api/test/')
        force_authenticate(request, user=user)
        return view(request, **urlKwargs)

    def test_list_only_own_teaching_classes(self):
        """列表天然只含当前教师执教的教学班（teachers 过滤）"""
        resp = self._call(TeacherTeachingClassListView, self.teacher)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([item['name'] for item in resp.data], ['对象测试一班'])
        # 列表项带课程/学院/学生数
        self.assertEqual(resp.data[0]['course']['name'], '对象测试课程')
        self.assertEqual(resp.data[0]['studentCount'], 1)

    def test_detail_own_teaching_class(self):
        resp = self._call(TeacherTeachingClassDetailView, self.teacher,
                          teachingClassId=self.myTeachingClass.id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['name'], '对象测试一班')
        # 学生名单经成员关系推导
        self.assertEqual(resp.data['studentCount'], 1)
        self.assertEqual([s['username'] for s in resp.data['students']], ['S9011'])

    def test_detail_other_teaching_class_404(self):
        """非执教教学班：404（与不存在不可区分，防探测）"""
        resp = self._call(TeacherTeachingClassDetailView, self.teacher,
                          teachingClassId=self.otherTeachingClass.id)
        self.assertEqual(resp.status_code, 404)

    def test_detail_missing_404(self):
        resp = self._call(TeacherTeachingClassDetailView, self.teacher,
                          teachingClassId=99999)
        self.assertEqual(resp.status_code, 404)

    def test_anonymous_rejected(self):
        """未登录：入口权限类拒绝"""
        resp = TeacherTeachingClassListView.as_view()(factory.get('/api/test/'))
        self.assertIn(resp.status_code, (401, 403))

    def test_student_group_rejected(self):
        """学生组：即使教学班真实存在也 403（入口即拒）"""
        resp = self._call(TeacherTeachingClassListView, self.student)
        self.assertEqual(resp.status_code, 403)
