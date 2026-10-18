"""教师工作台 API（前后端分离主线，挂在 /api/teacher/ 前缀下）

安全说明：教师的所有权限仅限教学班级。
- 列表接口数据天然只含当前教师教的班级（teachers 过滤）
- 详情接口先过 getTeacherClazz 对象级校验，非本班教师一律 404
- 后续功能接口（通知/作业等）都必须沿用这套校验
"""
from rest_framework.views import APIView
from rest_framework.response import Response

from academics.models import Clazz
from academics.serializers import ClazzSerializer
from users.permissions import IsTeacherGroup

from .helpers import getTeacherClazz
from .serializers import TeacherClazzSerializer


class TeacherClazzListView(APIView):
    """我的教学班级列表：GET /api/teacher/clazzes/

    入口校验：仅教师组（或超级用户）；返回当前教师教的全部班级
    （跨课程跨学院），按学院、班级名排序。
    """
    permission_classes = [IsTeacherGroup]

    def get(self, request):
        clazzes = Clazz.objects.filter(teachers=request.user).distinct() \
            .select_related('college') \
            .order_by('college__name', 'name')
        return Response(TeacherClazzSerializer(clazzes, many=True).data)


class TeacherClazzDetailView(APIView):
    """教师视角班级详情：GET /api/teacher/clazzes/<id>/

    入口校验：仅教师组（或超级用户）；
    对象级校验：班级教师表必须包含当前用户（超级用户放行），否则 404。
    返回班级信息、学院、课程、学生列表、同班教师（复用教务的 ClazzSerializer）。
    """
    permission_classes = [IsTeacherGroup]

    def get(self, request, clazzId):
        clazz = getTeacherClazz(request.user, clazzId)
        return Response(ClazzSerializer(clazz).data)
