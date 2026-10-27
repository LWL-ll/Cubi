"""教师共享资料选材下发、目标班可见范围与独立文件副本。"""
from pathlib import Path
import tempfile
from unittest import mock

from django.conf import settings
from django.test import override_settings
from rest_framework.test import APIClient

from academics.models import AcademicTerm, AuditEvent
from academics.services import leaveStudentToday
from academics.term_service import closeTerm
from course_team.models import CourseChapter, CourseMaterial
from course_team.storage import privateStorage, storeFile
from teaching.material_service import materialStorage
from teaching.models import TeachingMaterial
from teaching.tests import ObjectPermissionTestBase


class SharedPublicationTests(ObjectPermissionTestBase):
    def setUp(self):
        root = Path(settings.BASE_DIR) / 'private_uploads'
        root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='student-publish-', dir=root)
        self.addCleanup(self.directory.cleanup)
        override = override_settings(PRIVATE_MATERIAL_ROOT=Path(self.directory.name))
        override.enable()
        self.addCleanup(override.disable)
        self.client = APIClient()
        self.client.force_login(self.teacher)
        self.source = CourseMaterial.objects.create(course_college=self.myTeachingClass.course_college,
            title='共享讲义', description='课程组说明', visibility='course', original_name='讲义.txt',
            storage_name=storeFile('讲义.txt', b'original shared content'), size=len(b'original shared content'))
        self.url = f'/api/teacher/teaching-classes/{self.myTeachingClass.pk}/materials/from-course/'

    def publish(self, **extra):
        return self.client.post(self.url, {'materialId': self.source.pk, **extra}, format='json')

    def download(self, material):
        response = self.client.get(f'/api/materials/{material.pk}/download/')
        try:
            return response.status_code, b''.join(response.streaming_content)
        finally:
            response.close()

    def test_publish_creates_independent_student_material_and_safe_audit(self):
        response = self.publish()
        self.assertEqual(response.status_code, 201, response.data)
        material = TeachingMaterial.objects.get(pk=response.data['id'])
        self.assertEqual(material.uploaded_by, self.teacher)
        self.assertEqual(material.title, '共享讲义')
        self.assertNotEqual(material.storage_name, self.source.storage_name)
        self.assertNotIn('storage_name', response.data)
        event = AuditEvent.objects.get(action='material.publish_shared')
        self.assertEqual(event.after['sourceMaterialId'], self.source.pk)
        self.assertNotIn('original shared content', str(event.after))
        self.client.force_login(self.student)
        self.assertEqual(self.download(material), (200, b'original shared content'))
        self.assertEqual(self.client.get('/api/student/materials/').json()['count'], 1)

    def test_source_replace_or_delete_does_not_change_distributed_copy(self):
        material = TeachingMaterial.objects.get(pk=self.publish().data['id'])
        old = self.source.storage_name
        self.source.storage_name = storeFile('讲义.txt', b'revised source')
        self.source.size = len(b'revised source')
        self.source.save()
        privateStorage().delete(old)
        new = self.source.storage_name
        self.source.delete()
        privateStorage().delete(new)
        self.client.force_login(self.student)
        self.assertEqual(self.download(material), (200, b'original shared content'))

    def test_selected_visibility_is_checked_against_target_not_any_taught_class(self):
        self.source.visibility = 'classes'
        self.source.save(update_fields=['visibility'])
        self.source.visible_classes.add(self.myTeachingClass)
        self.otherTeachingClass.teachers.add(self.teacher)
        url = f'/api/teacher/teaching-classes/{self.otherTeachingClass.pk}/materials/from-course/'
        with mock.patch('teaching.shared_material_service.openFile') as opened:
            self.assertEqual(self.client.post(url, {'materialId': self.source.pk}, format='json').status_code, 404)
            opened.assert_not_called()
        self.assertEqual(self.publish().status_code, 201)

    def test_cross_teacher_target_denied_before_opening_source(self):
        self.client.force_login(self.teacher2)
        with mock.patch('teaching.shared_material_service.openFile') as opened:
            self.assertEqual(self.publish().status_code, 404)
            opened.assert_not_called()

    def test_other_course_and_missing_sources_are_same_404(self):
        from academics.models import Course, CourseCollege
        cc = CourseCollege.objects.create(course=Course.objects.create(code='OTHER-PUBLISH', name='其他课程'),
                                         department=self.myProfile.major.department)
        self.source.course_college = cc
        self.source.save(update_fields=['course_college'])
        with mock.patch('teaching.shared_material_service.openFile') as opened:
            foreign = self.publish()
            absent = self.client.post(self.url, {'materialId': 999999}, format='json')
            self.assertEqual(foreign.status_code, 404)
            self.assertEqual(foreign.data, absent.data)
            opened.assert_not_called()

    def test_chapter_selection_must_be_visible_same_course_and_include_material(self):
        chapter = CourseChapter.objects.create(course_college=self.myTeachingClass.course_college,
            title='第一章', position=1)
        self.assertEqual(self.publish(chapterId=chapter.pk).status_code, 404)
        chapter.materials.add(self.source)
        self.assertEqual(self.publish(chapterId=chapter.pk, title='课堂选读', description='指定内容').status_code, 201)
        chapter.is_visible = False
        chapter.save(update_fields=['is_visible'])
        self.assertEqual(self.publish(chapterId=chapter.pk).status_code, 404)

    def test_closed_term_cannot_publish_and_no_file_is_opened(self):
        term, _ = AcademicTerm.objects.get_or_create(code=self.myTeachingClass.term, defaults={'name': '学期'})
        closeTerm(self.superUser, term.pk)
        with mock.patch('teaching.shared_material_service.openFile') as opened:
            self.assertEqual(self.publish().status_code, 400)
            opened.assert_not_called()
        self.assertFalse(TeachingMaterial.objects.exists())

    def test_student_cannot_publish_or_browse_shared_library(self):
        self.client.force_login(self.student)
        self.assertEqual(self.publish().status_code, 403)
        self.assertEqual(self.client.get(f'/api/course-team/teacher/materials/{self.source.pk}/').status_code, 403)
        self.assertEqual(self.client.get(f'/api/course-team/materials/{self.source.pk}/').status_code, 403)

    def test_invalid_parameters_create_no_material(self):
        for payload in ([], {}, {'materialId': True}, {'materialId': self.source.pk, 'ownerId': self.student.pk},
                        {'materialId': self.source.pk, 'title': ''},
                        {'materialId': self.source.pk, 'chapterId': 'x'}):
            self.assertEqual(self.client.post(self.url, payload, format='json').status_code, 400)
        self.assertFalse(TeachingMaterial.objects.exists())
        self.assertFalse([item for item in Path(self.directory.name).iterdir() if item.is_file()])

    def test_database_or_audit_failure_cleans_new_copy(self):
        with mock.patch('teaching.shared_material_service.recordAudit', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.publish()
        self.assertFalse(TeachingMaterial.objects.exists())
        self.assertFalse([item for item in Path(self.directory.name).iterdir() if item.is_file()])
        self.assertTrue(privateStorage().exists(self.source.storage_name))

    def test_missing_or_inconsistent_source_file_fails_safely(self):
        self.source.size += 1
        self.source.save(update_fields=['size'])
        self.assertEqual(self.publish().status_code, 400)
        privateStorage().delete(self.source.storage_name)
        self.assertEqual(self.publish().status_code, 404)
        self.assertFalse(TeachingMaterial.objects.exists())

    def test_departed_student_loses_distributed_file_access(self):
        material = TeachingMaterial.objects.get(pk=self.publish().data['id'])
        leaveStudentToday(self.myProfile)
        self.client.force_login(self.student)
        with mock.patch('teaching.material_service.openMaterial') as opened:
            self.assertEqual(self.client.get(f'/api/materials/{material.pk}/download/').status_code, 404)
            opened.assert_not_called()

    def test_json_material_can_be_copied_as_private_attachment(self):
        self.source.original_name = 'data.json'
        self.source.save(update_fields=['original_name'])
        material = TeachingMaterial.objects.get(pk=self.publish().data['id'])
        self.assertEqual(material.original_name, 'data.json')
        self.assertTrue(materialStorage().exists(material.storage_name))
