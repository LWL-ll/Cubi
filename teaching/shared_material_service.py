"""教师选材下发：授权后复制为独立班级资料，不授予学生共享库访问权。"""
from collections.abc import Mapping

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F
from django.http import Http404
from rest_framework.exceptions import PermissionDenied, ValidationError

from academics.audit_service import recordAudit
from academics.models import Course, CourseCollege
from academics.term_service import requireTeachingWrite
from course_team.models import CourseChapter, CourseMaterial
from course_team.storage import openFile
from users.permissions import hasPerm
from .helpers import getTeacherTeachingClass
from .material_service import MAX_FILE_BYTES, deleteStoredFile, storeUpload, validateMaterial
from .models import TeachingMaterial
from .submission_service import parseObjectId


def publishSharedMaterial(user, teachingClassId, payload):
    if not hasPerm(user, 'material:class_manage'):
        raise PermissionDenied('没有班级资料管理权限')
    if not isinstance(payload, Mapping) or set(payload) - {'materialId', 'chapterId', 'title', 'description'}:
        raise ValidationError('只能提交 materialId、chapterId、title、description')
    materialId = parseObjectId(payload.get('materialId'), 'materialId')
    chapterId = parseObjectId(payload['chapterId'], 'chapterId') if 'chapterId' in payload else None
    originalClass = getTeacherTeachingClass(user, teachingClassId)
    courseId = originalClass.course_college.course_id
    name = None
    try:
        with transaction.atomic():
            requireTeachingWrite(originalClass)
            Course.objects.filter(pk=courseId).update(id=F('id'))
            CourseCollege.objects.filter(pk=originalClass.course_college_id).update(id=F('id'))
            tc = getTeacherTeachingClass(user, teachingClassId)
            if tc.term != originalClass.term or tc.course_college_id != originalClass.course_college_id:
                raise ValidationError('教学班设置已变化，请刷新后重试')
            source = CourseMaterial.objects.filter(pk=materialId, course_college_id=tc.course_college_id).first()
            if source is None or (source.visibility == 'classes' and not source.visible_classes.filter(pk=tc.pk).exists()):
                raise Http404('资料不存在')
            if chapterId is not None and not CourseChapter.objects.filter(pk=chapterId,
                    course_college_id=tc.course_college_id, is_visible=True, materials=source).exists():
                raise Http404('章节资料不存在')
            if not 0 < source.size <= MAX_FILE_BYTES:
                raise ValidationError('源资料大小不符合班级资料限制')
            # 验证课程、目标教学班和可见范围后，才打开课程组私有文件。
            with openFile(source) as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) != source.size or len(data) > MAX_FILE_BYTES:
                raise ValidationError('源文件大小与资料记录不一致，请联系课程组')
            upload = ContentFile(data, name=source.original_name)
            fields = validateMaterial({'title': payload.get('title', source.title),
                'description': payload.get('description', source.description)}, upload, creating=True)
            name = storeUpload(upload, fields['original_name'])
            material = TeachingMaterial.objects.create(teaching_class=tc, uploaded_by=user,
                                                        storage_name=name, **fields)
            recordAudit(user, 'material.publish_shared', material, after={
                'teachingClassId': tc.pk, 'sourceMaterialId': source.pk, 'sourceChapterId': chapterId,
                'title': material.title, 'size': material.size})
            return material
    except Exception:
        if name:
            deleteStoredFile(name)
        raise
