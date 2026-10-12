"""教务业务 API：课程、学院、课程×学院、班级、批量导入（前后端分离用）

每个视图类声明 permCode（所需权限码），由 PermRequired 统一校验；
写操作再按动作细分权限码，无权限时返回 403。
权限码与 xmind《用户权限》一一对应。
"""
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import User
from users.permissions import PermRequired, hasPerm

from .models import Clazz, College, Course, CourseCollege
from .serializers import (ClazzSerializer, CollegeSerializer, CourseCollegeSerializer,
                          CourseSerializer, UserBriefSerializer)
from .views import (findTeacherByNo, importCourses, importStudents,
                    importStudentsAuto, importTeachers, importTeachersToCourse,
                    parseXlsxRows, setCourseCollegeLeader, unsetCourseCollegeLeader)


def findOr404(modelClass, objectId, name):
    """通用取对象：不存在返回 (None, 404 响应)"""
    obj = modelClass.objects.filter(id=objectId).first()
    if obj is None:
        return None, Response({'error': f'{name}不存在'}, status=404)
    return obj, None


class CollegeListView(APIView):
    """学院列表：需要 course:manage（学院由学号解析自动生成，教务查看用）"""
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request):
        colleges = College.objects.all()
        return Response(CollegeSerializer(colleges, many=True).data)


class CourseListView(APIView):
    """课程列表：需要 course:manage（全校课程增删改查）"""
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request):
        courses = Course.objects.all().prefetch_related('course_colleges__college')
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
        collegeId = request.data.get('collegeId')
        college, errResp = findOr404(College, collegeId, '学院')
        if errResp:
            return errResp
        if action == 'addCollege':
            cc, created = CourseCollege.objects.get_or_create(course=course, college=college)
            return Response({'message': f'已为课程开设学院 {college.name}'})
        if action == 'removeCollege':
            # 删除课程×学院记录，其下的组长、教师池、班级分配一并解除
            CourseCollege.objects.filter(course=course, college=college).delete()
            return Response({'message': f'已移除学院 {college.name}'})
        return Response({'error': '未知操作'}, status=400)


class CourseCollegeView(APIView):
    """课程×学院 详情与设置：组长、教师池、班级"""
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request, courseId, collegeId):
        cc = CourseCollege.objects.filter(course_id=courseId, college_id=collegeId).first()
        if cc is None:
            return Response({'error': '该课程尚未开设此学院'}, status=404)
        return Response(CourseCollegeSerializer(cc).data)

    def post(self, request, courseId, collegeId):
        """按动作细分权限码：设组长=user:assign_role、教师/学生导入=class:manage、
        班级分配=course:assign_class"""
        cc = CourseCollege.objects.filter(course_id=courseId, college_id=collegeId).first()
        if cc is None:
            return Response({'error': '该课程尚未开设此学院'}, status=404)
        action = request.data.get('action', '')
        if action in ('setLeader', 'unsetLeader'):
            needCode = 'user:assign_role'
        elif action in ('addClazz', 'removeClazz'):
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
            return Response({
                'successCount': len(okList),
                'errors': [{'line': lineNo, 'reason': reason} for lineNo, reason in errList],
            })
        if action == 'addClazz':
            clazz, errResp = findOr404(Clazz, request.data.get('clazzId'), '班级')
            if errResp:
                return errResp
            # 只允许添加本学院的班级（服务端兜底校验）
            if clazz.college_id != cc.college_id:
                return Response({'error': f'班级 {clazz.name} 不属于该学院'}, status=400)
            cc.clazzes.add(clazz)
            return Response({'message': f'已添加班级 {clazz.name}'})
        if action == 'removeClazz':
            clazz, errResp = findOr404(Clazz, request.data.get('clazzId'), '班级')
            if errResp:
                return errResp
            cc.clazzes.remove(clazz)
            return Response({'message': f'已移除班级 {clazz.name}'})
        if action == 'importStudents':
            fileObj = request.FILES.get('file')
            if not fileObj:
                return Response({'error': '请上传 xlsx 文件'}, status=400)
            okList, errList = importStudentsAuto(parseXlsxRows(fileObj))
            return Response({
                'successCount': len(okList),
                'errors': [{'line': lineNo, 'reason': reason} for lineNo, reason in errList],
            })
        return Response({'error': '未知操作'}, status=400)


class ClazzListView(APIView):
    """班级列表：需要 class:manage（全校班级增删改查）"""
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    def get(self, request):
        clazzes = Clazz.objects.all().select_related('college').prefetch_related(
            'students', 'teachers', 'course_colleges__course')
        return Response(ClazzSerializer(clazzes, many=True).data)


class ClazzDetailView(APIView):
    """班级详情与教师分配：需要 class:manage；删除班级走 DELETE

    教师下拉候选按课程×学院教师池过滤：
    - GET 传 ?cc=<courseCollegeId>：返回该课程×学院教师池中未分配到本班的教师（availableTeachers）
    - POST addTeacher 传 ccId：校验教师必须在池内；工号添加的教师自动加入该池
    """
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    def get(self, request, clazzId):
        clazz, errResp = findOr404(Clazz, clazzId, '班级')
        if errResp:
            return errResp
        data = ClazzSerializer(clazz).data
        # 带课程×学院上下文时，返回教师池中未分配到本班的教师
        cc = self.getContextCc(request, clazz)
        if cc is not None:
            assignedIds = clazz.teachers.values_list('id', flat=True)
            available = cc.teachers.exclude(id__in=assignedIds)
            data['availableTeachers'] = UserBriefSerializer(available, many=True).data
        else:
            data['availableTeachers'] = []
        return Response(data)

    def delete(self, request, clazzId):
        """删除班级：学生和教师账号保留，课程分配解除"""
        clazz, errResp = findOr404(Clazz, clazzId, '班级')
        if errResp:
            return errResp
        clazzName = clazz.name
        clazz.delete()
        return Response({'message': f'已删除班级 {clazzName}'})

    def getContextCc(self, request, clazz):
        """解析请求中的课程×学院上下文（查询参数 cc 或 body 里的 ccId）

        校验该课程×学院确实包含本班级；无效时返回 None（页面模式已兜底校验）。
        """
        ccId = request.query_params.get('cc') or request.data.get('ccId')
        if not ccId:
            return None
        cc = CourseCollege.objects.filter(id=ccId).first()
        if cc is None or not cc.clazzes.filter(id=clazz.id).exists():
            return None
        return cc

    def post(self, request, clazzId):
        """分配/移除教师或导入学生，按动作细分权限码"""
        clazz, errResp = findOr404(Clazz, clazzId, '班级')
        if errResp:
            return errResp
        action = request.data.get('action', '')
        cc = self.getContextCc(request, clazz)
        if action in ('addTeacher', 'removeTeacher'):
            # 分配教师：需要 class:assign_teacher
            if not hasPerm(request.user, 'class:assign_teacher'):
                return Response({'error': '没有权限执行该操作'}, status=403)
            if action == 'addTeacher':
                # 两种方式：下拉框选教师（teacherId）或直接输工号（teacherNo）
                teacherNo = str(request.data.get('teacherNo', '')).strip()
                if teacherNo:
                    teacher, errMsg = findTeacherByNo(teacherNo)
                    if errMsg:
                        return Response({'error': errMsg}, status=400)
                    # 工号添加的教师自动加入本课程教师池
                    if cc is not None:
                        cc.teachers.add(teacher)
                else:
                    teacher = User.objects.filter(id=request.data.get('teacherId')).first()
                    if teacher is None:
                        return Response({'error': '教师不存在'}, status=400)
                    # 带课程上下文时，教师必须在池内（防伪造请求）
                    if cc is not None and not cc.teachers.filter(id=teacher.id).exists():
                        return Response({'error': '该教师不在本课程教师池中'}, status=400)
                clazz.teachers.add(teacher)
                return Response({'message': f'已添加教师 {teacher.name or teacher.username}'})
            teacher = User.objects.filter(id=request.data.get('teacherId')).first()
            if teacher is None:
                return Response({'error': '教师不存在'}, status=400)
            clazz.teachers.remove(teacher)
            return Response({'message': f'已移除教师 {teacher.name or teacher.username}'})
        if action == 'importStudents':
            # 导入学生属于班级管理：class:manage
            fileObj = request.FILES.get('file')
            if not fileObj:
                return Response({'error': '请上传 xlsx 文件'}, status=400)
            rows = parseXlsxRows(fileObj)
            okList, errList = importStudents(rows, clazz)
            return Response({
                'successCount': len(okList),
                'errors': [{'line': lineNo, 'reason': reason} for lineNo, reason in errList],
            })
        return Response({'error': '未知操作'}, status=400)


class ImportView(APIView):
    """批量导入：POST multipart {type, file(xlsx)}

    基础门槛 class:manage；按导入内容细分：
    导入课程 -> course:manage，导入教师/学生 -> class:manage；
    学生导入按学号格式自动分配学院和班级，不再需要目标班级参数。
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
        return Response({
            'successCount': len(okList),
            'errors': [{'line': lineNo, 'reason': reason} for lineNo, reason in errList],
        })
