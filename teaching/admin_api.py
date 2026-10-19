"""管理员题库 API（仅系统管理员）

端点：
- POST /api/admin/problems/import/：上传 ZIP 批量导入公共题库
  请求：multipart，file=ZIP，overwrite=on/true 可选
  权限：IsSuperUser（is_superuser），普通教师/课程组/教务一律 403
"""
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import IsSuperUser

from .problem_import import importProblemZip


class ProblemImportView(APIView):
    """题库 ZIP 批量导入：仅超级用户"""
    permission_classes = [IsSuperUser]

    def post(self, request):
        zipFile = request.FILES.get('file')
        if zipFile is None:
            raise ValidationError('请上传 ZIP 文件（字段名 file）')
        overwriteRaw = str(request.data.get('overwrite', '')).lower()
        overwrite = overwriteRaw in ('1', 'true', 'yes', 'on')
        result = importProblemZip(request.user, zipFile, overwrite=overwrite)
        return Response(result)
