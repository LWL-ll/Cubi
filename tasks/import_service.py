"""教务导入的私有源文件、权限、入队及超时回收。"""
import logging
from collections.abc import Mapping
from datetime import timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
import uuid

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.db.models import F
from django.http import Http404
from django.utils import timezone
from django_q.tasks import async_task

from academics.models import AdminClass, CourseCollege
from academics.views import MAX_XLSX_BYTES, parseXlsxRows
from tasks.models import OfficeImportJob
from users.models import User
from users.permissions import hasPerm

logger = logging.getLogger('codemind.office_import')
ACTIVE_STATUSES = (OfficeImportJob.STATUS_PENDING, OfficeImportJob.STATUS_RUNNING)
MAX_ACTIVE_IMPORTS = 3
PENDING_LIFETIME = timedelta(hours=24)
IMPORT_TASK = 'tasks.tasks_import.runOfficeImport'


def importStorage():
    return FileSystemStorage(location=Path(settings.PRIVATE_MATERIAL_ROOT) / 'office_imports')


def executionLifetime():
    # Q2 杀死超时 worker 后再开放重试，避免旧进程与新轮次并行导入。
    return timedelta(seconds=max(120, settings.Q_CLUSTER.get('timeout', 600)) + 60)


def requireImportPermission(actor, importType, adminClassId=None, courseCollegeId=None):
    if (actor is None or not actor.is_active or not actor.is_authenticated
            or not hasPerm(actor, 'class:manage')):
        raise PermissionDenied('没有权限执行该导入')
    if importType not in dict(OfficeImportJob.TYPE_CHOICES):
        raise ValidationError('未知导入类型')
    if importType == 'course' and not hasPerm(actor, 'course:manage'):
        raise PermissionDenied('导入课程需要 course:manage 权限')
    if importType == 'course_teacher' and not hasPerm(actor, 'class:assign_teacher'):
        raise PermissionDenied('导入课程教师需要 class:assign_teacher 权限')
    adminClass, cc = None, None
    if importType == 'class_student':
        adminClass = AdminClass.objects.select_related('major__department').filter(
            pk=adminClassId).first()
        if adminClass is None:
            raise ValidationError('目标行政班不存在')
        if (not adminClass.is_active or not adminClass.major.is_active
                or not adminClass.major.department.is_active):
            raise ValidationError('目标行政班、学院或专业已停用')
    elif adminClassId is not None:
        raise ValidationError('此导入类型不接受 adminClassId')
    if importType == 'course_teacher':
        cc = CourseCollege.objects.select_related('department', 'course').filter(pk=courseCollegeId).first()
        if cc is None:
            raise ValidationError('目标课程学院不存在')
        if not cc.department.is_active or not cc.course.is_active:
            raise ValidationError('目标课程或学院已停用')
    elif courseCollegeId is not None:
        raise ValidationError('此导入类型不接受 courseCollegeId')
    return adminClass, cc


def validateUpload(uploaded):
    if uploaded is None:
        raise ValidationError('请上传 xlsx 文件（字段名 file）')
    name = PurePosixPath(uploaded.name.replace('\\', '/')).name
    if (not name or len(name) > 255 or '\x00' in name
            or PurePosixPath(name).suffix.lower() != '.xlsx'):
        raise ValidationError('文件名须为不超过 255 字的 xlsx 文件名')
    try:
        name.encode('utf-8')
    except UnicodeEncodeError:
        raise ValidationError('文件名含无效字符') from None
    if getattr(uploaded, 'size', 0) > MAX_XLSX_BYTES:
        raise ValidationError('xlsx 文件不能超过 10MB')
    data = uploaded.read(MAX_XLSX_BYTES + 1)
    rows = parseXlsxRows(BytesIO(data))
    total = sum(bool(any(row)) for row in rows)
    if not total:
        raise ValidationError('xlsx 中没有可导入的数据行')
    return name, data, total


def deleteSource(name):
    if not name:
        return True
    try:
        importStorage().delete(name)
        return True
    except OSError:
        logger.warning('教务导入源文件清理失败，将在下一次回收时重试')
        return False


def cleanupImportSource(jobId, name):
    if deleteSource(name):
        OfficeImportJob.objects.filter(pk=jobId, source_name=name).exclude(
            status__in=ACTIVE_STATUSES).update(source_name='')


def storeSource(data):
    name = uuid.uuid4().hex + '.xlsx'
    try:
        return importStorage().save(name, ContentFile(data))
    except OSError:
        deleteSource(name)
        raise ValidationError('导入文件保存失败，请稍后重试') from None


def auditImport(actor, action, job):
    from academics.audit_service import recordAudit
    recordAudit(actor, action, job, after={
        'type': job.import_type, 'status': job.status, 'attempt': job.attempt,
        'total': job.total, 'successCount': job.success_count, 'errorCount': job.error_count,
    })


def expireOfficeImports(queryset=None):
    """轮询和定时任务共用；终态待删除文件也重试清理。"""
    queryset = queryset if queryset is not None else OfficeImportJob.objects.all()
    now = timezone.now()
    expired = queryset.filter(status__in=ACTIVE_STATUSES, expires_at__lte=now)
    count = 0
    for job in expired.only('id', 'attempt', 'source_name'):
        with transaction.atomic():
            changed = OfficeImportJob.objects.filter(
                pk=job.pk, attempt=job.attempt, status__in=ACTIVE_STATUSES,
                expires_at__lte=now).update(
                    status=OfficeImportJob.STATUS_FAILED, finished_at=now,
                    error='导入排队或执行超时，请重新上传文件重试')
            if changed:
                count += 1
                transaction.on_commit(
                    lambda jobId=job.pk, name=job.source_name: cleanupImportSource(jobId, name))
    for job in queryset.exclude(status__in=ACTIVE_STATUSES).exclude(source_name='').only(
            'id', 'source_name'):
        cleanupImportSource(job.pk, job.source_name)
    return count


def cleanupOrphanSources():
    """进程在建批次前退出时，清理超过一天且没有任何记录引用的随机源文件。"""
    storage = importStorage()
    try:
        _, names = storage.listdir('')
    except OSError:
        return
    cutoff = timezone.now() - PENDING_LIFETIME
    for name in names:
        if not re.fullmatch(r'[0-9a-f]{32}\.xlsx', name):
            continue
        try:
            if storage.get_modified_time(name) > cutoff:
                continue
        except OSError:
            continue
        if not OfficeImportJob.objects.filter(source_name=name).exists():
            deleteSource(name)


def dispatchImport(jobId, attempt):
    try:
        # 默认集群负责导入，long 集群仅判题；队列参数不含表格或初始密码。
        async_task(IMPORT_TASK, jobId, attempt, cluster=settings.Q_CLUSTER['name'])
    except Exception:
        job = OfficeImportJob.objects.filter(
            pk=jobId, attempt=attempt, status=OfficeImportJob.STATUS_PENDING).first()
        if job is not None:
            changed = OfficeImportJob.objects.filter(
                pk=jobId, attempt=attempt, status=OfficeImportJob.STATUS_PENDING).update(
                    status=OfficeImportJob.STATUS_FAILED, finished_at=timezone.now(),
                    error='导入任务入队失败，请重新上传文件重试')
            if changed:
                cleanupImportSource(jobId, job.source_name)
        logger.warning('教务导入任务入队失败：批次 %s，轮次 %s', jobId, attempt)


def createImport(actor, importType, uploaded, *, adminClassId=None, courseCollegeId=None):
    adminClass, cc = requireImportPermission(actor, importType, adminClassId, courseCollegeId)
    name, data, total = validateUpload(uploaded)
    source = storeSource(data)
    try:
        with transaction.atomic():
            # 同一提交人串行计数，避免并发请求越过活动批次上限。
            User.objects.filter(pk=actor.pk).update(id=F('id'))
            actor = User.objects.get(pk=actor.pk)
            requireImportPermission(actor, importType, adminClassId, courseCollegeId)
            if OfficeImportJob.objects.filter(
                    created_by=actor, status__in=ACTIVE_STATUSES).count() >= MAX_ACTIVE_IMPORTS:
                raise ValidationError('最多同时存在 3 个待处理导入批次')
            job = OfficeImportJob.objects.create(
                created_by=actor, import_type=importType, original_name=name,
                source_name=source, total=total, admin_class=adminClass, course_college=cc,
                expires_at=timezone.now() + PENDING_LIFETIME)
            auditImport(actor, 'import.create', job)
            transaction.on_commit(lambda: dispatchImport(job.pk, job.attempt))
        job.refresh_from_db()
        return job
    except Exception:
        deleteSource(source)
        raise


def visibleImports(actor):
    queryset = OfficeImportJob.objects.all()
    if not actor.is_superuser:
        queryset = queryset.filter(created_by=actor)
    return queryset


def getImport(actor, jobId):
    queryset = visibleImports(actor).filter(pk=jobId)
    if not queryset.exists():
        raise Http404('导入批次不存在')
    expireOfficeImports(queryset)
    return queryset.get()


def retryImport(actor, jobId, uploaded):
    job = getImport(actor, jobId)
    requireImportPermission(actor, job.import_type, job.admin_class_id, job.course_college_id)
    if job.status in (OfficeImportJob.STATUS_FAILED, OfficeImportJob.STATUS_PARTIAL) and job.source_name:
        raise ValidationError('旧导入文件清理尚未完成，请稍后重试')
    name, data, total = validateUpload(uploaded)
    source = storeSource(data)
    try:
        with transaction.atomic():
            User.objects.filter(pk=actor.pk).update(id=F('id'))
            OfficeImportJob.objects.filter(pk=job.pk).update(id=F('id'))
            job = visibleImports(actor).get(pk=job.pk)
            # 以原提交人的权限执行；管理员不能借重试改变任务归属。
            owner = User.objects.filter(pk=job.created_by_id).first()
            requireImportPermission(owner, job.import_type, job.admin_class_id, job.course_college_id)
            if job.status not in (OfficeImportJob.STATUS_FAILED, OfficeImportJob.STATUS_PARTIAL):
                raise ValidationError('仅失败或部分成功的批次可以重新上传重试')
            if OfficeImportJob.objects.filter(
                    created_by=owner, status__in=ACTIVE_STATUSES).count() >= MAX_ACTIVE_IMPORTS:
                raise ValidationError('最多同时存在 3 个待处理导入批次')
            oldName = job.source_name
            job.original_name, job.source_name, job.total = name, source, total
            job.completed = job.success_count = job.error_count = job.row_cursor = 0
            job.errors, job.error = [], ''
            job.attempt += 1
            job.status = OfficeImportJob.STATUS_PENDING
            job.started_at = job.finished_at = None
            job.expires_at = timezone.now() + PENDING_LIFETIME
            job.save()
            auditImport(actor, 'import.retry', job)
            transaction.on_commit(lambda: deleteSource(oldName))
            transaction.on_commit(lambda: dispatchImport(job.pk, job.attempt))
        job.refresh_from_db()
        return job
    except Exception:
        deleteSource(source)
        raise


def validatePayload(payload, fields):
    if not isinstance(payload, Mapping) or set(payload) - fields:
        raise ValidationError('请求含不支持的字段')
    return payload


def optionalId(payload, key):
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    if type(value) is not int or not 1 <= value <= 2 ** 63 - 1:
        raise ValidationError(f'{key} 必须为有效正整数')
    return value
