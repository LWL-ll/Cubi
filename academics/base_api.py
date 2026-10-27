"""教务基础维护接口；单独使用 office 路径保留旧列表返回契约。"""
from django.core.exceptions import ValidationError
from django.db.models import Q
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import PermRequired
from .base_service import (MODELS, baseData, deleteBase, getBase, integerId,
                           saveBase, textValue)


class OfficeManagementView(APIView):
    permission_classes = [PermRequired]

    def handle_exception(self, exc):
        if isinstance(exc, ValidationError):
            return Response({'error': '；'.join(exc.messages)}, status=400)
        return super().handle_exception(exc)


class OfficeBaseListView(OfficeManagementView):
    kind = None
    permCode = 'course:manage'

    def get(self, request):
        queryset = MODELS[self.kind].objects.all().order_by('pk')
        if 'isActive' in request.query_params:
            value = request.query_params['isActive']
            if value not in ('true', 'false'):
                raise ValidationError('isActive 只能为 true 或 false')
            queryset = queryset.filter(is_active=value == 'true')
        if 'q' in request.query_params:
            value = textValue(request.query_params['q'], 100, 'q', blank=True)
            queryset = queryset.filter(Q(name__icontains=value) | Q(code__icontains=value))
        for field, lookup in (('departmentId', 'department_id'), ('majorId', 'major_id')):
            if field in request.query_params:
                if ((field == 'departmentId' and self.kind != 'majors')
                        or (field == 'majorId' and self.kind != 'admin-classes')):
                    raise ValidationError(f'{field} 不适用于此列表')
                queryset = queryset.filter(**{lookup: integerId(request.query_params[field], field)})
        paginator = PageNumberPagination()
        paginator.page_size = 20
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response([baseData(obj) for obj in page])

    def post(self, request):
        return Response(baseData(saveBase(request.user, self.kind, request.data)), status=201)


class OfficeBaseDetailView(OfficeManagementView):
    kind = None
    permCode = 'course:manage'

    def get(self, request, objectId):
        return Response(baseData(getBase(self.kind, objectId)))

    def patch(self, request, objectId):
        return Response(baseData(saveBase(request.user, self.kind, request.data, objectId)))

    def delete(self, request, objectId):
        deleteBase(request.user, self.kind, objectId)
        return Response(status=204)


class OfficeDepartmentListView(OfficeBaseListView):
    kind = 'departments'


class OfficeDepartmentDetailView(OfficeBaseDetailView):
    kind = 'departments'


class OfficeMajorListView(OfficeBaseListView):
    kind = 'majors'


class OfficeMajorDetailView(OfficeBaseDetailView):
    kind = 'majors'


class OfficeCourseListView(OfficeBaseListView):
    kind = 'courses'


class OfficeCourseDetailView(OfficeBaseDetailView):
    kind = 'courses'


class OfficeAdminClassListView(OfficeBaseListView):
    kind = 'admin-classes'
    permCode = 'class:manage'


class OfficeAdminClassDetailView(OfficeBaseDetailView):
    kind = 'admin-classes'
    permCode = 'class:manage'
