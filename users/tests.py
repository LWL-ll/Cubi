"""权限测试矩阵：各角色 × 各端点的访问边界回归测试

权限体系是安全核心，任何权限点/分组/视图校验的改动都必须跑过这套矩阵。
测试库由迁移自动建好四分组和权限分配，setUpTestData 自建最小教学数据，
不依赖开发库里的演示数据。

V2 表结构：教师任课挂教学班、学生经成员关系挂行政班。
"""
from datetime import date

from django.contrib.auth.models import Group
from django.test import TestCase
from rest_framework.test import APIClient

from academics.models import (AdminClass, ClassMembership, Course,
                              CourseCollege, Department, Major, TeachingClass)
from users.models import SchoolIdentity, StudentProfile, User

PASSWORD = 'Test12345'


class PermissionMatrixTests(TestCase):
    """覆盖四角色 × 页面/API 端点的访问边界（200/403/404 精确断言）"""

    @classmethod
    def setUpTestData(cls):
        # 四个角色账号（四分组及权限分配由数据迁移在测试库建好）
        cls.teacher = User.objects.create_user(username='T9001', password=PASSWORD)
        cls.teacher.groups.add(Group.objects.get(name='教师'))
        cls.teacher2 = User.objects.create_user(username='T9002', password=PASSWORD)
        cls.teacher2.groups.add(Group.objects.get(name='教师'))
        cls.student = User.objects.create_user(username='S9001', password=PASSWORD)
        cls.student.groups.add(Group.objects.get(name='学生'))
        cls.adminUser = User.objects.create_user(username='A9001', password=PASSWORD)
        cls.adminUser.groups.add(Group.objects.get(name='教务'))
        cls.courseTeam = User.objects.create_user(username='C9001', password=PASSWORD)
        cls.courseTeam.groups.add(Group.objects.get(name='课程组'))
        cls.superUser = User.objects.create_superuser(username='SU9001', password=PASSWORD)

        # V2 教学数据：T9001 执教 myTeachingClass；
        # otherTeachingClass 没有教师；S9001 是 myTeachingClass 的学生
        department = Department.objects.create(code='D95', name='测试学院')
        major = Major.objects.create(
            department=department, code='95', name='测试专业')
        cls.myAdminClass = AdminClass.objects.create(
            name='测试2301', major=major, enrollment_year=2023)
        cls.otherAdminClass = AdminClass.objects.create(
            name='测试2302', major=major, enrollment_year=2023)
        course = Course.objects.create(code='TEST01', name='测试课程')
        cc = CourseCollege.objects.create(course=course, department=department)
        cc.admin_classes.add(cls.myAdminClass, cls.otherAdminClass)

        cls.myTeachingClass = TeachingClass.objects.create(
            name='测试一班', term='2026-2027-1', course_college=cc)
        cls.myTeachingClass.admin_classes.add(cls.myAdminClass)
        cls.myTeachingClass.teachers.add(cls.teacher)
        cls.otherTeachingClass = TeachingClass.objects.create(
            name='测试二班', term='2026-2027-1', course_college=cc)
        cls.otherTeachingClass.admin_classes.add(cls.otherAdminClass)

        # 学生三连：账号 → 学校身份 → 学生资料 → 班级成员关系
        identity = SchoolIdentity.objects.create(
            user=cls.student, school_number='20239501',
            identity_type=SchoolIdentity.IDENTITY_STUDENT)
        profile = StudentProfile.objects.create(
            identity=identity, major=major, enrollment_year=2023)
        ClassMembership.objects.create(
            student_profile=profile, admin_class=cls.myAdminClass,
            joined_at=date(2023, 9, 1))

    # ---- 工具：按角色构建已登录客户端 ----
    def _pageClient(self, username):
        """页面测试客户端：force_login 绕过 axes，只测权限逻辑"""
        self.client.force_login(User.objects.get(username=username))
        return self.client

    def _apiClient(self, username):
        """API 测试客户端：走真实登录接口"""
        client = APIClient()
        client.post('/api/auth/login/',
                    {'username': username, 'password': PASSWORD}, format='json')
        return client

    # ================= 教师工作台：入口分组校验 =================

    def test_teacher_page_group_gate(self):
        self.assertEqual(self._pageClient('T9001').get('/teacher/').status_code, 200)
        self.assertEqual(self._pageClient('SU9001').get('/teacher/').status_code, 200)
        self.assertEqual(self._pageClient('A9001').get('/teacher/').status_code, 403)
        self.assertEqual(self._pageClient('S9001').get('/teacher/').status_code, 403)

    def test_teacher_page_teaching_class_scope(self):
        """教学班详情页：非执教教学班一律 404（含存在但不教的班）"""
        self.assertEqual(self._pageClient('T9001')
                         .get(f'/teacher/teaching-classes/{self.myTeachingClass.id}/').status_code, 200)
        self.assertEqual(self._pageClient('T9001')
                         .get(f'/teacher/teaching-classes/{self.otherTeachingClass.id}/').status_code, 404)
        self.assertEqual(self._pageClient('T9002')
                         .get(f'/teacher/teaching-classes/{self.myTeachingClass.id}/').status_code, 404)
        # 无效 tab 参数：服务端强制 403
        self.assertEqual(self._pageClient('T9001')
                         .get(f'/teacher/teaching-classes/{self.myTeachingClass.id}/?tab=xxx').status_code, 403)

    # ================= 教师工作台 API =================

    def test_teacher_api_group_gate(self):
        self.assertEqual(self._apiClient('T9001').get('/api/teacher/teaching-classes/').status_code, 200)
        self.assertEqual(self._apiClient('SU9001').get('/api/teacher/teaching-classes/').status_code, 200)
        self.assertEqual(self._apiClient('A9001').get('/api/teacher/teaching-classes/').status_code, 403)
        self.assertEqual(self._apiClient('S9001').get('/api/teacher/teaching-classes/').status_code, 403)
        self.assertEqual(APIClient().get('/api/teacher/teaching-classes/').status_code, 403)

    def test_teacher_api_teaching_class_scope(self):
        self.assertEqual(self._apiClient('T9001')
                         .get(f'/api/teacher/teaching-classes/{self.myTeachingClass.id}/').status_code, 200)
        self.assertEqual(self._apiClient('T9001')
                         .get(f'/api/teacher/teaching-classes/{self.otherTeachingClass.id}/').status_code, 404)
        self.assertEqual(self._apiClient('T9002')
                         .get(f'/api/teacher/teaching-classes/{self.myTeachingClass.id}/').status_code, 404)
        # 不存在的教学班同样 404（不暴露存在性）
        self.assertEqual(self._apiClient('T9001').get('/api/teacher/teaching-classes/9999/').status_code, 404)

    # ================= 学生工作台：页面入口分组校验 =================

    def test_student_page_group_gate(self):
        self.assertEqual(self._pageClient('S9001').get('/student/').status_code, 302)
        self.assertEqual(self._pageClient('S9001').get('/student/homeworks/').status_code, 200)
        self.assertEqual(self._pageClient('S9001').get('/student/analysis/').status_code, 200)
        self.assertEqual(self._pageClient('S9001').get('/student/profile/').status_code, 200)
        self.assertEqual(self._pageClient('SU9001').get('/student/homeworks/').status_code, 200)
        self.assertEqual(self._pageClient('T9001').get('/student/homeworks/').status_code, 403)
        self.assertEqual(self._pageClient('A9001').get('/student/homeworks/').status_code, 403)
        # 非法菜单 key：服务端强制 403
        self.assertEqual(self._pageClient('S9001').get('/student/xxx/').status_code, 404)

    # ================= 教务工作台：页面与 API 入口 =================

    def test_office_page_gate(self):
        """教务页面必须拥有对应权限码才能打开（教师/学生 403）"""
        self.assertEqual(self._pageClient('A9001').get('/office/courses/').status_code, 200)
        self.assertEqual(self._pageClient('SU9001').get('/office/courses/').status_code, 200)
        self.assertEqual(self._pageClient('T9001').get('/office/courses/').status_code, 403)
        self.assertEqual(self._pageClient('S9001').get('/office/courses/').status_code, 403)

    def test_admin_api_permcodes(self):
        self.assertEqual(self._apiClient('A9001').get('/api/courses/').status_code, 200)
        self.assertEqual(self._apiClient('SU9001').get('/api/courses/').status_code, 200)
        self.assertEqual(self._apiClient('T9001').get('/api/courses/').status_code, 403)
        self.assertEqual(self._apiClient('S9001').get('/api/courses/').status_code, 403)

    # ================= api/me 权限码返回 =================

    def test_api_me_permcodes(self):
        expectTeacher = {'notice:manage', 'analysis:view', 'material:class_manage',
                         'homework:publish', 'homework:bank', 'homework:remind',
                         'homework:comment', 'homework:answer', 'homework:ai', 'profile:edit'}
        expectAdmin = {'class:manage', 'class:assign_teacher', 'course:manage',
                       'course:assign_class', 'user:assign_role', 'profile:edit'}
        expectStudent = {'homework:view', 'notice:view', 'material:view',
                         'analysis:view_self', 'profile:edit'}
        expectCourseTeam = {'knowledge:manage', 'material:class_visible',
                            'question_bank:manage', 'chapter:manage', 'profile:edit'}
        self.assertEqual(set(self._apiClient('T9001').get('/api/auth/me/').json()['permCodes']),
                         expectTeacher)
        self.assertEqual(set(self._apiClient('A9001').get('/api/auth/me/').json()['permCodes']),
                         expectAdmin)
        self.assertEqual(set(self._apiClient('S9001').get('/api/auth/me/').json()['permCodes']),
                         expectStudent)
        self.assertEqual(set(self._apiClient('C9001').get('/api/auth/me/').json()['permCodes']),
                         expectCourseTeam)
        self.assertEqual(self._apiClient('T9001').get('/api/auth/me/').json()['groupKey'], 'teacher')
        self.assertEqual(self._apiClient('A9001').get('/api/auth/me/').json()['groupKey'], 'admin')
        self.assertEqual(self._apiClient('C9001').get('/api/auth/me/').json()['groupKey'], 'course_team')

    # ================= 权限工具函数 =================

    def test_hasPerm(self):
        from users.permissions import hasPerm
        # 超级用户全放行
        self.assertTrue(hasPerm(self.superUser, 'class:manage'))
        self.assertTrue(hasPerm(self.superUser, 'homework:ai'))
        # 教师有自己的权限、没有教务和课程组权限
        self.assertTrue(hasPerm(self.teacher, 'homework:ai'))
        self.assertFalse(hasPerm(self.teacher, 'course:manage'))
        self.assertFalse(hasPerm(self.teacher, 'material:class_visible'))
        # 学生只有作业查看和资料修改
        self.assertTrue(hasPerm(self.student, 'homework:view'))
        self.assertFalse(hasPerm(self.student, 'homework:ai'))
        # 未知权限码 fail-closed
        self.assertFalse(hasPerm(self.teacher, '不存在的码'))

    def test_getGroupKey_priority(self):
        from users.permissions import getGroupKey
        # 课程组组长同时也是教师：组长身份优先
        leader = User.objects.create_user(username='T9003', password=PASSWORD)
        leader.groups.add(Group.objects.get(name='教师'), Group.objects.get(name='课程组'))
        self.assertEqual(getGroupKey(leader), 'course_team')
        self.assertEqual(getGroupKey(self.teacher), 'teacher')
        self.assertEqual(getGroupKey(self.superUser), 'superuser')

    def test_consistency_check_pass(self):
        """权限一致性系统检查当前应无报错（定义与数据库对齐）"""
        from users.checks import check_permission_consistency
        self.assertEqual(check_permission_consistency(None), [])


class PasswordLifecycleTests(TestCase):
    """首次登录强制改密闭环：登录标记 → 中间件全局拦截 → 改密后放行"""

    @classmethod
    def setUpTestData(cls):
        # 教师账号（教师组），置为「首次登录须改密」
        cls.mustUser = User.objects.create_user(username='P9001', password=PASSWORD)
        cls.mustUser.groups.add(Group.objects.get(name='教师'))
        cls.mustUser.must_change_password = True
        cls.mustUser.save()

    def _login(self):
        """真实登录接口（走中间件与 DRF 全链路）"""
        client = APIClient()
        resp = client.post('/api/auth/login/',
                           {'username': 'P9001', 'password': PASSWORD}, format='json')
        return client, resp

    def test_login_returns_must_change_flag(self):
        """登录响应携带 mustChangePassword，前端据此跳转改密页"""
        _, resp = self._login()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['mustChangePassword'])

    def test_middleware_blocks_api(self):
        """未改密前除白名单外所有 API 一律 403（含改密提示）"""
        client, _ = self._login()
        resp = client.get('/api/teacher/teaching-classes/')
        self.assertEqual(resp.status_code, 403)
        self.assertIn('首次登录', resp.json()['error'])
        # 白名单放行：登录/登出/me/改密本身
        self.assertEqual(client.get('/api/auth/me/').status_code, 200)

    def test_middleware_redirects_page(self):
        """未改密前访问页面一律重定向到改密页，改密页本身可访问"""
        pageClient = self.client
        pageClient.force_login(self.mustUser)
        resp = pageClient.get('/teacher/', follow=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], '/change-password/')
        self.assertEqual(pageClient.get('/change-password/').status_code, 200)

    def test_change_password_success(self):
        """改密成功：解除标记 + 密码生效 + 会话保持 + 业务 API 放行"""
        client, _ = self._login()
        resp = client.post('/api/auth/change-password/',
                           {'oldPassword': PASSWORD, 'newPassword': 'NewPass123'},
                           format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['message'], '密码修改成功')
        self.mustUser.refresh_from_db()
        self.assertFalse(self.mustUser.must_change_password)
        self.assertTrue(self.mustUser.check_password('NewPass123'))
        # 会话未断（update_session_auth_hash）：改密后业务 API 直接放行
        self.assertEqual(client.get('/api/teacher/teaching-classes/').status_code, 200)

    def test_change_password_rejects_wrong_old(self):
        """旧密码错误：拒绝且不改密标记保持"""
        client, _ = self._login()
        resp = client.post('/api/auth/change-password/',
                           {'oldPassword': 'WrongPass1', 'newPassword': 'NewPass123'},
                           format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('旧密码不正确', resp.json()['error'])
        self.mustUser.refresh_from_db()
        self.assertTrue(self.mustUser.must_change_password)

    def test_change_password_rejects_weak(self):
        """新密码强度不足（纯数字）：拒绝且不改密标记保持"""
        client, _ = self._login()
        resp = client.post('/api/auth/change-password/',
                           {'oldPassword': PASSWORD, 'newPassword': '12345678'},
                           format='json')
        self.assertEqual(resp.status_code, 400)
        self.mustUser.refresh_from_db()
        self.assertTrue(self.mustUser.must_change_password)
