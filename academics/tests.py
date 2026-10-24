"""教务 API 权限测试矩阵（V2 表结构）

覆盖教务 8 个 API 视图的三层校验：
1. 入口权限码（PermRequired + 视图 permCode，无权限 403）
2. 动作级细分权限码（POST/DELETE 内按 action 二次校验）
3. 参数兜底（跨学院班级拒绝、教学班至少保留一个组成班级、同池教师等）

配合 users/tests.py（功能级权限矩阵）与 teaching/tests.py（对象级边界）
组成三层防线测试：分组权限 → 功能权限码 → 对象归属。
"""
from datetime import date
from io import BytesIO

from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from academics.api import (AdminClassDetailView, AdminClassListView,
                           CourseCollegeView, CourseDetailView, CourseListView,
                           DepartmentListView, ImportView,
                           TeachingClassDetailView, TeachingClassListView)
from academics.models import (AdminClass, ClassMembership, Course,
                              CourseCollege, Department, Major, TeachingClass)
from users.models import SchoolIdentity, TeacherProfile, User

PASSWORD = 'Test12345'
factory = APIRequestFactory()


def makeXlsx(headers, rows):
    """用 openpyxl 在内存生成真实 xlsx 文件（导入接口测试用）

    rows 传行列表的列表；只传一行数据时可直接传该行（自动包装）。
    """
    from openpyxl import Workbook
    # 单行数据（首元素是字符串）自动包装成一行
    if rows and isinstance(rows[0], str):
        rows = [rows]
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return SimpleUploadedFile(
        'test.xlsx', buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument'
                     '.spreadsheetml.sheet')


class OfficeApiTestBase(TestCase):
    """公共测试数据：教务（全权限）、裸用户（逐个授码测动作级）、教师、学生"""

    @classmethod
    def setUpTestData(cls):
        cls.adminUser = User.objects.create_user(username='A9021', password=PASSWORD)
        cls.adminUser.groups.add(Group.objects.get(name='教务'))
        # 裸用户：无分组，用 user_permissions 逐个授码测「动作级细分权限」
        cls.bareUser = User.objects.create_user(username='B9021', password=PASSWORD)
        cls.teacher = User.objects.create_user(username='T9021', password=PASSWORD)
        cls.teacher.groups.add(Group.objects.get(name='教师'))
        # 教师身份档案（findTeacherByNo 严格化后按工号查找要求档案齐全）
        teacherIdentity = SchoolIdentity.objects.create(
            user=cls.teacher, school_number='T9021',
            identity_type=SchoolIdentity.IDENTITY_TEACHER)
        TeacherProfile.objects.create(identity=teacherIdentity)
        # teacher2 不入教师池（测 addTeacher 的池校验），但同样要有教师身份档案
        cls.teacher2 = User.objects.create_user(username='T9022', password=PASSWORD)
        cls.teacher2.groups.add(Group.objects.get(name='教师'))
        teacher2Identity = SchoolIdentity.objects.create(
            user=cls.teacher2, school_number='T9022',
            identity_type=SchoolIdentity.IDENTITY_TEACHER)
        TeacherProfile.objects.create(identity=teacher2Identity)
        cls.student = User.objects.create_user(username='S9021', password=PASSWORD)
        cls.student.groups.add(Group.objects.get(name='学生'))

        cls.department = Department.objects.create(code='D91', name='矩阵测试学院')
        cls.otherDepartment = Department.objects.create(code='D92', name='矩阵外学院')
        cls.major = Major.objects.create(
            department=cls.department, code='91', name='矩阵专业')
        cls.otherMajor = Major.objects.create(
            department=cls.otherDepartment, code='92', name='外院专业')
        cls.adminClass = AdminClass.objects.create(
            name='矩阵2301', major=cls.major, enrollment_year=2023)
        cls.otherAdminClass = AdminClass.objects.create(
            name='矩阵外2301', major=cls.otherMajor, enrollment_year=2023)
        cls.course = Course.objects.create(code='MTR01', name='矩阵课程')
        cls.cc = CourseCollege.objects.create(
            course=cls.course, department=cls.department)
        cls.cc.admin_classes.add(cls.adminClass)
        cls.cc.teachers.add(cls.teacher)
        cls.teachingClass = TeachingClass.objects.create(
            name='矩阵一班', term='2026-2027-1', course_college=cls.cc)
        cls.teachingClass.admin_classes.add(cls.adminClass)
        cls.teachingClass.teachers.add(cls.teacher)

    def _call(self, viewClass, user, method='get', data=None,
              format='json', **urlKwargs):
        """构造已认证请求并执行视图（走 DRF 完整权限链）"""
        view = viewClass.as_view()
        request = getattr(factory, method)('/api/test/', data, format=format)
        force_authenticate(request, user=user)
        return view(request, **urlKwargs)

    def _grant(self, user, codename):
        """给用户授单个 Django 权限（动作级权限测试用）"""
        permission = Permission.objects.get(
            content_type__app_label='users', codename=codename)
        user.user_permissions.add(permission)


class DepartmentListApiTests(OfficeApiTestBase):
    """学院列表：入口权限 course:manage"""

    def test_admin_can_list(self):
        resp = self._call(DepartmentListView, self.adminUser)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2)

    def test_teacher_forbidden(self):
        self.assertEqual(
            self._call(DepartmentListView, self.teacher).status_code, 403)

    def test_anonymous_rejected(self):
        resp = DepartmentListView.as_view()(factory.get('/api/test/'))
        self.assertIn(resp.status_code, (401, 403))


class CourseApiTests(OfficeApiTestBase):
    """课程视图：入口 course:manage；学院分配动作 course:assign_class"""

    def test_list_and_detail(self):
        resp = self._call(CourseListView, self.adminUser)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        resp = self._call(CourseDetailView, self.adminUser,
                          courseId=self.course.id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['code'], 'MTR01')

    def test_teacher_forbidden(self):
        self.assertEqual(
            self._call(CourseListView, self.teacher).status_code, 403)

    def test_add_department(self):
        resp = self._call(
            CourseDetailView, self.adminUser, method='post',
            data={'action': 'addDepartment',
                  'departmentId': self.otherDepartment.id},
            courseId=self.course.id)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(CourseCollege.objects.filter(
            course=self.course, department=self.otherDepartment).exists())

    def test_remove_department_cascades_teaching_classes(self):
        resp = self._call(
            CourseDetailView, self.adminUser, method='post',
            data={'action': 'removeDepartment',
                  'departmentId': self.department.id},
            courseId=self.course.id)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(CourseCollege.objects.filter(
            course=self.course, department=self.department).exists())
        # 课程×学院删除后，其下的教学班一并解除
        self.assertFalse(TeachingClass.objects.filter(
            id=self.teachingClass.id).exists())

    def test_department_action_requires_assign_class(self):
        """只有 course:manage 没有 course:assign_class → 动作级 403"""
        self._grant(self.bareUser, 'course_manage')
        resp = self._call(
            CourseDetailView, self.bareUser, method='post',
            data={'action': 'addDepartment',
                  'departmentId': self.otherDepartment.id},
            courseId=self.course.id)
        self.assertEqual(resp.status_code, 403)

    def test_missing_department_404(self):
        resp = self._call(
            CourseDetailView, self.adminUser, method='post',
            data={'action': 'addDepartment', 'departmentId': 99999},
            courseId=self.course.id)
        self.assertEqual(resp.status_code, 404)

    def test_missing_course_404(self):
        resp = self._call(CourseDetailView, self.adminUser, courseId=99999)
        self.assertEqual(resp.status_code, 404)

    def test_unknown_action_400(self):
        resp = self._call(
            CourseDetailView, self.adminUser, method='post',
            data={'action': 'xxx', 'departmentId': self.department.id},
            courseId=self.course.id)
        self.assertEqual(resp.status_code, 400)


class CourseCollegeApiTests(OfficeApiTestBase):
    """课程×学院视图：入口 course:manage；组长=user:assign_role、
    班级分配=course:assign_class、教师/学生导入=class:manage"""

    def test_get_detail(self):
        resp = self._call(CourseCollegeView, self.adminUser,
                          courseId=self.course.id,
                          departmentId=self.department.id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['teachers'][0]['username'], 'T9021')

    def test_missing_cc_404(self):
        resp = self._call(CourseCollegeView, self.adminUser,
                          courseId=self.course.id,
                          departmentId=self.otherDepartment.id)
        self.assertEqual(resp.status_code, 404)

    def test_set_leader_requires_assign_role(self):
        """只有 course:manage 没有 user:assign_role → 动作级 403"""
        self._grant(self.bareUser, 'course_manage')
        resp = self._call(
            CourseCollegeView, self.bareUser, method='post',
            data={'action': 'setLeader', 'teacherNo': 'T9021'},
            courseId=self.course.id, departmentId=self.department.id)
        self.assertEqual(resp.status_code, 403)

    def test_set_leader_success(self):
        resp = self._call(
            CourseCollegeView, self.adminUser, method='post',
            data={'action': 'setLeader', 'teacherNo': 'T9021'},
            courseId=self.course.id, departmentId=self.department.id)
        self.assertEqual(resp.status_code, 200)
        self.cc.refresh_from_db()
        self.assertEqual(self.cc.leader, self.teacher)
        # 组长自动加入课程组分组 + 教师池
        self.assertTrue(self.teacher.groups.filter(name='课程组').exists())
        self.assertTrue(self.cc.teachers.filter(id=self.teacher.id).exists())

    def test_set_leader_missing_teacher_400(self):
        resp = self._call(
            CourseCollegeView, self.adminUser, method='post',
            data={'action': 'setLeader', 'teacherNo': 'NOPE'},
            courseId=self.course.id, departmentId=self.department.id)
        self.assertEqual(resp.status_code, 400)

    def test_unset_leader_leaves_course_team_group(self):
        self.cc.leader = self.teacher
        self.cc.save()
        self.teacher.groups.add(Group.objects.get(name='课程组'))
        resp = self._call(
            CourseCollegeView, self.adminUser, method='post',
            data={'action': 'unsetLeader'},
            courseId=self.course.id, departmentId=self.department.id)
        self.assertEqual(resp.status_code, 200)
        self.cc.refresh_from_db()
        self.assertIsNone(self.cc.leader)
        # 不再担任任何组长 → 移出课程组分组
        self.assertFalse(self.teacher.groups.filter(name='课程组').exists())

    def test_add_admin_class_other_department_400(self):
        """跨学院班级：服务端兜底拒绝（经专业层判断学院归属）"""
        resp = self._call(
            CourseCollegeView, self.adminUser, method='post',
            data={'action': 'addAdminClass',
                  'adminClassId': self.otherAdminClass.id},
            courseId=self.course.id, departmentId=self.department.id)
        self.assertEqual(resp.status_code, 400)

    def test_add_admin_class_success(self):
        resp = self._call(
            CourseCollegeView, self.adminUser, method='post',
            data={'action': 'addAdminClass',
                  'adminClassId': self.adminClass.id},
            courseId=self.course.id, departmentId=self.department.id)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            self.cc.admin_classes.filter(id=self.adminClass.id).exists())

    def test_import_teachers_requires_class_manage(self):
        """只有 course:manage 没有 class:manage → 动作级 403"""
        self._grant(self.bareUser, 'course_manage')
        resp = self._call(
            CourseCollegeView, self.bareUser, method='post',
            data={'action': 'importTeachers'},
            courseId=self.course.id, departmentId=self.department.id)
        self.assertEqual(resp.status_code, 403)

    def test_import_teachers_missing_file_400(self):
        resp = self._call(
            CourseCollegeView, self.adminUser, method='post',
            data={'action': 'importTeachers'},
            courseId=self.course.id, departmentId=self.department.id)
        self.assertEqual(resp.status_code, 400)


class AdminClassApiTests(OfficeApiTestBase):
    """行政班视图：入口 class:manage；支持删除与导入学生"""

    def test_list_and_detail(self):
        resp = self._call(AdminClassListView, self.adminUser)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2)
        resp = self._call(AdminClassDetailView, self.adminUser,
                          adminClassId=self.adminClass.id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['name'], '矩阵2301')

    def test_teacher_forbidden(self):
        self.assertEqual(
            self._call(AdminClassListView, self.teacher).status_code, 403)

    def test_delete_success(self):
        resp = self._call(AdminClassDetailView, self.adminUser, method='delete',
                          adminClassId=self.adminClass.id)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AdminClass.objects.filter(id=self.adminClass.id).exists())

    def test_delete_requires_class_manage(self):
        self.assertEqual(
            self._call(AdminClassDetailView, self.teacher, method='delete',
                       adminClassId=self.adminClass.id).status_code, 403)

    def test_import_students_missing_file_400(self):
        resp = self._call(
            AdminClassDetailView, self.adminUser, method='post',
            data={'action': 'importStudents'}, adminClassId=self.adminClass.id)
        self.assertEqual(resp.status_code, 400)

    def test_unknown_action_400(self):
        resp = self._call(
            AdminClassDetailView, self.adminUser, method='post',
            data={'action': 'xxx'}, adminClassId=self.adminClass.id)
        self.assertEqual(resp.status_code, 400)


class TeachingClassApiTests(OfficeApiTestBase):
    """教学班视图：列表/详情入口 course:manage；组班 course:assign_class；
    解散 class:manage；教师分配 class:assign_teacher"""

    def test_list_and_filter_by_cc(self):
        resp = self._call(TeachingClassListView, self.adminUser)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        # ?cc= 过滤某个课程×学院下的教学班
        view = TeachingClassListView.as_view()
        request = factory.get('/api/test/?cc=%d' % self.cc.id)
        force_authenticate(request, user=self.adminUser)
        resp = view(request)
        self.assertEqual(len(resp.data), 1)

    def test_create_requires_assign_class(self):
        """只有 course:manage 没有 course:assign_class → 动作级 403"""
        self._grant(self.bareUser, 'course_manage')
        resp = self._call(
            TeachingClassListView, self.bareUser, method='post',
            data={'name': '矩阵二班', 'courseCollegeId': self.cc.id,
                  'adminClassIds': [self.adminClass.id]})
        self.assertEqual(resp.status_code, 403)

    def test_create_success_inherits_teacher_pool(self):
        self.teachingClass.admin_classes.remove(self.adminClass)
        resp = self._call(
            TeachingClassListView, self.adminUser, method='post',
            data={'name': '矩阵二班', 'courseCollegeId': self.cc.id,
                  'adminClassIds': [self.adminClass.id]})
        self.assertEqual(resp.status_code, 201)
        teachingClass = TeachingClass.objects.get(name='矩阵二班')
        # 新教学班默认继承课程教师池
        self.assertEqual(list(teachingClass.teachers.all()), [self.teacher])

    def test_duplicate_admin_class_same_course_term_rejected(self):
        resp = self._call(
            TeachingClassListView, self.adminUser, method='post',
            data={'name': '重复组班', 'courseCollegeId': self.cc.id,
                  'adminClassIds': [self.adminClass.id]})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(TeachingClass.objects.filter(name='重复组班').exists())

    def test_add_admin_class_already_grouped_rejected(self):
        target = TeachingClass.objects.create(
            name='待组班', term=self.teachingClass.term, course_college=self.cc)
        resp = self._call(
            TeachingClassDetailView, self.adminUser, method='post',
            data={'action': 'addAdminClass', 'adminClassId': self.adminClass.id},
            teachingClassId=target.id)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(target.admin_classes.exists())

    def test_create_duplicate_name_400(self):
        resp = self._call(
            TeachingClassListView, self.adminUser, method='post',
            data={'name': '矩阵一班', 'courseCollegeId': self.cc.id,
                  'adminClassIds': [self.adminClass.id]})
        self.assertEqual(resp.status_code, 400)

    def test_create_empty_admin_classes_400(self):
        resp = self._call(
            TeachingClassListView, self.adminUser, method='post',
            data={'name': '矩阵三班', 'courseCollegeId': self.cc.id,
                  'adminClassIds': []})
        self.assertEqual(resp.status_code, 400)

    def test_create_admin_class_not_in_cc_400(self):
        """组成班级必须属于该课程×学院（防伪造请求）"""
        resp = self._call(
            TeachingClassListView, self.adminUser, method='post',
            data={'name': '矩阵三班', 'courseCollegeId': self.cc.id,
                  'adminClassIds': [self.otherAdminClass.id]})
        self.assertEqual(resp.status_code, 400)

    def test_delete_requires_class_manage(self):
        """只有 course:manage 没有 class:manage → 动作级 403"""
        self._grant(self.bareUser, 'course_manage')
        resp = self._call(TeachingClassDetailView, self.bareUser,
                          method='delete',
                          teachingClassId=self.teachingClass.id)
        self.assertEqual(resp.status_code, 403)

    def test_delete_success_keeps_admin_classes(self):
        resp = self._call(TeachingClassDetailView, self.adminUser,
                          method='delete',
                          teachingClassId=self.teachingClass.id)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(TeachingClass.objects.filter(
            id=self.teachingClass.id).exists())
        # 组成班级、教师、学生数据都保留（教学班只是授课组织）
        self.assertTrue(AdminClass.objects.filter(id=self.adminClass.id).exists())

    def test_add_admin_class_not_in_cc_400(self):
        resp = self._call(
            TeachingClassDetailView, self.adminUser, method='post',
            data={'action': 'addAdminClass',
                  'adminClassId': self.otherAdminClass.id},
            teachingClassId=self.teachingClass.id)
        self.assertEqual(resp.status_code, 400)

    def test_remove_last_admin_class_400(self):
        """教学班至少保留一个组成班级"""
        resp = self._call(
            TeachingClassDetailView, self.adminUser, method='post',
            data={'action': 'removeAdminClass',
                  'adminClassId': self.adminClass.id},
            teachingClassId=self.teachingClass.id)
        self.assertEqual(resp.status_code, 400)

    def test_add_teacher_requires_assign_teacher(self):
        """只有 course:manage + course:assign_class
        没有 class:assign_teacher → 动作级 403"""
        self._grant(self.bareUser, 'course_manage')
        self._grant(self.bareUser, 'course_assign_class')
        resp = self._call(
            TeachingClassDetailView, self.bareUser, method='post',
            data={'action': 'addTeacher', 'teacherId': self.teacher2.id},
            teachingClassId=self.teachingClass.id)
        self.assertEqual(resp.status_code, 403)

    def test_add_teacher_not_in_pool_400(self):
        """下拉框选的教师必须在本课程教师池中（防伪造请求）"""
        resp = self._call(
            TeachingClassDetailView, self.adminUser, method='post',
            data={'action': 'addTeacher', 'teacherId': self.teacher2.id},
            teachingClassId=self.teachingClass.id)
        self.assertEqual(resp.status_code, 400)

    def test_add_teacher_by_no_joins_pool(self):
        """工号添加的教师自动加入本课程教师池"""
        resp = self._call(
            TeachingClassDetailView, self.adminUser, method='post',
            data={'action': 'addTeacher', 'teacherNo': 'T9022'},
            teachingClassId=self.teachingClass.id)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            self.teachingClass.teachers.filter(id=self.teacher2.id).exists())
        self.assertTrue(self.cc.teachers.filter(id=self.teacher2.id).exists())

    def test_remove_teacher(self):
        resp = self._call(
            TeachingClassDetailView, self.adminUser, method='post',
            data={'action': 'removeTeacher', 'teacherId': self.teacher.id},
            teachingClassId=self.teachingClass.id)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            self.teachingClass.teachers.filter(id=self.teacher.id).exists())


class ImportApiTests(OfficeApiTestBase):
    """批量导入视图：入口 class:manage；导入课程需 course:manage"""

    def test_course_import_requires_course_manage(self):
        """只有 class:manage 没有 course:manage → 动作级 403"""
        self._grant(self.bareUser, 'class_manage')
        resp = self._call(ImportView, self.bareUser, method='post',
                          data={'type': 'course'})
        self.assertEqual(resp.status_code, 403)

    def test_teacher_import_requires_class_manage(self):
        self.assertEqual(
            self._call(ImportView, self.teacher, method='post',
                       data={'type': 'teacher'}).status_code, 403)

    def test_missing_file_400(self):
        resp = self._call(ImportView, self.adminUser, method='post',
                          data={'type': 'student'})
        self.assertEqual(resp.status_code, 400)

    def test_unknown_type_400(self):
        fileObj = makeXlsx(['学号', '姓名', '初始密码'], ['20239101', '某生', PASSWORD])
        resp = self._call(ImportView, self.adminUser, method='post',
                          data={'type': 'xxx', 'file': fileObj},
                          format='multipart')
        self.assertEqual(resp.status_code, 400)

    def test_import_student_success(self):
        """学号 20231001 → 计算机学院/计科/计科2301 自动三连"""
        fileObj = makeXlsx(
            ['学号', '姓名', '初始密码'], ['20231001', '导入学生', PASSWORD])
        resp = self._call(ImportView, self.adminUser, method='post',
                          data={'type': 'student', 'file': fileObj},
                          format='multipart')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['successCount'], 1)
        # 三连验证：账号 → 学生身份 → 学生资料 → 班级成员关系
        user = User.objects.get(username='20231001')
        identity = user.identities.get(identity_type='student')
        profile = identity.student_profile
        self.assertEqual(profile.enrollment_year, 2023)
        self.assertTrue(ClassMembership.objects.filter(
            student_profile=profile).exists())
        # 学院/专业/班级按学号规则自动创建（班级编码 = 专业码 + 班序）
        adminClass = profile.memberships.get().admin_class
        self.assertEqual(adminClass.name, '计科2301')
        self.assertEqual(adminClass.major.name, '计科')

    def test_import_teacher_success(self):
        fileObj = makeXlsx(
            ['工号', '姓名', '初始密码'], ['T9029', '导入教师', PASSWORD])
        resp = self._call(ImportView, self.adminUser, method='post',
                          data={'type': 'teacher', 'file': fileObj},
                          format='multipart')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['successCount'], 1)
        # 账号 + 教师身份 + 教师资料
        user = User.objects.get(username='T9029')
        self.assertTrue(user.groups.filter(name='教师').exists())
        identity = user.identities.get(identity_type='teacher')
        self.assertTrue(hasattr(identity, 'teacher_profile'))

    def test_import_course_success(self):
        fileObj = makeXlsx(['课程编号', '课程名称'], ['MTR99', '导入课程'])
        resp = self._call(ImportView, self.adminUser, method='post',
                          data={'type': 'course', 'file': fileObj},
                          format='multipart')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['successCount'], 1)
        self.assertTrue(Course.objects.filter(code='MTR99').exists())
