"""教务学期与只读审计查询接口。"""
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from users.permissions import PermRequired
from .api import OfficeAPIView, objectPayload, parseObjectId, textField
from .models import AcademicTerm, AuditEvent
from .term_service import (archiveTerm, closeTerm, createTerm, setCurrentTerm,
                           termData, updateTerm)


class TermListView(OfficeAPIView):
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request):
        return Response([termData(term) for term in AcademicTerm.objects.all()])

    def post(self, request):
        try:
            term = createTerm(request.user, request.data)
        except IntegrityError:
            raise ValidationError('学期编码已存在') from None
        return Response(termData(term), status=201)


class TermDetailView(OfficeAPIView):
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def get(self, request, termId):
        return Response(termData(get_object_or_404(AcademicTerm, pk=termId)))

    def patch(self, request, termId):
        return Response(termData(updateTerm(request.user, termId, request.data)))


class TermActionView(OfficeAPIView):
    permission_classes = [PermRequired]
    permCode = 'course:manage'

    def post(self, request, termId, action):
        if objectPayload(request.data, set()):
            raise ValidationError('请求体必须为空对象')
        functions = {'current': setCurrentTerm, 'close': closeTerm, 'archive': archiveTerm}
        return Response(termData(functions[action](request.user, termId)))


class AuditListView(OfficeAPIView):
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    def get(self, request):
        query = AuditEvent.objects.all()
        for key, field, limit in (('action', 'action', 100), ('objectType', 'object_type', 100),
                                  ('objectId', 'object_id', 64)):
            if key in request.query_params:
                query = query.filter(**{field: textField(request.query_params, key, limit)})
        if 'actorId' in request.query_params:
            query = query.filter(actor_id=parseObjectId(request.query_params['actorId'], 'actorId'))
        for key, lookup in (('since', 'created_at__gte'), ('until', 'created_at__lte')):
            if key in request.query_params:
                try:
                    raw = request.query_params[key]
                    value = parse_datetime(raw) if len(raw) <= 40 else None
                except ValueError:
                    value = None
                if value is None:
                    raise ValidationError(f'{key} 须为 ISO 时间')
                if timezone.is_naive(value):
                    value = timezone.make_aware(value)
                query = query.filter(**{lookup: value})
        paginator = PageNumberPagination()
        paginator.page_size = 50
        page = paginator.paginate_queryset(query, request)
        return paginator.get_paginated_response([{
            'id': row.pk, 'actorId': row.actor_id, 'actorUsername': row.actor_username,
            'action': row.action, 'objectType': row.object_type, 'objectId': row.object_id,
            'before': row.before, 'after': row.after, 'createdAt': row.created_at,
        } for row in page])
