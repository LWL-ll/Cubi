"""资料授权、私有文件生命周期与错误路径清理。"""
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from teaching.models import TeachingMaterial
from teaching.tests import ObjectPermissionTestBase


class TeachingMaterialTests(ObjectPermissionTestBase):
    def setUp(self):
        testRoot = Path(settings.BASE_DIR) / 'private_uploads'
        testRoot.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='test-', dir=testRoot)
        self.addCleanup(self.directory.cleanup)
        setting = override_settings(PRIVATE_MATERIAL_ROOT=Path(self.directory.name))
        setting.enable()
        self.addCleanup(setting.disable)
        self.client = APIClient()
        self.client.force_login(self.teacher)
        self.url = f'/api/teacher/teaching-classes/{self.myTeachingClass.pk}/materials/'

    def upload(self, name='讲义.pdf', content=b'%PDF-test', **extra):
        return self.client.post(self.url, {
            'title': '第一章讲义', 'description': '课堂资料',
            'file': SimpleUploadedFile(name, content), **extra}, format='multipart')

    def stored(self):
        return sorted(p.name for p in Path(self.directory.name).iterdir())

    def test_upload_metadata_hides_storage_path_and_student_download(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.data)
        material = TeachingMaterial.objects.get(pk=response.data['id'])
        self.assertEqual(material.uploaded_by, self.teacher)
        self.assertNotIn('storage_name', response.data)
        self.assertNotIn(material.storage_name, str(response.data))
        self.assertNotIn(self.directory.name, str(response.data))
        self.assertNotEqual(material.storage_name, material.original_name)
        self.client.force_login(self.student)
        rows = self.client.get(
            f'/api/teaching-classes/{self.myTeachingClass.pk}/materials/').json()
        self.assertEqual(rows['count'], 1)
        download = self.client.get(f'/api/materials/{material.pk}/download/')
        try:
            self.assertEqual(download.status_code, 200)
            self.assertEqual(b''.join(download.streaming_content), b'%PDF-test')
            self.assertIn('attachment', download['Content-Disposition'])
        finally:
            download.close()

    def test_teacher_download_is_protected(self):
        materialId = self.upload().data['id']
        response = self.client.get(f'/api/teacher/materials/{materialId}/download/')
        try:
            self.assertEqual(response.status_code, 200)
        finally:
            response.close()
        self.client.force_login(self.teacher2)
        with mock.patch('teaching.material_service.materialStorage') as storage:
            self.assertEqual(self.client.get(
                f'/api/teacher/materials/{materialId}/download/').status_code, 404)
            storage.assert_not_called()

    def test_cross_class_student_never_opens_file(self):
        materialId = self.upload().data['id']
        self.client.force_login(self.student2)
        with mock.patch('teaching.material_service.materialStorage') as storage:
            self.assertEqual(self.client.get(f'/api/materials/{materialId}/download/').status_code, 404)
            self.assertEqual(self.client.get(
                f'/api/teaching-classes/{self.myTeachingClass.pk}/materials/').status_code, 404)
            storage.assert_not_called()

    def test_departed_member_loses_access_immediately(self):
        materialId = self.upload().data['id']
        member = self.myProfile.memberships.get(left_at__isnull=True)
        member.left_at = timezone.now().date()
        member.save()
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(f'/api/materials/{materialId}/download/').status_code, 404)

    def test_student_cannot_manage_and_other_teacher_cannot_upload(self):
        materialId = self.upload().data['id']
        self.client.force_login(self.student)
        self.assertEqual(self.client.delete(f'/api/teacher/materials/{materialId}/').status_code, 403)
        self.assertEqual(self.client.patch(f'/api/teacher/materials/{materialId}/',
                                          {'title': '越权'}, format='json').status_code, 403)
        self.client.force_login(self.teacher2)
        self.assertEqual(self.upload().status_code, 404)
        self.assertEqual(len(self.stored()), 1)

    def test_same_class_co_teacher_can_manage(self):
        materialId = self.upload().data['id']
        self.myTeachingClass.teachers.add(self.teacher2)
        self.client.force_login(self.teacher2)
        self.assertEqual(self.client.patch(f'/api/teacher/materials/{materialId}/',
                                          {'title': '共同维护'}, format='json').status_code, 200)

    def test_bad_type_empty_large_and_forged_owner_leave_no_files(self):
        for name, content, extra in (
                ('bad.exe', b'data', {}), ('bad.html', b'html', {}),
                ('empty.pdf', b'', {}), ('ok.pdf', b'ok', {'uploadedById': self.teacher2.pk}),
                ('ok.pdf', b'ok', {'title': ''})):
            with self.subTest(name=name, extra=extra):
                self.assertEqual(self.upload(name, content, **extra).status_code, 400)
        with mock.patch('teaching.material_service.MAX_FILE_BYTES', 3):
            self.assertEqual(self.upload(content=b'four').status_code, 400)
        self.assertEqual(self.stored(), [])
        self.assertFalse(TeachingMaterial.objects.exists())

    def test_metadata_update_and_file_replace_cleanup(self):
        materialId = self.upload().data['id']
        material = TeachingMaterial.objects.get(pk=materialId)
        original = material.storage_name
        url = f'/api/teacher/materials/{materialId}/'
        self.assertEqual(self.client.patch(url, {'description': '修订说明'}, format='json').status_code, 200)
        self.assertEqual(self.stored(), [original])
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(url, {
                'file': SimpleUploadedFile('新版.txt', b'new'), 'title': '新版'}, format='multipart')
        self.assertEqual(response.status_code, 200)
        material.refresh_from_db()
        self.assertNotEqual(material.storage_name, original)
        self.assertEqual(self.stored(), [material.storage_name])
        self.assertEqual(material.original_name, '新版.txt')
        self.assertEqual(material.size, 3)

    def test_delete_cleans_file_and_revokes_download(self):
        materialId = self.upload().data['id']
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(f'/api/teacher/materials/{materialId}/').status_code, 204)
        self.assertEqual(self.stored(), [])
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(f'/api/materials/{materialId}/download/').status_code, 404)

    def test_creation_database_error_cleans_new_file(self):
        with mock.patch('teaching.material_service.TeachingMaterial.objects.create',
                        side_effect=RuntimeError('db unavailable')):
            with self.assertRaises(RuntimeError):
                self.upload()
        self.assertEqual(self.stored(), [])

    def test_replacement_database_error_keeps_old_file(self):
        materialId = self.upload().data['id']
        original = self.stored()
        with mock.patch('teaching.material_service.TeachingMaterial.save',
                        side_effect=RuntimeError('db unavailable')):
            with self.assertRaises(RuntimeError):
                self.client.patch(f'/api/teacher/materials/{materialId}/',
                    {'file': SimpleUploadedFile('new.txt', b'new')}, format='multipart')
        self.assertEqual(self.stored(), original)
        self.assertEqual(TeachingMaterial.objects.get(pk=materialId).storage_name, original[0])

    def test_missing_disk_file_is_404(self):
        materialId = self.upload().data['id']
        (Path(self.directory.name) / self.stored()[0]).unlink()
        self.assertEqual(self.client.get(
            f'/api/teacher/materials/{materialId}/download/').status_code, 404)

    def test_foreign_and_missing_material_have_identical_404(self):
        materialId = self.upload().data['id']
        self.client.force_login(self.student2)
        existing = self.client.get(f'/api/materials/{materialId}/download/')
        missing = self.client.get('/api/materials/999999/download/')
        self.assertEqual(existing.status_code, 404)
        self.assertEqual(existing.json(), missing.json())

    def test_fake_file_field_is_rejected(self):
        materialId = self.upload().data['id']
        response = self.client.patch(f'/api/teacher/materials/{materialId}/',
                                     {'file': 'not-an-upload'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.stored()), 1)

    def test_partial_storage_failure_is_cleaned(self):
        from teaching.material_service import materialStorage
        storage = materialStorage()

        def partial(name, uploaded):
            storage.path(name)
            (Path(self.directory.name) / name).write_bytes(b'partial')
            raise OSError('disk unavailable')

        with mock.patch('teaching.material_service.materialStorage', return_value=storage), \
                mock.patch.object(storage, 'save', side_effect=partial):
            self.assertEqual(self.upload().status_code, 400)
        self.assertEqual(self.stored(), [])
        self.assertFalse(TeachingMaterial.objects.exists())
