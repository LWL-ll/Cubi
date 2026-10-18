"""教师工作台 API（前后端分离主线，挂在 /api/teacher/ 前缀下）

安全说明：教师的所有权限仅限教学班级。
- 列表接口数据天然只含当前教师教的教学班（teachers 过滤）
- 详情接口先过 getTeacherTeachingClass 对象级校验，非本班教师一律 404
- 后续功能接口（通知/作业等）都必须沿用这套校验
"""
from rest_framework.views import APIView
from rest_framework.response import Response

from academics.models import TeachingClass
from academics.serializers import TeachingClassSerializer
from users.permissions import IsTeacherGroup

from .helpers import getTeacherTeachingClass
from .serializers import TeacherTeachingClassSerializer


class TeacherTeachingClassListView(APIView):
    """我的教学班级列表：GET /api/teacher/teaching-classes/

    入口校验：仅教师组（或超级用户）；返回当前教师教的全部教学班
    （跨课程跨学院），按课程名、教学班名排序。
    """
    permission_classes = [IsTeacherGroup]

    def get(self, request):
        teachingClasses = TeachingClass.objects.filter(
            teachers=request.user).distinct() \
            .select_related('course_college__course', 'course_college__department') \
            .order_by('course_college__course__name', 'name')
        return Response(TeacherTeachingClassSerializer(teachingClasses, many=True).data)


class TeacherTeachingClassDetailView(APIView):
    """教师视角教学班详情：GET /api/teacher/teaching-classes/<id>/

    入口校验：仅教师组（或超级用户）；
    对象级校验：教学班教师表必须包含当前用户（超级用户放行），否则 404。
    返回教学班信息、课程、学院、组成班级、学生名单、同班教师
    （复用教务的 TeachingClassSerializer）。
    """
    permission_classes = [IsTeacherGroup]

    def get(self, request, teachingClassId):
        teachingClass = getTeacherTeachingClass(request.user, teachingClassId)
        return Response(TeachingClassSerializer(teachingClass).data)
