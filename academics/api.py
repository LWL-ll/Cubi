"""教务业务 API：学院、课程、课程×学院、行政班、教学班、批量导入（前后端分离用）

每个视图类声明 permCode（所需权限码），由 PermRequired 统一校验；
写操作再按动作细分权限码，无权限时返回 403。
权限码与 xmind《用户权限》一一对应。

V2 说明：教师任课挂教学班（不再挂行政班），
教学班管理（组班/教师分配/解散）统一在 TeachingClass* 视图。
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import User
from users.permissions import PermRequired, hasPerm

from .models import AdminClass, Course, CourseCollege, Department, TeachingClass
from .services import createTeachingClass
from .serializers import (AdminClassSerializer, CourseCollegeSerializer,
                          CourseSerializer, DepartmentSerializer,
                          TeachingClassSerializer)
from .views import (CURRENT_TERM, findTeacherByNo, importCourses,
                    importStudents, importStudentsAuto, importTeachers,
                    importTeachersToCourse, parseXlsxRows,
                    setCourseCollegeLeader, unsetCourseCollegeLeader)


def findOr404(modelClass, objectId, name):
    """通用取对象：不存在返回 (None, 404 响应)"""
    obj = modelClass.objects.filter(id=objectId).first()
    if obj is None:
        return None, Response({'error': f'{name}不存在'}, status=404)
    return obj, None


def getImportResponse(okList, errList):
    """统一的导入结果响应：成功数 + 逐行失败原因"""
    return Response({
        'successCount': len(okList),
        'errors': [{'line': lineNo, 'reason': reason}
                   for lineNo, reason in errList],
    })


class DepartmentListView(APIView):
    """学院列表：需要 course:manage（学院由学号解析自动生成，教务查看用）"""
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request):
        departments = Department.objects.all()
        return Response(DepartmentSerializer(departments, many=True).data)


class CourseListView(APIView):
    """课程列表：需要 course:manage（全校课程增删改查）"""
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request):
        courses = Course.objects.all().prefetch_related('course_colleges__department')
        return Response(CourseSerializer(courses, many=True).data)


class CourseDetailView(APIView):
    """课程详情与学院分配"""
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request, courseId):
        course, errResp = findOr404(Course, courseId, '课程')
        if errResp:
            return errResp
        return Response(CourseSerializer(course).data)

    def post(self, request, courseId):
        """添加/移除开设学院：动作级校验，需要 course:assign_class"""
        if not hasPerm(request.user, 'course:assign_class'):
            return Response({'error': '没有权限执行该操作'}, status=403)
        course, errResp = findOr404(Course, courseId, '课程')
        if errResp:
            return errResp
        action = request.data.get('action', '')
        departmentId = request.data.get('departmentId')
        department, errResp = findOr404(Department, departmentId, '学院')
        if errResp:
            return errResp
        if action == 'addDepartment':
            cc, created = CourseCollege.objects.get_or_create(
                course=course, department=department)
            return Response({'message': f'已为课程开设学院 {department.name}'})
        if action == 'removeDepartment':
            # 先解散该课程×学院下的教学班（course_college 是 PROTECT 外键，
            # 不先删教学班会抛 ProtectedError），再删课程×学院记录
            cc = CourseCollege.objects.filter(
                course=course, department=department).first()
            if cc is not None:
                cc.teaching_classes.all().delete()
                cc.delete()
            return Response({'message': f'已移除学院 {department.name}'})
        return Response({'error': '未知操作'}, status=400)


class CourseCollegeView(APIView):
    """课程×学院 详情与设置：组长、教师池、行政班"""
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request, courseId, departmentId):
        cc = CourseCollege.objects.filter(
            course_id=courseId, department_id=departmentId).first()
        if cc is None:
            return Response({'error': '该课程尚未开设此学院'}, status=404)
        return Response(CourseCollegeSerializer(cc).data)

    def post(self, request, courseId, departmentId):
        """按动作细分权限码：设组长=user:assign_role、班级分配=course:assign_class、
        教师/学生导入=class:manage"""
        cc = CourseCollege.objects.filter(
            course_id=courseId, department_id=departmentId).first()
        if cc is None:
            return Response({'error': '该课程尚未开设此学院'}, status=404)
        action = request.data.get('action', '')
        if action in ('setLeader', 'unsetLeader'):
            needCode = 'user:assign_role'
        elif action in ('addAdminClass', 'removeAdminClass'):
            needCode = 'course:assign_class'
        else:
            needCode = 'class:manage'  # importTeachers / importStudents
        if not hasPerm(request.user, needCode):
            return Response({'error': '没有权限执行该操作'}, status=403)

        if action == 'setLeader':
            teacherNo = str(request.data.get('teacherNo', '')).strip()
            teacher, errMsg = findTeacherByNo(teacherNo)
            if errMsg:
                return Response({'error': errMsg}, status=400)
            setCourseCollegeLeader(cc, teacher)
            return Response({'message': f'已设置 {teacherNo} 为课程组组长'})
        if action == 'unsetLeader':
            leader = cc.leader
            unsetCourseCollegeLeader(cc)
            return Response({'message': f'已撤销组长 {leader.username if leader else ""}'})
        if action == 'importTeachers':
            fileObj = request.FILES.get('file')
            if not fileObj:
                return Response({'error': '请上传 xlsx 文件'}, status=400)
            okList, errList = importTeachersToCourse(parseXlsxRows(fileObj), cc)
            return getImportResponse(okList, errList)
        if action == 'addAdminClass':
            adminClass, errResp = findOr404(
                AdminClass, request.data.get('adminClassId'), '行政班')
            if errResp:
                return errResp
            # 只允许添加本学院的班级（服务端兜底校验，经专业层判断学院归属）
            if adminClass.major.department_id != cc.department_id:
                return Response(
                    {'error': f'班级 {adminClass.name} 不属于该学院'}, status=400)
            cc.admin_classes.add(adminClass)
            return Response({'message': f'已添加班级 {adminClass.name}'})
        if action == 'removeAdminClass':
            adminClass, errResp = findOr404(
                AdminClass, request.data.get('adminClassId'), '行政班')
            if errResp:
                return errResp
            cc.admin_classes.remove(adminClass)
            return Response({'message': f'已移除班级 {adminClass.name}'})
        if action == 'importStudents':
            fileObj = request.FILES.get('file')
            if not fileObj:
                return Response({'error': '请上传 xlsx 文件'}, status=400)
            okList, errList = importStudentsAuto(parseXlsxRows(fileObj))
            return getImportResponse(okList, errList)
        return Response({'error': '未知操作'}, status=400)


class AdminClassListView(APIView):
    """行政班列表：需要 class:manage（全校班级增删改查）"""
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    def get(self, request):
        adminClasses = AdminClass.objects.all().select_related(
            'major__department').prefetch_related('memberships')
        return Response(AdminClassSerializer(adminClasses, many=True).data)


class AdminClassDetailView(APIView):
    """行政班详情：需要 class:manage；支持导入学生到本班、删除班级"""
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    def get(self, request, adminClassId):
        adminClass, errResp = findOr404(AdminClass, adminClassId, '行政班')
        if errResp:
            return errResp
        return Response(AdminClassSerializer(adminClass).data)

    def delete(self, request, adminClassId):
        """删除行政班：成员关系级联删除，学生账号保留"""
        adminClass, errResp = findOr404(AdminClass, adminClassId, '行政班')
        if errResp:
            return errResp
        adminClassName = adminClass.name
        adminClass.delete()
        return Response({'message': f'已删除班级 {adminClassName}'})

    def post(self, request, adminClassId):
        """导入学生到本班：需要 class:manage"""
        adminClass, errResp = findOr404(AdminClass, adminClassId, '行政班')
        if errResp:
            return errResp
        action = request.data.get('action', '')
        if action != 'importStudents':
            return Response({'error': '未知操作'}, status=400)
        fileObj = request.FILES.get('file')
        if not fileObj:
            return Response({'error': '请上传 xlsx 文件'}, status=400)
        okList, errList = importStudents(parseXlsxRows(fileObj), adminClass)
        return getImportResponse(okList, errList)


class TeachingClassListView(APIView):
    """教学班列表与组班：列表需要 course:manage；创建需要 course:assign_class

    GET ?cc=<courseCollegeId> 过滤某个课程×学院下的教学班。
    """
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request):
        teachingClasses = TeachingClass.objects.all()
        ccId = request.query_params.get('cc')
        if ccId:
            teachingClasses = teachingClasses.filter(course_college_id=ccId)
        return Response(TeachingClassSerializer(teachingClasses, many=True).data)

    def post(self, request):
        """组教学班：名称 + 课程×学院 + 组成行政班（至少一个），默认继承教师池"""
        if not hasPerm(request.user, 'course:assign_class'):
            return Response({'error': '没有权限执行该操作'}, status=403)
        cc, errResp = findOr404(
            CourseCollege, request.data.get('courseCollegeId'), '课程×学院')
        if errResp:
            return errResp
        name = str(request.data.get('name', '')).strip()
        if not name:
            return Response({'error': '教学班名称不能为空'}, status=400)
        term = str(request.data.get('term', '')).strip() or CURRENT_TERM
        # 组成班级必须都属于该课程×学院（服务端兜底，防伪造请求）
        adminClassIds = list(request.data.get('adminClassIds', []))
        adminClasses = AdminClass.objects.filter(
            id__in=adminClassIds, course_colleges=cc)
        if not adminClasses or len(adminClasses) != len(set(adminClassIds)):
            return Response(
                {'error': '组成班级无效，至少选择一个本课程的行政班'}, status=400)
        if TeachingClass.objects.filter(
                course_college=cc, term=term, name=name).exists():
            return Response({'error': f'同学期已存在同名教学班 {name}'}, status=400)
        try:
            teachingClass = createTeachingClass(cc, name, term, adminClasses)
        except ValidationError as err:
            return Response({'error': '；'.join(err.messages)}, status=400)
        return Response(TeachingClassSerializer(teachingClass).data, status=201)


class TeachingClassDetailView(APIView):
    """教学班详情与设置：GET 详情；POST 动作管理组成班级/教师；DELETE 解散"""
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request, teachingClassId):
        teachingClass, errResp = findOr404(
            TeachingClass, teachingClassId, '教学班')
        if errResp:
            return errResp
        return Response(TeachingClassSerializer(teachingClass).data)

    def delete(self, request, teachingClassId):
        """解散教学班：组成班级、教师、学生数据都保留（教学班是授课组织）"""
        if not hasPerm(request.user, 'class:manage'):
            return Response({'error': '没有权限执行该操作'}, status=403)
        teachingClass, errResp = findOr404(
            TeachingClass, teachingClassId, '教学班')
        if errResp:
            return errResp
        teachingClassName = teachingClass.name
        teachingClass.delete()
        return Response({'message': f'已解散教学班 {teachingClassName}'})

    def post(self, request, teachingClassId):
        teachingClass, errResp = findOr404(
            TeachingClass, teachingClassId, '教学班')
        if errResp:
            return errResp
        action = request.data.get('action', '')
        if action in ('addAdminClass', 'removeAdminClass'):
            # 组班调整属于班级分配：course:assign_class
            if not hasPerm(request.user, 'course:assign_class'):
                return Response({'error': '没有权限执行该操作'}, status=403)
            adminClass, errResp = findOr404(
                AdminClass, request.data.get('adminClassId'), '行政班')
            if errResp:
                return errResp
            cc = teachingClass.course_college
            if action == 'addAdminClass':
                # 只能添加本课程×学院候选池里的班级（服务端兜底）
                if not cc.admin_classes.filter(id=adminClass.id).exists():
                    return Response(
                        {'error': f'班级 {adminClass.name} 不在本课程×学院中'}, status=400)
                try:
                    with transaction.atomic():
                        teachingClass.admin_classes.add(adminClass)
                except ValidationError as err:
                    return Response({'error': '；'.join(err.messages)}, status=400)
                return Response({'message': f'已添加班级 {adminClass.name}'})
            # removeAdminClass：教学班至少保留一个组成班级
            if teachingClass.admin_classes.count() <= 1:
                return Response({'error': '教学班至少保留一个组成班级'}, status=400)
            teachingClass.admin_classes.remove(adminClass)
            return Response({'message': f'已移除班级 {adminClass.name}'})
        if action in ('addTeacher', 'removeTeacher'):
            # 分配教师：需要 class:assign_teacher
            if not hasPerm(request.user, 'class:assign_teacher'):
                return Response({'error': '没有权限执行该操作'}, status=403)
            cc = teachingClass.course_college
            if action == 'addTeacher':
                # 两种方式：下拉框选教师（teacherId）或直接输工号（teacherNo）
                teacherNo = str(request.data.get('teacherNo', '')).strip()
                if teacherNo:
                    teacher, errMsg = findTeacherByNo(teacherNo)
                    if errMsg:
                        return Response({'error': errMsg}, status=400)
                    # 工号添加的教师自动加入本课程教师池
                    cc.teachers.add(teacher)
                else:
                    teacher = User.objects.filter(
                        id=request.data.get('teacherId')).first()
                    if teacher is None:
                        return Response({'error': '教师不存在'}, status=400)
                    # 带课程上下文时，教师必须在池内（防伪造请求）
                    if not cc.teachers.filter(id=teacher.id).exists():
                        return Response(
                            {'error': '该教师不在本课程教师池中'}, status=400)
                teachingClass.teachers.add(teacher)
                return Response(
                    {'message': f'已添加教师 {teacher.name or teacher.username}'})
            teacher = User.objects.filter(
                id=request.data.get('teacherId')).first()
            if teacher is None:
                return Response({'error': '教师不存在'}, status=400)
            teachingClass.teachers.remove(teacher)
            return Response(
                {'message': f'已移除教师 {teacher.name or teacher.username}'})
        return Response({'error': '未知操作'}, status=400)


class ImportView(APIView):
    """批量导入：POST multipart {type, file(xlsx)}

    基础门槛 class:manage；按导入内容细分：
    导入课程 -> course:manage，导入教师/学生 -> class:manage；
    学生导入按学号格式自动分配学院、专业和班级，不再需要目标班级参数。
    """
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    def post(self, request):
        importType = request.data.get('type', '')
        needCode = 'course:manage' if importType == 'course' else 'class:manage'
        if not hasPerm(request.user, needCode):
            return Response({'error': '没有权限执行该导入'}, status=403)
        fileObj = request.FILES.get('file')
        if not fileObj:
            return Response({'error': '请上传 xlsx 文件'}, status=400)
        rows = parseXlsxRows(fileObj)
        if importType == 'course':
            okList, errList = importCourses(rows)
        elif importType == 'teacher':
            okList, errList = importTeachers(rows)
        elif importType == 'student':
            okList, errList = importStudentsAuto(rows)
        else:
            return Response({'error': '未知导入类型'}, status=400)
        return getImportResponse(okList, errList)
