"""默认 Q2 集群执行教务导入；逐行事务与批次轮次共同防重。"""
import time

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from academics import views
from tasks.import_service import (
    PENDING_LIFETIME, auditImport, cleanupImportSource, cleanupOrphanSources, dispatchImport,
    executionLifetime, expireOfficeImports, importStorage, requireImportPermission,
)
from tasks.models import OfficeImportJob
from users.models import User

MAX_ROWS_PER_TASK = 100
MAX_CHUNK_SECONDS = 60


class ImportLeaseLost(Exception):
    pass


def importOneRow(job, actor, cells):
    adminClass, cc = requireImportPermission(
        actor, job.import_type, job.admin_class_id, job.course_college_id)
    if job.import_type == 'class_student':
        return views.importStudents([cells], adminClass)
    if job.import_type == 'course_teacher':
        return views.importTeachersToCourse([cells], cc, actor=actor)
    handler = {
        'course': views.importCourses, 'teacher': views.importTeachers,
        'student': views.importStudentsAuto,
    }[job.import_type]
    return handler([cells])


def finishImport(jobId, attempt, *, error=''):
    with transaction.atomic():
        if not OfficeImportJob.objects.filter(
                pk=jobId, attempt=attempt, status=OfficeImportJob.STATUS_RUNNING).update(id=F('id')):
            return
        job = OfficeImportJob.objects.get(pk=jobId)
        if error:
            job.status = OfficeImportJob.STATUS_FAILED
        elif not job.error_count:
            job.status = OfficeImportJob.STATUS_SUCCEEDED
        elif job.success_count:
            job.status = OfficeImportJob.STATUS_PARTIAL
        else:
            job.status = OfficeImportJob.STATUS_FAILED
        job.error, job.finished_at = error, timezone.now()
        job.save(update_fields=['status', 'error', 'finished_at'])
        actor = User.objects.filter(pk=job.created_by_id).first()
        auditImport(actor, 'import.finish', job)
        transaction.on_commit(lambda: cleanupImportSource(job.pk, job.source_name))


def continueImport(jobId, attempt):
    """大表分段执行，同批次游标和轮次保持不变。"""
    with transaction.atomic():
        updated = OfficeImportJob.objects.filter(
            pk=jobId, attempt=attempt, status=OfficeImportJob.STATUS_RUNNING).update(
                status=OfficeImportJob.STATUS_PENDING,
                expires_at=timezone.now() + PENDING_LIFETIME)
        if updated:
            transaction.on_commit(lambda: dispatchImport(jobId, attempt))


def runOfficeImport(jobId, attempt):
    now = timezone.now()
    claimed = OfficeImportJob.objects.filter(
        pk=jobId, attempt=attempt, status=OfficeImportJob.STATUS_PENDING,
        expires_at__gt=now).update(
            status=OfficeImportJob.STATUS_RUNNING, started_at=Coalesce('started_at', Value(now)),
            expires_at=now + executionLifetime())
    if not claimed:
        return
    try:
        job = OfficeImportJob.objects.get(pk=jobId)
        actor = User.objects.filter(pk=job.created_by_id).first()
        requireImportPermission(actor, job.import_type, job.admin_class_id, job.course_college_id)
        with importStorage().open(job.source_name, 'rb') as source:
            rows = views.parseXlsxRows(source)
        started = time.monotonic()
        processed = 0
        for offset, cells in enumerate(rows):
            with transaction.atomic():
                if not OfficeImportJob.objects.filter(
                        pk=jobId, attempt=attempt, status=OfficeImportJob.STATUS_RUNNING,
                        expires_at__gt=timezone.now()).update(id=F('id')):
                    raise ImportLeaseLost()
                job = OfficeImportJob.objects.get(pk=jobId)
                if offset < job.row_cursor:
                    continue
                if any(cells):
                    # 权限每行读取新账号，导入过程中撤权或停用立即停止后续行。
                    actor = User.objects.filter(pk=job.created_by_id).first()
                    successes, errors = importOneRow(job, actor, cells)
                    job.completed += 1
                    job.success_count += len(successes)
                    job.error_count += len(errors)
                    job.errors.extend({'line': offset + 2, 'reason': reason} for _, reason in errors)
                    processed += 1
                # 业务行写入和游标同一事务：进程中断不会丢进度或重做已提交行。
                job.row_cursor = offset + 1
                job.save(update_fields=[
                    'row_cursor', 'completed', 'success_count', 'error_count', 'errors'])
            if (offset + 1 < len(rows) and (processed >= MAX_ROWS_PER_TASK
                    or time.monotonic() - started >= MAX_CHUNK_SECONDS)):
                continueImport(jobId, attempt)
                return
        finishImport(jobId, attempt)
    except ImportLeaseLost:
        return
    except PermissionDenied:
        finishImport(jobId, attempt, error='提交人已停用或缺少导入权限，后续行未执行')
    except ValidationError:
        finishImport(jobId, attempt, error='导入文件或目标数据已失效，请核查后重新上传')
    except Exception:
        # 不把异常内容、文件路径、表格行或密码写入日志和公开错误。
        finishImport(jobId, attempt, error='导入执行失败，请核查文件后重新上传重试')


def recoverOfficeImports():
    recovered = expireOfficeImports()
    cleanupOrphanSources()
    return recovered
