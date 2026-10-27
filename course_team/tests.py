import io
import json
from pathlib import Path
import tempfile
from unittest import mock
import zipfile

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.deletion import ProtectedError
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from rest_framework.test import APIClient

from academics.models import AcademicTerm, AuditEvent, Course, CourseCollege, TeachingClass
from teaching.tests import ObjectPermissionTestBase

from .models import CourseChapter, CourseKnowledge, CourseMaterial
from .services import saveMaterial
from .storage import parseDocument


class CourseTeamBackendTests(ObjectPermissionTestBase):
    def setUp(self):
        root = Path(settings.PRIVATE_MATERIAL_ROOT)
        root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='course-team-test-', dir=root)
        self.addCleanup(self.directory.cleanup)
        setting = override_settings(PRIVATE_MATERIAL_ROOT=Path(self.directory.name))
        setting.enable()
        self.addCleanup(setting.disable)
        self.client = APIClient()
        self.client.force_login(self.courseTeam)
        self.cc = self.myTeachingClass.course_college
        self.cc.leader = self.courseTeam
        self.cc.save()
        self.foreign = CourseCollege.objects.create(
            course=Course.objects.create(code='RESOURCE2', name='别的课程'),
            department=self.cc.department)
        self.base = f'/api/course-team/course-colleges/{self.cc.pk}'
        self.teacherBase = f'/api/course-team/teacher/course-colleges/{self.cc.pk}'
        AcademicTerm.objects.filter(code=self.myTeachingClass.term).update(status='open')

    def knowledge(self, **extra):
        response = self.client.post(self.base + '/knowledge/', {
            'title': '变量', 'content': '变量用于保存数据', **extra}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def material(self, **extra):
        response = self.client.post(self.base + '/materials/', {
            'title': '课程讲义', 'file': SimpleUploadedFile('讲义.pdf', b'%PDF-test'),
            **extra}, format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def chapter(self, **extra):
        response = self.client.post(self.base + '/chapters/', {
            'title': '第一章', **extra}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def document(self, name='知识.md', content='知识正文'.encode(), **extra):
        response = self.client.post(self.base + '/documents/', {
            'title': '知识文档', 'file': SimpleUploadedFile(name, content), **extra}, format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def stored(self):
        return sorted(path.name for path in Path(self.directory.name).rglob('*') if path.is_file())

    def test_course_group_without_leader_cannot_access_other_course(self):
        response = self.client.get(f'/api/course-team/course-colleges/{self.foreign.pk}/knowledge/')
        self.assertEqual(response.status_code, 404)
        ids = [row['id'] for row in self.client.get('/api/course-team/course-colleges/').data['results']]
        self.assertEqual(ids, [self.cc.pk])

    def test_ordinary_teacher_and_student_cannot_manage_even_if_assigned(self):
        for user in (self.teacher, self.student):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.base + '/knowledge/').status_code, 403)
            self.assertEqual(self.client.post(self.base + '/chapters/', {
                'title': '越权'}, format='json').status_code, 403)

    def test_superuser_can_manage_any_course_group(self):
        self.client.force_login(self.superUser)
        response = self.client.post(f'/api/course-team/course-colleges/{self.foreign.pk}/knowledge/', {
            'title': '管理员创建', 'content': '正文'}, format='json')
        self.assertEqual(response.status_code, 201)

    def test_removed_leader_loses_access_even_with_group_permission(self):
        obj = self.knowledge()
        CourseCollege.objects.filter(pk=self.cc.pk).update(leader=None)
        self.assertEqual(self.client.get(f"/api/course-team/knowledge/{obj['id']}/").status_code, 404)

    def test_feature_permission_is_required_even_for_real_leader(self):
        group = self.courseTeam.groups.get(name='课程组')
        group.permissions.remove(*group.permissions.filter(codename='knowledge_manage'))
        self.assertEqual(self.client.get(self.base + '/knowledge/').status_code, 403)
        self.assertEqual(self.client.get(self.base + '/materials/').status_code, 200)

    def test_knowledge_crud_and_strict_fields(self):
        obj = self.knowledge()
        url = f"/api/course-team/knowledge/{obj['id']}/"
        response = self.client.patch(url, {'title': '变量与类型', 'isVisible': False}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['isVisible'])
        for payload in ({'courseCollegeId': self.foreign.pk}, {'title': 123},
                        {'content': '\x00'}, {'title': ''}, {'title': '\ud800'}):
            with self.subTest(payload=repr(payload)):
                self.assertEqual(self.client.generic('PATCH', url, json.dumps(payload).encode(),
                                                     content_type='application/json').status_code, 400)
        self.assertEqual(self.client.delete(url).status_code, 204)

    def test_cross_course_object_read_update_delete_rejected(self):
        foreign = CourseKnowledge.objects.create(course_college=self.foreign, title='秘密', content='正文')
        url = f'/api/course-team/knowledge/{foreign.pk}/'
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.patch(url, {'title': '篡改'}, format='json').status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
        self.assertTrue(CourseKnowledge.objects.filter(pk=foreign.pk).exists())

    def test_resource_audit_records_changes_without_knowledge_body(self):
        obj = self.knowledge(content='私有知识正文marker')
        before = AuditEvent.objects.filter(action='course_team.knowledge.create').get().after
        self.assertNotIn('marker', json.dumps(before, ensure_ascii=False))
        self.client.patch(f"/api/course-team/knowledge/{obj['id']}/", {
            'content': '更新后的正文'}, format='json')
        event = AuditEvent.objects.filter(action='course_team.knowledge.update').get()
        self.assertNotEqual(event.before['bodySha256'], event.after['bodySha256'])
        self.assertEqual(event.actor_id, self.courseTeam.pk)

    def test_json_import_validates_all_before_writing(self):
        rows = [{'title': '有效', 'content': '正文'}, {'title': '不完整'}]
        response = self.client.post(self.base + '/knowledge/import/', {
            'file': SimpleUploadedFile('知识.json', json.dumps(rows).encode())}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CourseKnowledge.objects.exists())
        rows[1]['content'] = '第二条'
        response = self.client.post(self.base + '/knowledge/import/', {
            'file': SimpleUploadedFile('知识.json', json.dumps(rows).encode())}, format='multipart')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['count'], 2)

    def test_import_invalid_encoding_shape_and_type(self):
        for name, content in [('a.json', b'\xff'), ('a.json', b'{}'),
                              ('a.json', b'[]'), ('a.md', b'hello')]:
            with self.subTest(name=name, content=content):
                self.assertEqual(self.client.post(self.base + '/knowledge/import/', {
                    'file': SimpleUploadedFile(name, content)}, format='multipart').status_code, 400)
        self.assertFalse(CourseKnowledge.objects.exists())

    def test_document_parse_and_file_hidden_from_response(self):
        obj = self.document()
        self.assertEqual(obj['status'], 'ready')
        self.assertTrue(obj['isVisible'])
        self.assertEqual(obj['content'], '知识正文')
        self.assertEqual(obj['parseError'], '')
        stored = CourseKnowledge.objects.get(pk=obj['id']).storage_name
        self.assertNotIn(stored, str(obj))
        self.assertNotIn(self.directory.name, str(obj))
        self.assertNotIn('storage_name', obj)

    def test_document_failure_is_queryable_and_replacement_recovers(self):
        obj = self.document(name='坏.json', content=b'{broken')
        self.assertEqual(obj['status'], 'failed')
        self.assertTrue(obj['parseError'])
        self.assertEqual(obj['content'], '')
        self.assertEqual(self.client.get(self.base + '/knowledge/?status=failed').data['count'], 1)
        url = f"/api/course-team/knowledge/{obj['id']}/"
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(url, {
                'file': SimpleUploadedFile('修复.txt', '修复后的正文'.encode())}, format='multipart')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'ready')
        self.assertEqual(len(self.stored()), 1)

    def test_reparse_is_explicit_and_not_allowed_for_manual_entry(self):
        obj = self.document()
        url = f"/api/course-team/knowledge/{obj['id']}/reparse/"
        self.assertEqual(self.client.post(url, {}, format='json').status_code, 200)
        self.assertEqual(self.client.post(url, {'unexpected': True}, format='json').status_code, 400)
        entry = self.knowledge()
        self.assertEqual(self.client.post(f"/api/course-team/knowledge/{entry['id']}/reparse/", {},
                                         format='json').status_code, 400)

    def test_document_content_cannot_be_forged_and_upload_limits(self):
        obj = self.document()
        self.assertEqual(self.client.patch(f"/api/course-team/knowledge/{obj['id']}/", {
            'content': '伪造正文'}, format='json').status_code, 400)
        for name, content in [('a.pdf', b'%PDF'), ('empty.txt', b'')]:
            self.assertEqual(self.client.post(self.base + '/documents/', {
                'title': '文档', 'file': SimpleUploadedFile(name, content)},
                format='multipart').status_code, 400)
        with mock.patch('course_team.storage.MAX_DOCUMENT_BYTES', 3):
            self.assertEqual(self.client.post(self.base + '/documents/', {
                'title': '过大', 'file': SimpleUploadedFile('a.txt', b'four')},
                format='multipart').status_code, 400)
        self.assertEqual(CourseKnowledge.objects.count(), 1)

    def test_supported_document_formats_and_docx_limits(self):
        for name, content, expected in [
                ('a.txt', b'\xef\xbb\xbfhello', 'hello'), ('a.md', b'# heading', '# heading'),
                ('a.csv', b'a,b\n1,2', 'a\tb'), ('a.json', b'{"a": 1}', '"a": 1')]:
            with self.subTest(name=name):
                text, error = parseDocument(name, content)
                self.assertFalse(error)
                self.assertIn(expected, text)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>DOCX正文</w:t></w:r></w:p></w:body></w:document>')
        text, error = parseDocument('a.docx', output.getvalue())
        self.assertEqual((text, error), ('DOCX正文', ''))
        with mock.patch('course_team.storage.MAX_DOCX_UNPACKED', 2):
            self.assertTrue(parseDocument('a.docx', output.getvalue())[1])

    def test_docx_entities_are_rejected_and_never_expanded(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('word/document.xml', '<!DOCTYPE test [<!ENTITY x "secret">]><test>&x;</test>')
        self.assertTrue(parseDocument('a.docx', output.getvalue())[1])

    def test_unauthorized_download_does_not_open_private_storage(self):
        obj = self.document()
        CourseCollege.objects.filter(pk=self.cc.pk).update(leader=None)
        with mock.patch('course_team.storage.privateStorage') as storage:
            self.assertEqual(self.client.get(
                f"/api/course-team/knowledge/{obj['id']}/download/").status_code, 404)
            storage.assert_not_called()

    def test_document_download_and_missing_file_return_safe_error(self):
        obj = self.document()
        url = f"/api/course-team/knowledge/{obj['id']}/download/"
        response = self.client.get(url)
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content).decode(), '知识正文')
        finally:
            response.close()
        self.client.force_login(self.teacher)
        response = self.client.get(f"/api/course-team/teacher/knowledge/{obj['id']}/download/")
        response.close()
        self.assertEqual(response.status_code, 200)
        self.client.force_login(self.courseTeam)
        filePath = next(path for path in Path(self.directory.name).rglob('*') if path.is_file())
        filePath.unlink()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_material_upload_metadata_replacement_and_delete_cleanup(self):
        obj = self.material()
        url = f"/api/course-team/materials/{obj['id']}/"
        model = CourseMaterial.objects.get(pk=obj['id'])
        self.assertNotIn(model.storage_name, str(obj))
        self.assertNotEqual(model.storage_name, model.original_name)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(url, {
                'file': SimpleUploadedFile('新讲义.txt', b'new')}, format='multipart')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.stored()), 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertEqual(self.stored(), [])

    def test_stale_course_visibility_instance_cannot_reopen_selected_material(self):
        obj = self.material()
        stale = CourseMaterial.objects.get(pk=obj['id'])
        saveMaterial(self.courseTeam, self.cc, {
            'visibility': 'classes', 'class_ids': [self.myTeachingClass.pk]}, obj=stale)
        saved = saveMaterial(self.courseTeam, self.cc, {'title': '仅改标题'}, obj=stale)
        self.assertEqual(saved.title, '仅改标题')
        self.assertEqual(saved.visibility, 'classes')
        self.assertEqual(list(saved.visible_classes.values_list('pk', flat=True)),
                         [self.myTeachingClass.pk])

    def test_stale_selected_visibility_instance_can_edit_current_course_material(self):
        obj = self.material(visibility='classes', teachingClassIds=[self.myTeachingClass.pk])
        stale = CourseMaterial.objects.get(pk=obj['id'])
        saveMaterial(self.courseTeam, self.cc, {'visibility': 'course'}, obj=stale)
        saved = saveMaterial(self.courseTeam, self.cc, {'description': '仅改描述'}, obj=stale)
        self.assertEqual(saved.description, '仅改描述')
        self.assertEqual(saved.visibility, 'course')
        self.assertFalse(saved.visible_classes.exists())

    def test_failed_material_database_write_cleans_new_file(self):
        with mock.patch('course_team.services.recordAudit', side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):
                self.client.post(self.base + '/materials/', {
                    'title': '文件', 'file': SimpleUploadedFile('a.txt', b'ok')}, format='multipart')
        self.assertEqual(self.stored(), [])
        self.assertFalse(CourseMaterial.objects.exists())

    def test_material_rejects_empty_oversize_unknown_fields_and_executable(self):
        for name, content, extra in [('bad.exe', b'exe', {}), ('a.txt', b'', {}),
                                      ('a.txt', b'text', {'courseCollegeId': self.foreign.pk})]:
            self.assertEqual(self.client.post(self.base + '/materials/', {
                'title': '文件', 'file': SimpleUploadedFile(name, content), **extra},
                format='multipart').status_code, 400)
        with mock.patch('course_team.storage.MAX_FILE_BYTES', 3):
            self.assertEqual(self.client.post(self.base + '/materials/', {
                'title': '文件', 'file': SimpleUploadedFile('a.txt', b'four')},
                format='multipart').status_code, 400)
        self.assertEqual(self.stored(), [])

    def test_material_visibility_requires_same_course_unique_nonempty_classes(self):
        self.foreign.admin_classes.add(self.otherAdminClass)
        foreign = TeachingClass.objects.create(course_college=self.foreign,
                                              term=self.myTeachingClass.term, name='别课教学班')
        foreign.admin_classes.add(self.otherAdminClass)
        for ids in ([], [foreign.pk], [999999], [True],
                    [self.myTeachingClass.pk, self.myTeachingClass.pk]):
            self.assertEqual(self.client.post(self.base + '/materials/', {
                'title': '文件', 'file': SimpleUploadedFile('a.txt', b'ok'),
                'visibility': 'classes', 'teachingClassIds': ids}, format='multipart').status_code, 400)
        self.assertFalse(CourseMaterial.objects.exists())
        self.assertEqual(self.stored(), [])

    def test_teacher_must_really_teach_course_not_merely_be_in_pool(self):
        self.cc.teachers.add(self.courseTeam)
        self.client.force_login(self.courseTeam)
        # 课程组负责人可管理，不代表已经在任课教师 API 中授课。
        self.courseTeam.groups.add(self.teacher.groups.get(name='教师'))
        self.assertEqual(self.client.get(self.teacherBase + '/knowledge/').status_code, 404)
        self.myTeachingClass.teachers.add(self.courseTeam)
        self.assertEqual(self.client.get(self.teacherBase + '/knowledge/').status_code, 200)

    def test_teacher_sees_only_visible_ready_knowledge_and_is_read_only(self):
        visible = self.knowledge()
        hidden = self.knowledge(isVisible=False)
        failed = self.document(name='bad.json', content=b'{')
        self.client.force_login(self.teacher)
        response = self.client.get(self.teacherBase + '/knowledge/')
        self.assertEqual([row['id'] for row in response.data['results']], [visible['id']])
        for obj in (hidden, failed):
            self.assertEqual(self.client.get(f"/api/course-team/teacher/knowledge/{obj['id']}/").status_code, 404)
        self.assertEqual(self.client.post(self.teacherBase + '/knowledge/', {}, format='json').status_code, 405)
        self.assertEqual(self.client.patch(f"/api/course-team/teacher/knowledge/{visible['id']}/", {
            'title': '改'}, format='json').status_code, 405)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(self.teacherBase + '/knowledge/').status_code, 403)

    def test_selected_class_material_cannot_be_downloaded_by_other_class_teacher(self):
        obj = self.material(visibility='classes', teachingClassIds=[self.myTeachingClass.pk])
        self.client.force_login(self.teacher2)
        self.assertEqual(self.client.get(self.teacherBase + '/materials/').data['count'], 0)
        with mock.patch('course_team.storage.privateStorage') as storage:
            self.assertEqual(self.client.get(
                f"/api/course-team/teacher/materials/{obj['id']}/download/").status_code, 404)
            storage.assert_not_called()
        self.client.force_login(self.teacher)
        response = self.client.get(f"/api/course-team/teacher/materials/{obj['id']}/download/")
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content), b'%PDF-test')
        finally:
            response.close()

    def test_teacher_loses_shared_access_when_removed_from_last_class(self):
        obj = self.material()
        self.myTeachingClass.teachers.remove(self.teacher)
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get(f"/api/course-team/teacher/materials/{obj['id']}/").status_code, 404)
        self.assertEqual(self.client.get(self.teacherBase + '/materials/').status_code, 404)

    def test_material_cross_course_download_and_mutation_deny_before_file_access(self):
        obj = self.material()
        CourseMaterial.objects.filter(pk=obj['id']).update(course_college=self.foreign)
        url = f"/api/course-team/materials/{obj['id']}/"
        with mock.patch('course_team.storage.privateStorage') as storage:
            self.assertEqual(self.client.get(url + 'download/').status_code, 404)
            self.assertEqual(self.client.patch(url, {'title': '篡改'}, format='json').status_code, 404)
            self.assertEqual(self.client.delete(url).status_code, 404)
            storage.assert_not_called()

    def test_archived_visibility_cannot_be_changed_but_material_content_can_be_corrected(self):
        obj = self.material(visibility='classes', teachingClassIds=[self.myTeachingClass.pk])
        term, _ = AcademicTerm.objects.get_or_create(code=self.myTeachingClass.term,
                                                   defaults={'name': '学期'})
        term.status = 'archived'
        term.is_current = False
        term.save()
        url = f"/api/course-team/materials/{obj['id']}/"
        self.assertEqual(self.client.patch(url, {'visibility': 'course'}, format='json').status_code, 400)
        self.assertEqual(self.client.delete(url).status_code, 400)
        self.assertEqual(self.client.patch(url, {'title': '修正文案'}, format='json').status_code, 200)
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get(f"/api/course-team/teacher/materials/{obj['id']}/").status_code, 200)

    def test_chapter_relations_must_belong_to_same_course(self):
        foreign = CourseKnowledge.objects.create(course_college=self.foreign, title='别课', content='正文')
        for ids in ([foreign.pk], [True], [999999], [foreign.pk, foreign.pk]):
            self.assertEqual(self.client.post(self.base + '/chapters/', {
                'title': '章节', 'knowledgeIds': ids}, format='json').status_code, 400)
        self.assertFalse(CourseChapter.objects.exists())

    def test_chapter_linked_resources_require_explicit_detach_before_delete(self):
        knowledge = self.knowledge()
        material = self.material()
        chapter = self.chapter(knowledgeIds=[knowledge['id']], materialIds=[material['id']])
        for resource, obj in (('knowledge', knowledge), ('materials', material)):
            self.assertEqual(self.client.delete(f"/api/course-team/{resource}/{obj['id']}/").status_code, 400)
        self.assertEqual(self.client.patch(f"/api/course-team/chapters/{chapter['id']}/", {
            'knowledgeIds': [], 'materialIds': []}, format='json').status_code, 200)
        self.assertEqual(self.client.delete(f"/api/course-team/knowledge/{knowledge['id']}/").status_code, 204)

    def test_cross_course_chapter_detail_mutation_and_order_rejected(self):
        foreign = CourseChapter.objects.create(course_college=self.foreign, title='别课章节', position=1)
        url = f'/api/course-team/chapters/{foreign.pk}/'
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.patch(url, {'title': '篡改'}, format='json').status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
        self.assertEqual(self.client.post(self.base + '/chapter-order/', {
            'chapterIds': [foreign.pk]}, format='json').status_code, 400)

    def test_chapter_order_is_complete_unique_and_atomic(self):
        one, two, three = self.chapter(), self.chapter(), self.chapter()
        ids = [three['id'], one['id'], two['id']]
        response = self.client.post(self.base + '/chapter-order/', {'chapterIds': ids}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['id'] for row in response.data], ids)
        self.assertEqual([row['position'] for row in response.data], [1, 2, 3])
        for invalid in (ids[:-1], [ids[0]] * 3, ids + [999999]):
            self.assertEqual(self.client.post(self.base + '/chapter-order/', {
                'chapterIds': invalid}, format='json').status_code, 400)
        self.assertEqual(list(CourseChapter.objects.values_list('id', flat=True)), ids)

    def test_teacher_chapter_filters_unreadable_linked_resource_ids(self):
        visible, hidden = self.knowledge(), self.knowledge(isVisible=False)
        material = self.material(visibility='classes', teachingClassIds=[self.otherTeachingClass.pk])
        chapter = self.chapter(knowledgeIds=[visible['id'], hidden['id']], materialIds=[material['id']])
        hiddenChapter = self.chapter(isVisible=False)
        self.client.force_login(self.teacher)
        response = self.client.get(f"/api/course-team/teacher/chapters/{chapter['id']}/")
        self.assertEqual(response.data['knowledgeIds'], [visible['id']])
        self.assertEqual(response.data['materialIds'], [])
        self.assertEqual(self.client.get(f"/api/course-team/teacher/chapters/{hiddenChapter['id']}/").status_code, 404)

    def test_teacher_chapter_page_prefetches_authorized_relations(self):
        visible = self.knowledge()
        chapters = CourseChapter.objects.bulk_create([
            CourseChapter(course_college=self.cc, title=f'章节{i}', position=i + 1) for i in range(23)])
        for chapter in chapters:
            chapter.knowledge_items.add(visible['id'])
        self.client.force_login(self.teacher)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.teacherBase + '/chapters/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 23)
        self.assertEqual(len(response.data['results']), 20)
        self.assertLessEqual(len(queries), 10)

    def test_course_college_deletion_protected_by_shared_resources(self):
        self.knowledge()
        with self.assertRaises(ProtectedError):
            self.cc.delete()

    def test_queries_validate_filters_and_paginate(self):
        CourseKnowledge.objects.bulk_create([
            CourseKnowledge(course_college=self.cc, title=f'知识{index}', content='正文')
            for index in range(23)])
        response = self.client.get(self.base + '/knowledge/')
        self.assertEqual(response.data['count'], 23)
        self.assertEqual(len(response.data['results']), 20)
        self.assertEqual(self.client.get(self.base + '/knowledge/?kind=unknown').status_code, 400)
        self.assertEqual(self.client.get(self.base + '/knowledge/?q=' + 'x' * 101).status_code, 400)
