"""教务业务 API：学院、课程、课程×学院、行政班、教学班、批量导入（前后端分离用）

每个视图类声明 permCode（所需权限码），由 PermRequired 统一校验；
写操作再按动作细分权限码，无权限时返回 403。
权限码与 xmind《用户权限》一一对应。

V2 说明：教师任课挂教学班（不再挂行政班），
教学班管理（组班/教师分配/解散）统一在 TeachingClass* 视图。
"""
from collections.abc import Mapping
from functools import wraps

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F
from rest_framework.exceptions import ValidationError as RequestValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import User
from users.permissions import PermRequired, hasPerm

from .models import AdminClass, Course, CourseCollege, Department, TeachingClass
from .services import (createTeachingClass, deleteAdminClass, deleteCourseCollege,
                       deleteTeachingClass, removeCandidateAdminClass,
                       removeTeachingAdminClass)
from .serializers import (AdminClassSerializer, CourseCollegeSerializer,
                          CourseSerializer, DepartmentSerializer,
                          TeachingClassSerializer)
from .audit_service import organizationState, recordAudit
from .term_service import currentTermCode, requireClassWrite
from .models import AcademicTerm
from .views import (findTeacherByNo, importCourses,
                    importStudents, importStudentsAuto, importTeachers,
                    importTeachersToCourse, parseXlsxRows,
                    setCourseCollegeLeader, unsetCourseCollegeLeader)


def parseObjectId(value, field='id'):
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 19:
        value = int(value)
    if type(value) is not int or not 0 < value < 2 ** 63:
        raise RequestValidationError({field: '必须是有效的正整数'})
    return value


def objectPayload(payload, allowed):
    if not isinstance(payload, Mapping) or set(payload) - set(allowed):
        raise RequestValidationError('请求体必须是对象，且只能包含允许的字段')
    return payload


def textField(payload, key, limit, default=None):
    value = payload.get(key, default)
    if not isinstance(value, str) or len(value) > limit:
        raise RequestValidationError({key: f'须为不超过 {limit} 字符的文本'})
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise RequestValidationError({key: '包含无效字符'})
    if '\x00' in value:
        raise RequestValidationError({key: '包含无效字符'})
    value = value.strip()
    if not value:
        if default is not None:
            return default
        raise RequestValidationError({key: '不能为空'})
    return value


def auditOfficeMutation(method):
    @wraps(method)
    def wrapped(view, request, *args, **kwargs):
        # 认证/功能权限由 DRF 在事务外完成；事务内先写后读，避免 SQLite 锁升级。
        # 低频教务组织操作与学期关闭共用锁，审计前后快照与业务原子提交。
        with transaction.atomic():
            AcademicTerm.objects.update(id=F('id'))
            target = view.auditTarget(kwargs)
            before = organizationState(target)
            response = method(view, request, *args, **kwargs)
            if response.status_code >= 400:
                transaction.set_rollback(True)
                return response
            after = view.auditTarget(kwargs, response)
            state = organizationState(after)
            if 'successCount' in getattr(response, 'data', {}):
                state['successCount'] = response.data['successCount']
                state['errorCount'] = len(response.data.get('errors', []))
            action = request.data.get('action', request.method)
            recordAudit(request.user, f'office.{view.__class__.__name__}.{action}'[:100],
                        after or target, before, state)
            return response
    return wrapped


class OfficeAPIView(APIView):

    def auditTarget(self, kwargs, response=None):
        if 'teachingClassId' in kwargs:
            return TeachingClass.objects.filter(pk=kwargs['teachingClassId']).first()
        if 'adminClassId' in kwargs:
            return AdminClass.objects.filter(pk=kwargs['adminClassId']).first()
        if 'courseId' in kwargs and 'departmentId' in kwargs:
            return CourseCollege.objects.filter(course_id=kwargs['courseId'],
                                                 department_id=kwargs['departmentId']).first()
        if 'courseId' in kwargs:
            return Course.objects.filter(pk=kwargs['courseId']).first()
        if response is not None and self.__class__.__name__ == 'TeachingClassListView':
            return TeachingClass.objects.filter(pk=response.data.get('id')).first()
        return None

    def handle_exception(self, exc):
        if isinstance(exc, ValidationError):
            return Response({'error': '；'.join(exc.messages)}, status=400)
        return super().handle_exception(exc)


def findOr404(modelClass, objectId, name):
    """通用取对象：不存在返回 (None, 404 响应)"""
    obj = modelClass.objects.filter(id=parseObjectId(objectId)).first()
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


class DepartmentListView(OfficeAPIView):
    """学院列表：需要 course:manage（学院由学号解析自动生成，教务查看用）"""
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request):
        departments = Department.objects.annotate(
            adminClassCount=Count('majors__admin_classes', distinct=True))
        return Response(DepartmentSerializer(departments, many=True).data)


class CourseListView(OfficeAPIView):
    """课程列表：需要 course:manage（全校课程增删改查）"""
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request):
        courses = Course.objects.all().prefetch_related('course_colleges__department')
        return Response(CourseSerializer(courses, many=True).data)


class CourseDetailView(OfficeAPIView):
    """课程详情与学院分配"""
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request, courseId):
        course, errResp = findOr404(Course, courseId, '课程')
        if errResp:
            return errResp
        return Response(CourseSerializer(course).data)

    @auditOfficeMutation
    def post(self, request, courseId):
        """添加/移除开设学院：动作级校验，需要 course:assign_class"""
        if not hasPerm(request.user, 'course:assign_class'):
            return Response({'error': '没有权限执行该操作'}, status=403)
        course, errResp = findOr404(Course, courseId, '课程')
        if errResp:
            return errResp
        payload = objectPayload(request.data, {'action', 'departmentId'})
        action = textField(payload, 'action', 40)
        if action not in ('addDepartment', 'removeDepartment'):
            return Response({'error': '未知操作'}, status=400)
        departmentId = payload.get('departmentId')
        department, errResp = findOr404(Department, departmentId, '学院')
        if errResp:
            return errResp
        if action == 'addDepartment':
            if not course.is_active or not department.is_active:
                raise ValidationError('课程或学院未启用，不能开设课程')
            cc, created = CourseCollege.objects.get_or_create(
                course=course, department=department)
            return Response({'message': f'已为课程开设学院 {department.name}'})
        if action == 'removeDepartment':
            cc = CourseCollege.objects.filter(
                course=course, department=department).first()
            if cc is not None:
                deleteCourseCollege(cc)
            return Response({'message': f'已移除学院 {department.name}'})
        return Response({'error': '未知操作'}, status=400)


class CourseCollegeView(OfficeAPIView):
    """课程×学院 详情与设置：组长、教师池、行政班"""
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request, courseId, departmentId):
        courseId = parseObjectId(courseId, 'courseId')
        departmentId = parseObjectId(departmentId, 'departmentId')
        cc = CourseCollege.objects.filter(
            course_id=courseId, department_id=departmentId).first()
        if cc is None:
            return Response({'error': '该课程尚未开设此学院'}, status=404)
        return Response(CourseCollegeSerializer(cc).data)

    @auditOfficeMutation
    def post(self, request, courseId, departmentId):
        """按动作细分权限码：设组长=user:assign_role、班级分配=course:assign_class、
        教师/学生导入=class:manage"""
        courseId = parseObjectId(courseId, 'courseId')
        departmentId = parseObjectId(departmentId, 'departmentId')
        cc = CourseCollege.objects.filter(
            course_id=courseId, department_id=departmentId).first()
        if cc is None:
            return Response({'error': '该课程尚未开设此学院'}, status=404)
        payload = objectPayload(request.data, {
            'action', 'teacherNo', 'adminClassId', 'file'})
        action = textField(payload, 'action', 40)
        actionFields = {
            'setLeader': {'teacherNo'}, 'unsetLeader': set(),
            'addAdminClass': {'adminClassId'}, 'removeAdminClass': {'adminClassId'},
            'importTeachers': {'file'}, 'importStudents': {'file'},
        }
        if action not in actionFields:
            return Response({'error': '未知操作'}, status=400)
        objectPayload(payload, {'action'} | actionFields[action])
        if action in ('setLeader', 'unsetLeader'):
            needCode = 'user:assign_role'
        elif action in ('addAdminClass', 'removeAdminClass'):
            needCode = 'course:assign_class'
        else:
            needCode = 'class:manage'  # importTeachers / importStudents
        if not hasPerm(request.user, needCode):
            return Response({'error': '没有权限执行该操作'}, status=403)

        if action == 'setLeader':
            teacherNo = textField(payload, 'teacherNo', 32)
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
            if not hasPerm(request.user, 'class:assign_teacher'):
                return Response({'error': '导入课程教师需要任课分配权限'}, status=403)
            fileObj = request.FILES.get('file')
            if not fileObj:
                return Response({'error': '请上传 xlsx 文件'}, status=400)
            okList, errList = importTeachersToCourse(
                parseXlsxRows(fileObj), cc, actor=request.user)
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
            removeCandidateAdminClass(cc, adminClass)
            return Response({'message': f'已移除班级 {adminClass.name}'})
        if action == 'importStudents':
            fileObj = request.FILES.get('file')
            if not fileObj:
                return Response({'error': '请上传 xlsx 文件'}, status=400)
            okList, errList = importStudentsAuto(parseXlsxRows(fileObj))
            return getImportResponse(okList, errList)
        return Response({'error': '未知操作'}, status=400)


class AdminClassListView(OfficeAPIView):
    """行政班列表：需要 class:manage（全校班级增删改查）"""
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    def get(self, request):
        adminClasses = AdminClass.objects.all().select_related(
            'major__department').prefetch_related('memberships')
        return Response(AdminClassSerializer(adminClasses, many=True).data)


class AdminClassDetailView(OfficeAPIView):
    """行政班详情：需要 class:manage；支持导入学生到本班、删除班级"""
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    def get(self, request, adminClassId):
        adminClass, errResp = findOr404(AdminClass, adminClassId, '行政班')
        if errResp:
            return errResp
        return Response(AdminClassSerializer(adminClass).data)

    @auditOfficeMutation
    def delete(self, request, adminClassId):
        """仅允许删除没有成员历史且未组成教学班的闲置行政班。"""
        adminClass, errResp = findOr404(AdminClass, adminClassId, '行政班')
        if errResp:
            return errResp
        adminClassName = adminClass.name
        deleteAdminClass(adminClass)
        return Response({'message': f'已删除班级 {adminClassName}'})

    @auditOfficeMutation
    def post(self, request, adminClassId):
        """导入学生到本班：需要 class:manage"""
        adminClass, errResp = findOr404(AdminClass, adminClassId, '行政班')
        if errResp:
            return errResp
        payload = objectPayload(request.data, {'action', 'file'})
        action = textField(payload, 'action', 40)
        if action != 'importStudents':
            return Response({'error': '未知操作'}, status=400)
        fileObj = request.FILES.get('file')
        if not fileObj:
            return Response({'error': '请上传 xlsx 文件'}, status=400)
        okList, errList = importStudents(parseXlsxRows(fileObj), adminClass)
        return getImportResponse(okList, errList)


class TeachingClassListView(OfficeAPIView):
    """教学班列表与组班：列表需要 course:manage；创建需要 course:assign_class

    GET ?cc=<courseCollegeId> 过滤某个课程×学院下的教学班。
    """
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request):
        teachingClasses = TeachingClass.objects.all()
        ccId = request.query_params.get('cc')
        if ccId:
            teachingClasses = teachingClasses.filter(course_college_id=parseObjectId(ccId, 'cc'))
        if 'term' in request.query_params:
            term = textField(request.query_params, 'term', 20)
            teachingClasses = teachingClasses.filter(term=term)
        return Response(TeachingClassSerializer(teachingClasses, many=True).data)

    @auditOfficeMutation
    def post(self, request):
        """组教学班：名称 + 课程×学院 + 组成行政班（至少一个），默认继承教师池"""
        if not hasPerm(request.user, 'course:assign_class'):
            return Response({'error': '没有权限执行该操作'}, status=403)
        payload = objectPayload(request.data, {
            'courseCollegeId', 'name', 'term', 'adminClassIds'})
        cc, errResp = findOr404(
            CourseCollege, payload.get('courseCollegeId'), '课程×学院')
        if errResp:
            return errResp
        name = textField(payload, 'name', 50)
        term = textField(payload, 'term', 20) if 'term' in payload else currentTermCode()
        if not AcademicTerm.objects.filter(code=term).exists():
            raise ValidationError('请先在学期管理中登记该学期')
        # 组成班级必须都属于该课程×学院（服务端兜底，防伪造请求）
        rawIds = payload.get('adminClassIds')
        if not isinstance(rawIds, list) or not rawIds:
            raise RequestValidationError({'adminClassIds': '必须是非空的行政班编号数组'})
        adminClassIds = [parseObjectId(value, 'adminClassIds') for value in rawIds]
        if len(set(adminClassIds)) != len(adminClassIds):
            raise RequestValidationError({'adminClassIds': '不能包含重复编号'})
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


class TeachingClassDetailView(OfficeAPIView):
    """教学班详情与设置：GET 详情；POST 动作管理组成班级/教师；DELETE 解散"""
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request, teachingClassId):
        teachingClass, errResp = findOr404(
            TeachingClass, teachingClassId, '教学班')
        if errResp:
            return errResp
        return Response(TeachingClassSerializer(teachingClass).data)

    @auditOfficeMutation
    def delete(self, request, teachingClassId):
        """解散教学班：组成班级、教师、学生数据都保留（教学班是授课组织）"""
        if not hasPerm(request.user, 'class:manage'):
            return Response({'error': '没有权限执行该操作'}, status=403)
        teachingClass, errResp = findOr404(
            TeachingClass, teachingClassId, '教学班')
        if errResp:
            return errResp
        teachingClassName = teachingClass.name
        deleteTeachingClass(teachingClass)
        return Response({'message': f'已解散教学班 {teachingClassName}'})

    @auditOfficeMutation
    def post(self, request, teachingClassId):
        teachingClass, errResp = findOr404(
            TeachingClass, teachingClassId, '教学班')
        if errResp:
            return errResp
        requireClassWrite(teachingClass)
        payload = objectPayload(request.data, {
            'action', 'adminClassId', 'teacherId', 'teacherNo'})
        action = textField(payload, 'action', 40)
        actionFields = {
            'addAdminClass': {'adminClassId'}, 'removeAdminClass': {'adminClassId'},
            'addTeacher': {'teacherId', 'teacherNo'}, 'removeTeacher': {'teacherId'},
        }
        if action not in actionFields:
            return Response({'error': '未知操作'}, status=400)
        objectPayload(payload, {'action'} | actionFields[action])
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
            removeTeachingAdminClass(teachingClass, adminClass)
            return Response({'message': f'已移除班级 {adminClass.name}'})
        if action in ('addTeacher', 'removeTeacher'):
            # 分配教师：需要 class:assign_teacher
            if not hasPerm(request.user, 'class:assign_teacher'):
                return Response({'error': '没有权限执行该操作'}, status=403)
            cc = teachingClass.course_college
            if action == 'addTeacher':
                # 两种方式：下拉框选教师（teacherId）或直接输工号（teacherNo）
                teacherNo = textField(payload, 'teacherNo', 32, '')
                if teacherNo:
                    if payload.get('teacherId') is not None:
                        raise RequestValidationError('teacherId 与 teacherNo 只能选择一种')
                    teacher, errMsg = findTeacherByNo(teacherNo)
                    if errMsg:
                        return Response({'error': errMsg}, status=400)
                    # 工号添加的教师自动加入本课程教师池
                    cc.teachers.add(teacher)
                else:
                    teacher = User.objects.filter(
                        id=parseObjectId(payload.get('teacherId'), 'teacherId')).first()
                    if teacher is None:
                        return Response({'error': '教师不存在'}, status=400)
                    # 带课程上下文时，教师必须在池内（防伪造请求）
                    if not cc.teachers.filter(id=teacher.id).exists():
                        return Response(
                            {'error': '该教师不在本课程教师池中'}, status=400)
                if not teacher.is_active:
                    raise ValidationError('已停用的教师不能新增任课关系')
                teachingClass.teachers.add(teacher)
                return Response(
                    {'message': f'已添加教师 {teacher.name or teacher.username}'})
            teacher = User.objects.filter(
                id=parseObjectId(payload.get('teacherId'), 'teacherId')).first()
            if teacher is None:
                return Response({'error': '教师不存在'}, status=400)
            teachingClass.teachers.remove(teacher)
            return Response(
                {'message': f'已移除教师 {teacher.name or teacher.username}'})
        return Response({'error': '未知操作'}, status=400)


class ImportView(OfficeAPIView):
    """批量导入：POST multipart {type, file(xlsx)}

    基础门槛 class:manage；按导入内容细分：
    导入课程 -> course:manage，导入教师/学生 -> class:manage；
    学生导入按学号格式自动分配学院、专业和班级，不再需要目标班级参数。
    """
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    @auditOfficeMutation
    def post(self, request):
        payload = objectPayload(request.data, {'type', 'file'})
        importType = textField(payload, 'type', 10)
        if importType not in ('course', 'teacher', 'student'):
            return Response({'error': '未知导入类型'}, status=400)
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
