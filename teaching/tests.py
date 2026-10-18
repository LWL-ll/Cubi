"""教学业务对象级权限测试矩阵

对象级校验是防越权的核心（教师边界=执教班级、学生边界=所在班级），
任何教师/学生业务接口（通知、作业、资料…）开发时都必须通过这套矩阵：
- getTeacherClazz：教师只能取自己执教的班级，越界/不存在一律 404
- getStudentClazz：学生只能取自己所在的班级，越界/不存在一律 404
- IsStudentGroup / IsCourseTeamGroup：API 入口分组校验（与 IsTeacherGroup 同构）

与 users/tests.py（功能级权限矩阵）互补：那边管「有没有权限码」，这边管「能不能对这个班用」。
"""
from django.contrib.auth.models import AnonymousUser, Group
from django.http import Http404
from django.test import TestCase
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory, force_authenticate

from academics.models import Clazz, College, Course, CourseCollege
from users.models import User
from users.permissions import IsCourseTeamGroup, IsStudentGroup, IsTeacherGroup

from .helpers import getStudentClazz, getTeacherClazz

PASSWORD = 'Test12345'


class ObjectPermissionTestBase(TestCase):
    """公共测试数据：T9011 执教 myClazz；S9011 是 myClazz 学生"""

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

        college = College.objects.create(name='对象级测试学院')
        cls.myClazz = Clazz.objects.create(name='对象测试2301', college=college)
        cls.otherClazz = Clazz.objects.create(name='对象测试2302', college=college)
        cls.myClazz.teachers.add(cls.teacher)
        cls.myClazz.students.add(cls.student)
        course = Course.objects.create(code='OBJ01', name='对象测试课程')
        CourseCollege.objects.create(course=course, college=college) \
            .clazzes.add(cls.myClazz, cls.otherClazz)


class TeacherObjectPermissionTests(ObjectPermissionTestBase):
    """教师对象级：getTeacherClazz 只认执教班级"""

    def test_own_clazz_allowed(self):
        """执教班级：正常返回班级对象"""
        self.assertEqual(getTeacherClazz(self.teacher, self.myClazz.id), self.myClazz)

    def test_other_clazz_blocked(self):
        """非执教班级（存在但不教的班）：404，不暴露存在性"""
        with self.assertRaises(Http404):
            getTeacherClazz(self.teacher, self.otherClazz.id)

    def test_other_teacher_clazz_blocked(self):
        """别的教师执教的班级：404"""
        with self.assertRaises(Http404):
            getTeacherClazz(self.teacher2, self.myClazz.id)

    def test_missing_clazz_blocked(self):
        """不存在的班级：同样 404（与无权访问不可区分，防探测）"""
        with self.assertRaises(Http404):
            getTeacherClazz(self.teacher, 99999)

    def test_non_teacher_group_blocked(self):
        """学生组用户：即使班级真实存在也 404"""
        with self.assertRaises(Http404):
            getTeacherClazz(self.student, self.myClazz.id)

    def test_superuser_bypass(self):
        """超级用户放行（管理员排查问题用）"""
        self.assertEqual(getTeacherClazz(self.superUser, self.otherClazz.id),
                         self.otherClazz)


class StudentObjectPermissionTests(ObjectPermissionTestBase):
    """学生对象级：getStudentClazz 只认所在班级"""

    def test_own_clazz_allowed(self):
        """所在班级：正常返回班级对象"""
        self.assertEqual(getStudentClazz(self.student, self.myClazz.id), self.myClazz)

    def test_other_clazz_blocked(self):
        """非所在班级（存在但不是我的班）：404"""
        with self.assertRaises(Http404):
            getStudentClazz(self.student, self.otherClazz.id)

    def test_other_student_clazz_blocked(self):
        """别的学生所在班级：404"""
        with self.assertRaises(Http404):
            getStudentClazz(self.student2, self.myClazz.id)

    def test_missing_clazz_blocked(self):
        """不存在的班级：同样 404（防探测）"""
        with self.assertRaises(Http404):
            getStudentClazz(self.student, 99999)

    def test_non_student_group_blocked(self):
        """教师组用户：404（学生与教师的班级边界互不相通）"""
        with self.assertRaises(Http404):
            getStudentClazz(self.teacher, self.myClazz.id)

    def test_superuser_bypass(self):
        """超级用户放行"""
        self.assertEqual(getStudentClazz(self.superUser, self.otherClazz.id),
                         self.otherClazz)


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
