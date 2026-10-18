"""权限测试矩阵：各角色 × 各端点的访问边界回归测试

权限体系是安全核心，任何权限点/分组/视图校验的改动都必须跑过这套矩阵。
测试库由迁移自动建好四分组和权限分配，setUpTestData 自建最小教学数据，
不依赖开发库里的演示数据。
"""
from django.contrib.auth.models import Group
from django.test import TestCase
from rest_framework.test import APIClient

from academics.models import Clazz, College, Course, CourseCollege
from users.models import User

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

        # 教学数据：T9001 执教 myClazz；otherClazz 没有教师；S9001 是 myClazz 的学生
        college = College.objects.create(name='测试学院')
        cls.myClazz = Clazz.objects.create(name='测试2301', college=college)
        cls.otherClazz = Clazz.objects.create(name='测试2302', college=college)
        cls.myClazz.teachers.add(cls.teacher)
        cls.myClazz.students.add(cls.student)
        course = Course.objects.create(code='TEST01', name='测试课程')
        CourseCollege.objects.create(course=course, college=college) \
            .clazzes.add(cls.myClazz, cls.otherClazz)

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

    def test_teacher_page_clazz_scope(self):
        """班级详情页：非执教班级一律 404（含存在但不教的班级）"""
        self.assertEqual(self._pageClient('T9001')
                         .get(f'/teacher/clazzes/{self.myClazz.id}/').status_code, 200)
        self.assertEqual(self._pageClient('T9001')
                         .get(f'/teacher/clazzes/{self.otherClazz.id}/').status_code, 404)
        self.assertEqual(self._pageClient('T9002')
                         .get(f'/teacher/clazzes/{self.myClazz.id}/').status_code, 404)
        # 无效 tab 参数：服务端强制 403
        self.assertEqual(self._pageClient('T9001')
                         .get(f'/teacher/clazzes/{self.myClazz.id}/?tab=xxx').status_code, 403)

    # ================= 教师工作台 API =================

    def test_teacher_api_group_gate(self):
        self.assertEqual(self._apiClient('T9001').get('/api/teacher/clazzes/').status_code, 200)
        self.assertEqual(self._apiClient('SU9001').get('/api/teacher/clazzes/').status_code, 200)
        self.assertEqual(self._apiClient('A9001').get('/api/teacher/clazzes/').status_code, 403)
        self.assertEqual(self._apiClient('S9001').get('/api/teacher/clazzes/').status_code, 403)
        self.assertEqual(APIClient().get('/api/teacher/clazzes/').status_code, 403)

    def test_teacher_api_clazz_scope(self):
        self.assertEqual(self._apiClient('T9001')
                         .get(f'/api/teacher/clazzes/{self.myClazz.id}/').status_code, 200)
        self.assertEqual(self._apiClient('T9001')
                         .get(f'/api/teacher/clazzes/{self.otherClazz.id}/').status_code, 404)
        self.assertEqual(self._apiClient('T9002')
                         .get(f'/api/teacher/clazzes/{self.myClazz.id}/').status_code, 404)
        # 不存在的班级同样 404（不暴露存在性）
        self.assertEqual(self._apiClient('T9001').get('/api/teacher/clazzes/9999/').status_code, 404)

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
