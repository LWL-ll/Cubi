"""教务后台导入批次接口；原始表格及初始密码没有下载入口。"""
import csv
from io import StringIO

from django.core.exceptions import ValidationError
from django.http import HttpResponse
from rest_framework.response import Response
from rest_framework.views import APIView

from tasks.import_service import (
    createImport, expireOfficeImports, getImport, optionalId, retryImport,
    validatePayload, visibleImports,
)
from tasks.models import OfficeImportJob
from users.permissions import PermRequired


def serializeImport(job):
    return {
        'id': job.pk, 'type': job.import_type, 'status': job.status,
        'originalName': job.original_name, 'createdBy': job.created_by_id,
        'adminClassId': job.admin_class_id, 'courseCollegeId': job.course_college_id,
        'total': job.total, 'completed': job.completed,
        'progress': job.completed * 100 // job.total if job.total else 0,
        'successCount': job.success_count, 'errorCount': job.error_count,
        'error': job.error, 'attempt': job.attempt,
        'retryable': (job.status in (OfficeImportJob.STATUS_FAILED, OfficeImportJob.STATUS_PARTIAL)
                      and not job.source_name),
        'createdAt': job.created_at, 'startedAt': job.started_at,
        'finishedAt': job.finished_at,
    }


def pageNumber(request):
    value = request.query_params.get('page', '1')
    if not value.isascii() or not value.isdecimal() or len(value) > 6 or int(value) < 1:
        raise ValidationError('page 必须为 1～999999 的正整数')
    return int(value)


class ImportAPIView(APIView):
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    def handle_exception(self, exc):
        if isinstance(exc, ValidationError):
            return Response({'error': '；'.join(exc.messages)}, status=400)
        return super().handle_exception(exc)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        return response


class OfficeImportListView(ImportAPIView):
    def get(self, request):
        page = pageNumber(request)
        queryset = visibleImports(request.user)
        expireOfficeImports(queryset)
        for key, allowed, field in (
                ('type', dict(OfficeImportJob.TYPE_CHOICES), 'import_type'),
                ('status', dict(OfficeImportJob.STATUS_CHOICES), 'status')):
            value = request.query_params.get(key)
            if value is not None:
                if value not in allowed:
                    raise ValidationError(f'{key} 无效')
                queryset = queryset.filter(**{field: value})
        offset = (page - 1) * 20
        return Response({'count': queryset.count(), 'page': page,
                         'results': [serializeImport(job) for job in queryset[offset:offset + 20]]})

    def post(self, request):
        payload = validatePayload(request.data, {'type', 'file', 'adminClassId', 'courseCollegeId'})
        importType = payload.get('type')
        if not isinstance(importType, str):
            raise ValidationError('type 必须为导入类型文本')
        job = createImport(
            request.user, importType, request.FILES.get('file'),
            adminClassId=optionalId(payload, 'adminClassId'),
            courseCollegeId=optionalId(payload, 'courseCollegeId'))
        return Response(serializeImport(job), status=202)


class OfficeImportDetailView(ImportAPIView):
    def get(self, request, jobId):
        return Response(serializeImport(getImport(request.user, jobId)))


class OfficeImportRetryView(ImportAPIView):
    def post(self, request, jobId):
        validatePayload(request.data, {'file'})
        job = retryImport(request.user, jobId, request.FILES.get('file'))
        return Response(serializeImport(job), status=202)


class OfficeImportErrorsView(ImportAPIView):
    def get(self, request, jobId):
        job = getImport(request.user, jobId)
        page = pageNumber(request)
        offset = (page - 1) * 100
        return Response({'count': job.error_count, 'page': page,
                         'results': job.errors[offset:offset + 100]})


def csvCell(value):
    text = str(value)
    if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text


class OfficeImportErrorDownloadView(ImportAPIView):
    def get(self, request, jobId):
        job = getImport(request.user, jobId)
        output = StringIO(newline='')
        output.write('\ufeff')
        writer = csv.writer(output)
        writer.writerow(['Excel 行号', '错误原因'])
        for error in job.errors:
            writer.writerow([csvCell(error['line']), csvCell(error['reason'])])
        response = HttpResponse(output.getvalue(), content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="import-{job.pk}-errors.csv"'
        response['X-Content-Type-Options'] = 'nosniff'
        return response
