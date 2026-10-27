"""后台导入的授权、逐行原子性、队列防重和私有文件生命周期。"""
from datetime import timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from academics.models import Course
from academics.tests import OfficeApiTestBase, makeXlsx
from tasks import import_service, tasks_import
from tasks.import_api import (
    OfficeImportDetailView, OfficeImportErrorDownloadView, OfficeImportErrorsView,
    OfficeImportListView, OfficeImportRetryView,
)
from tasks.models import OfficeImportJob
from users.models import User


class OfficeImportJobTests(OfficeApiTestBase):
    def setUp(self):
        super().setUp()
        base = Path(settings.BASE_DIR) / 'private_uploads'
        base.mkdir(exist_ok=True)
        temporary = TemporaryDirectory(prefix='import-tests-', dir=base)
        self.addCleanup(temporary.cleanup)
        context = override_settings(PRIVATE_MATERIAL_ROOT=temporary.name)
        context.enable()
        self.addCleanup(context.disable)

    def upload(self, rows=None):
        return makeXlsx(['编号', '姓名', '初始密码'], rows or [['IMP101', '导入教师', 'OriginalPw123']])

    def create(self, importType='teacher', rows=None, **kwargs):
        return import_service.createImport(self.adminUser, importType, self.upload(rows), **kwargs)

    def execute(self, job):
        with self.captureOnCommitCallbacks(execute=True):
            tasks_import.runOfficeImport(job.pk, job.attempt)
        job.refresh_from_db()
        return job

    def test_submit_returns_202_and_enqueues_only_ids_after_commit(self):
        with mock.patch.object(import_service, 'async_task') as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                response = self._call(OfficeImportListView, self.adminUser, 'post', {
                    'type': 'teacher', 'file': self.upload()}, format='multipart')
                enqueue.assert_not_called()
            self.assertEqual(response.status_code, 202)
            job = OfficeImportJob.objects.get()
            enqueue.assert_called_once_with(
                import_service.IMPORT_TASK, job.pk, 1, cluster=settings.Q_CLUSTER['name'])
        self.assertEqual(job.total, 1)
        self.assertTrue(import_service.importStorage().exists(job.source_name))
        self.assertNotIn('source_name', response.data)
        self.assertNotIn('OriginalPw123', str(response.data))
        self.assertNotIn('OriginalPw123', json.dumps(job.errors))
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_submit_rejects_teacher_student_and_bare_user(self):
        for user in (self.teacher, self.student, self.bareUser):
            response = self._call(OfficeImportListView, user, 'post', {
                'type': 'teacher', 'file': self.upload()}, format='multipart')
            self.assertEqual(response.status_code, 403)
        self.assertFalse(OfficeImportJob.objects.exists())

    def test_course_and_course_teacher_require_their_extra_permission(self):
        self._grant(self.bareUser, 'class_manage')
        for importType, target in (('course', {}), ('course_teacher', {'courseCollegeId': self.cc.pk})):
            response = self._call(OfficeImportListView, self.bareUser, 'post', {
                'type': importType, 'file': self.upload(), **target}, format='multipart')
            self.assertEqual(response.status_code, 403)
        self.assertFalse(OfficeImportJob.objects.exists())

    def test_invalid_payload_or_targets_create_no_job_or_source(self):
        cases = [
            {'type': 'other'}, {'type': 'teacher', 'password': 'forbidden'},
            {'type': 'teacher', 'adminClassId': self.adminClass.pk},
            {'type': 'class_student', 'adminClassId': 'bad'},
            {'type': 'class_student', 'adminClassId': 999999},
            {'type': 'course_teacher', 'courseCollegeId': 999999},
            {'type': 'teacher', 'courseCollegeId': self.cc.pk},
        ]
        for payload in cases:
            response = self._call(OfficeImportListView, self.adminUser, 'post', {
                **payload, 'file': self.upload()}, format='multipart')
            self.assertEqual(response.status_code, 400, payload)
        response = self._call(OfficeImportListView, self.adminUser, 'post', [], format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(OfficeImportJob.objects.exists())
        self.assertEqual(list(Path(settings.PRIVATE_MATERIAL_ROOT).rglob('*.xlsx')), [])

    def test_missing_bad_empty_or_extra_column_file_is_rejected(self):
        files = [None, SimpleUploadedFile('bad.xlsx', b'bad'),
                 SimpleUploadedFile('bad.txt', b'bad'),
                 self.upload([['', '', '']]), self.upload([['x', 'y', 'pw', 'extra']])]
        for uploaded in files:
            data = {'type': 'teacher'}
            if uploaded is not None:
                data['file'] = uploaded
            response = self._call(OfficeImportListView, self.adminUser, 'post', data, format='multipart')
            self.assertEqual(response.status_code, 400)
        self.assertFalse(OfficeImportJob.objects.exists())

    def test_inactive_department_or_major_blocks_fixed_class_import(self):
        self.major.is_active = False
        self.major.save()
        with self.assertRaises(ValidationError):
            self.create('class_student', adminClassId=self.adminClass.pk)
        self.major.is_active = True
        self.major.save()
        self.department.is_active = False
        self.department.save()
        with self.assertRaises(ValidationError):
            self.create('course_teacher', courseCollegeId=self.cc.pk)

    def test_three_pending_jobs_limit_also_cleans_rejected_upload(self):
        for _ in range(3):
            self.create()
        with self.assertRaises(ValidationError):
            self.create()
        self.assertEqual(OfficeImportJob.objects.count(), 3)
        self.assertEqual(len(list(Path(settings.PRIVATE_MATERIAL_ROOT).rglob('*.xlsx'))), 3)

    def test_only_owner_or_superuser_can_query_errors_or_retry(self):
        job = self.create()
        other = User.objects.create_user(username='OTHER-IMPORT')
        other.groups.add(Group.objects.get(name='教务'))
        for view in (OfficeImportDetailView, OfficeImportErrorsView, OfficeImportErrorDownloadView):
            self.assertEqual(self._call(view, other, jobId=job.pk).status_code, 404)
        response = self._call(OfficeImportRetryView, other, 'post', {
            'file': self.upload()}, format='multipart', jobId=job.pk)
        self.assertEqual(response.status_code, 404)
        response = self._call(OfficeImportListView, other)
        self.assertEqual(response.data['count'], 0)
        superuser = User.objects.create_superuser(username='IMPORT-ROOT', password='RootPw123')
        self.assertEqual(self._call(OfficeImportDetailView, superuser, jobId=job.pk).status_code, 200)
        self.assertEqual(self._call(OfficeImportListView, superuser).data['count'], 1)

    def test_full_success_creates_account_and_cleans_source(self):
        job = self.create()
        name = job.source_name
        self.execute(job)
        self.assertEqual(job.status, OfficeImportJob.STATUS_SUCCEEDED)
        self.assertEqual((job.total, job.completed, job.success_count, job.error_count), (1, 1, 1, 0))
        self.assertEqual(job.source_name, '')
        self.assertFalse(import_service.importStorage().exists(name))
        user = User.objects.get(username='IMP101')
        self.assertTrue(user.check_password('OriginalPw123'))
        self.assertTrue(user.must_change_password)
        self.assertTrue(user.identities.filter(teacher_profile__isnull=False).exists())

    def test_partial_rows_keep_physical_line_numbers_and_prior_success(self):
        job = self.create(rows=[[], ['IMP101', '甲', 'pw1'], [],
                               ['IMP102', '', 'PasswordMustNotLeak'], ['IMP103', '丙', 'pw3']])
        self.execute(job)
        self.assertEqual(job.status, OfficeImportJob.STATUS_PARTIAL)
        self.assertEqual((job.total, job.completed, job.success_count, job.error_count), (3, 3, 2, 1))
        self.assertEqual(job.errors[0]['line'], 5)
        self.assertNotIn('PasswordMustNotLeak', json.dumps(job.errors))
        self.assertTrue(User.objects.filter(username='IMP101').exists())
        self.assertTrue(User.objects.filter(username='IMP103').exists())
        self.assertFalse(User.objects.filter(username='IMP102').exists())

    def test_all_bad_rows_result_in_failed_completed_batch(self):
        job = self.create(rows=[['IMP101', '', 'private']])
        self.execute(job)
        self.assertEqual(job.status, OfficeImportJob.STATUS_FAILED)
        self.assertEqual((job.completed, job.error_count), (1, 1))
        self.assertEqual(job.error, '')
        self.assertEqual(job.source_name, '')

    def test_duplicate_queue_message_does_not_import_or_audit_twice(self):
        from academics.models import AuditEvent
        job = self.create()
        self.execute(job)
        before = AuditEvent.objects.count()
        self.execute(job)
        self.assertEqual(User.objects.filter(username='IMP101').count(), 1)
        self.assertEqual(AuditEvent.objects.count(), before)
        self.assertEqual(job.success_count, 1)

    def test_running_job_is_not_claimed_by_another_delivery(self):
        job = self.create()
        OfficeImportJob.objects.filter(pk=job.pk).update(status=OfficeImportJob.STATUS_RUNNING)
        self.execute(job)
        self.assertFalse(User.objects.filter(username='IMP101').exists())
        self.assertEqual(job.status, OfficeImportJob.STATUS_RUNNING)

    def test_large_job_continues_from_committed_cursor_without_reimporting_rows(self):
        job = self.create(rows=[['IMP101', '甲', 'pw1'], ['IMP102', '乙', 'pw2']])
        with mock.patch.object(tasks_import, 'MAX_ROWS_PER_TASK', 1):
            with mock.patch.object(import_service, 'async_task') as enqueue:
                self.execute(job)
                self.assertEqual(job.status, OfficeImportJob.STATUS_PENDING)
                self.assertEqual((job.completed, job.row_cursor), (1, 1))
                self.assertTrue(import_service.importStorage().exists(job.source_name))
                enqueue.assert_called_once_with(
                    import_service.IMPORT_TASK, job.pk, job.attempt, cluster=settings.Q_CLUSTER['name'])
            firstStarted = job.started_at
            self.execute(job)
        self.assertEqual(job.status, OfficeImportJob.STATUS_SUCCEEDED)
        self.assertEqual((job.completed, job.success_count, job.error_count), (2, 2, 0))
        self.assertEqual(job.started_at, firstStarted)
        self.assertEqual(job.source_name, '')
        self.assertTrue(User.objects.get(username='IMP101').check_password('pw1'))
        self.assertTrue(User.objects.get(username='IMP102').check_password('pw2'))

    def test_continuation_queue_failure_keeps_committed_progress_and_cleans_source(self):
        job = self.create(rows=[['IMP101', '甲', 'pw1'], ['IMP102', '乙', 'pw2']])
        with mock.patch.object(tasks_import, 'MAX_ROWS_PER_TASK', 1):
            with mock.patch.object(import_service, 'async_task', side_effect=RuntimeError('secret')):
                self.execute(job)
        self.assertEqual(job.status, OfficeImportJob.STATUS_FAILED)
        self.assertEqual((job.completed, job.success_count), (1, 1))
        self.assertEqual(job.source_name, '')
        self.assertTrue(User.objects.filter(username='IMP101').exists())
        self.assertFalse(User.objects.filter(username='IMP102').exists())

    def test_fixed_class_and_course_teacher_use_existing_identity_and_assignment_rules(self):
        studentJob = self.create('class_student', rows=[['NEW-STUDENT', '学生', 'StudPw123']],
                                 adminClassId=self.adminClass.pk)
        self.execute(studentJob)
        profile = User.objects.get(username='NEW-STUDENT').identities.get().student_profile
        self.assertTrue(profile.memberships.filter(admin_class=self.adminClass, left_at__isnull=True).exists())
        teacherJob = self.create('course_teacher', courseCollegeId=self.cc.pk)
        self.execute(teacherJob)
        imported = User.objects.get(username='IMP101')
        self.assertTrue(self.cc.teachers.filter(pk=imported.pk).exists())
        self.assertTrue(self.teachingClass.teachers.filter(pk=imported.pk).exists())

    def test_course_and_legacy_student_auto_import_handlers(self):
        job = self.create('course', rows=[['IMP-COURSE', '新课程']])
        self.execute(job)
        self.assertEqual(job.status, OfficeImportJob.STATUS_SUCCEEDED)
        self.assertTrue(Course.objects.filter(code='IMP-COURSE', name='新课程').exists())
        job = self.create('student', rows=[['20231001', '自动学生', 'StudPw123']])
        self.execute(job)
        self.assertEqual(job.status, OfficeImportJob.STATUS_SUCCEEDED)
        self.assertTrue(User.objects.get(username='20231001').identities.get().student_profile.memberships.exists())

    def test_permission_revoked_before_execution_stops_every_row_and_cleans_file(self):
        job = self.create()
        self.adminUser.groups.clear()
        self.execute(job)
        self.assertEqual(job.status, OfficeImportJob.STATUS_FAILED)
        self.assertEqual(job.completed, 0)
        self.assertEqual(job.source_name, '')
        self.assertFalse(User.objects.filter(username='IMP101').exists())

    def test_disabled_submitter_or_deleted_target_does_not_execute(self):
        job = self.create('class_student', adminClassId=self.adminClass.pk)
        OfficeImportJob.objects.filter(pk=job.pk).update(admin_class=None)
        self.execute(job)
        self.assertEqual(job.status, OfficeImportJob.STATUS_FAILED)
        job = self.create()
        User.objects.filter(pk=self.adminUser.pk).update(is_active=False)
        self.execute(job)
        self.assertEqual(job.status, OfficeImportJob.STATUS_FAILED)
        self.assertFalse(User.objects.filter(username='IMP101').exists())

    def test_runtime_revocation_keeps_committed_row_and_stops_remaining_rows(self):
        job = self.create(rows=[['IMP101', '甲', 'pw1'], ['IMP102', '乙', 'pw2']])
        original = tasks_import.importOneRow

        def importAndRevoke(currentJob, actor, cells):
            result = original(currentJob, actor, cells)
            User.objects.filter(pk=actor.pk).update(is_active=False)
            return result

        with mock.patch.object(tasks_import, 'importOneRow', side_effect=importAndRevoke):
            self.execute(job)
        self.assertEqual(job.status, OfficeImportJob.STATUS_FAILED)
        self.assertEqual((job.completed, job.success_count), (1, 1))
        self.assertTrue(User.objects.filter(username='IMP101').exists())
        self.assertFalse(User.objects.filter(username='IMP102').exists())

    def test_exception_rolls_back_same_row_account_and_progress_without_disclosing_details(self):
        job = self.create(rows=[['IMP101', '甲', 'pw1'], ['IMP102', '乙', 'pw2']])
        original = tasks_import.importOneRow

        def crashOnSecond(currentJob, actor, cells):
            result = original(currentJob, actor, cells)
            if cells[0] == 'IMP102':
                raise RuntimeError('PrivatePasswordAndPath')
            return result

        with mock.patch.object(tasks_import, 'importOneRow', side_effect=crashOnSecond):
            self.execute(job)
        self.assertEqual(job.status, OfficeImportJob.STATUS_FAILED)
        self.assertEqual((job.completed, job.success_count, job.row_cursor), (1, 1, 1))
        self.assertTrue(User.objects.filter(username='IMP101').exists())
        self.assertFalse(User.objects.filter(username='IMP102').exists())
        self.assertNotIn('PrivatePasswordAndPath', job.error)

    def test_queue_failure_records_safe_failure_and_cleans_source(self):
        with mock.patch.object(import_service, 'async_task', side_effect=RuntimeError('PasswordAndPath')):
            with self.captureOnCommitCallbacks(execute=True):
                job = self.create()
        job.refresh_from_db()
        self.assertEqual(job.status, OfficeImportJob.STATUS_FAILED)
        self.assertIn('入队失败', job.error)
        self.assertNotIn('PasswordAndPath', job.error)
        self.assertEqual(job.source_name, '')

    def test_expired_pending_or_running_jobs_become_retryable_and_delete_source(self):
        jobs = [self.create(), self.create()]
        for job, status in zip(jobs, (OfficeImportJob.STATUS_PENDING, OfficeImportJob.STATUS_RUNNING)):
            OfficeImportJob.objects.filter(pk=job.pk).update(
                status=status, expires_at=timezone.now() - timedelta(seconds=1))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(tasks_import.recoverOfficeImports(), 2)
        for job in jobs:
            job.refresh_from_db()
            self.assertEqual(job.status, OfficeImportJob.STATUS_FAILED)
            self.assertEqual(job.source_name, '')
        self.assertEqual(tasks_import.recoverOfficeImports(), 0)

    def test_detail_polling_also_recovers_expired_job(self):
        job = self.create()
        OfficeImportJob.objects.filter(pk=job.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.captureOnCommitCallbacks(execute=True):
            response = self._call(OfficeImportDetailView, self.adminUser, jobId=job.pk)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['retryable'])
        self.assertEqual(response.data['status'], OfficeImportJob.STATUS_FAILED)

    def test_retry_requires_reupload_and_never_overwrites_existing_password_or_name(self):
        job = self.create(rows=[['IMP101', '原名', 'FirstPassword'], ['IMP102', '', 'bad']])
        self.execute(job)
        self.assertEqual(job.status, OfficeImportJob.STATUS_PARTIAL)
        response = self._call(OfficeImportRetryView, self.adminUser, 'post', {},
                              format='multipart', jobId=job.pk)
        self.assertEqual(response.status_code, 400)
        with mock.patch.object(import_service, 'async_task'):
            with self.captureOnCommitCallbacks(execute=True):
                response = self._call(OfficeImportRetryView, self.adminUser, 'post', {
                    'file': self.upload([['IMP101', '禁止覆盖', 'NewPassword'], ['IMP102', '新增', 'NewPw2']])},
                    format='multipart', jobId=job.pk)
        self.assertEqual(response.status_code, 202)
        job.refresh_from_db()
        self.assertEqual(job.attempt, 2)
        tasks_import.runOfficeImport(job.pk, 1)
        job.refresh_from_db()
        self.assertEqual(job.status, OfficeImportJob.STATUS_PENDING)
        self.execute(job)
        original = User.objects.get(username='IMP101')
        self.assertEqual(original.name, '原名')
        self.assertTrue(original.check_password('FirstPassword'))
        self.assertEqual(job.success_count, 1)
        self.assertEqual(job.error_count, 1)
        self.assertTrue(User.objects.filter(username='IMP102', name='新增').exists())

    def test_active_or_successful_job_cannot_be_retried(self):
        job = self.create()
        for status in (OfficeImportJob.STATUS_PENDING, OfficeImportJob.STATUS_RUNNING,
                       OfficeImportJob.STATUS_SUCCEEDED):
            OfficeImportJob.objects.filter(pk=job.pk).update(status=status)
            with self.assertRaises(ValidationError):
                import_service.retryImport(self.adminUser, job.pk, self.upload())
        self.assertEqual(len(list(Path(settings.PRIVATE_MATERIAL_ROOT).rglob('*.xlsx'))), 0)

    def test_error_endpoints_return_only_errors_and_csv_formulas_are_escaped(self):
        job = self.create()
        errors = [{'line': 2, 'reason': '=HYPERLINK("https://invalid")'},
                  {'line': 3, 'reason': ' +SUM(1,2)'}, {'line': 4, 'reason': '\tformula'}]
        OfficeImportJob.objects.filter(pk=job.pk).update(
            errors=errors, error_count=3, completed=3, status=OfficeImportJob.STATUS_FAILED)
        response = self._call(OfficeImportErrorsView, self.adminUser, jobId=job.pk)
        self.assertEqual(response.data['results'], errors)
        response = self._call(OfficeImportErrorDownloadView, self.adminUser, jobId=job.pk)
        content = response.content.decode('utf-8-sig')
        self.assertIn("'=HYPERLINK", content)
        self.assertIn("' +SUM", content)
        self.assertIn("'\tformula", content)
        self.assertNotIn('OriginalPw123', content)
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')

    def test_job_list_and_error_pages_validate_and_paginate(self):
        job = self.create()
        errors = [{'line': line, 'reason': '错误'} for line in range(2, 107)]
        OfficeImportJob.objects.filter(pk=job.pk).update(errors=errors, error_count=len(errors))
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()
        request = factory.get('/api/import-jobs/?type=teacher&status=pending')
        force_authenticate(request, user=self.adminUser)
        response = OfficeImportListView.as_view()(request)
        self.assertEqual(response.data['count'], 1)
        request = factory.get('/errors/?page=2')
        force_authenticate(request, user=self.adminUser)
        response = OfficeImportErrorsView.as_view()(request, jobId=job.pk)
        self.assertEqual(len(response.data['results']), 5)
        for suffix in ('?page=0', '?page=huge', '?type=bad', '?status=bad'):
            request = factory.get('/api/import-jobs/' + suffix)
            force_authenticate(request, user=self.adminUser)
            self.assertEqual(OfficeImportListView.as_view()(request).status_code, 400)

    def test_cleanup_failure_keeps_key_for_later_recovery(self):
        job = self.create()
        with mock.patch.object(import_service, 'deleteSource', return_value=False):
            self.execute(job)
        self.assertNotEqual(job.source_name, '')
        self.assertTrue(import_service.importStorage().exists(job.source_name))
        tasks_import.recoverOfficeImports()
        job.refresh_from_db()
        self.assertEqual(job.source_name, '')

    def test_retry_waits_for_old_private_source_cleanup_instead_of_losing_its_key(self):
        job = self.create(rows=[['IMP101', '', 'pw']])
        with mock.patch.object(import_service, 'deleteSource', return_value=False):
            self.execute(job)
            oldName = job.source_name
            with self.assertRaises(ValidationError):
                import_service.retryImport(self.adminUser, job.pk, self.upload())
        job.refresh_from_db()
        self.assertEqual(job.source_name, oldName)
        self.assertEqual(job.attempt, 1)
        self.assertEqual(len(list(Path(settings.PRIVATE_MATERIAL_ROOT).rglob('*.xlsx'))), 1)

    def test_recovery_cleans_old_orphan_sources_but_preserves_referenced_and_recent_files(self):
        job = self.create()
        orphan = import_service.storeSource(b'orphan')
        recent = import_service.storeSource(b'recent')
        oldTime = (timezone.now() - timedelta(days=2)).timestamp()
        storage = import_service.importStorage()
        os.utime(storage.path(orphan), (oldTime, oldTime))
        os.utime(storage.path(job.source_name), (oldTime, oldTime))
        tasks_import.recoverOfficeImports()
        self.assertFalse(storage.exists(orphan))
        self.assertTrue(storage.exists(recent))
        self.assertTrue(storage.exists(job.source_name))

    def test_missing_source_has_safe_failure_and_no_account(self):
        job = self.create()
        import_service.importStorage().delete(job.source_name)
        self.execute(job)
        self.assertEqual(job.status, OfficeImportJob.STATUS_FAILED)
        self.assertEqual(job.source_name, '')
        self.assertFalse(User.objects.filter(username='IMP101').exists())

    def test_audit_keeps_only_safe_batch_metadata(self):
        from academics.models import AuditEvent
        job = self.create()
        self.execute(job)
        events = AuditEvent.objects.filter(object_type='tasks.officeimportjob', object_id=str(job.pk))
        self.assertEqual(events.count(), 2)
        for event in events:
            value = json.dumps(event.after)
            self.assertNotIn('OriginalPw123', value)
            self.assertNotIn('office_imports', value)
            self.assertNotIn('xlsx', value)
