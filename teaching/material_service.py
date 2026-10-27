"""班级资料私有存储；对象授权发生在读取文件之前。"""
import logging
from collections.abc import Mapping
from pathlib import PurePosixPath
import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.db.models import F
from django.http import Http404
from rest_framework.exceptions import ValidationError
from academics.term_service import requireTeachingWrite

from teaching.helpers import getStudentTeachingClass, getTeacherTeachingClass
from teaching.models import TeachingMaterial

logger = logging.getLogger('codemind.material')
MAX_FILE_BYTES = 50 * 1024 * 1024
ALLOWED_SUFFIXES = {
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.txt', '.md', '.csv', '.json', '.zip', '.c', '.py'}


def materialStorage():
    return FileSystemStorage(location=settings.PRIVATE_MATERIAL_ROOT)


def validateMaterial(payload, uploaded, *, creating):
    if not isinstance(payload, Mapping) or set(payload) - {'title', 'description', 'file'}:
        raise ValidationError('只允许 title、description、file 字段')
    if creating and uploaded is None:
        raise ValidationError('请上传文件（字段名 file）')
    if 'file' in payload and uploaded is None:
        raise ValidationError('file 必须是上传文件')
    fields = {}
    for key, limit in (('title', 100), ('description', 2000)):
        if key not in payload:
            if creating:
                if key == 'title':
                    raise ValidationError('标题不能为空')
                fields[key] = ''
            continue
        value = payload[key]
        if not isinstance(value, str) or len(value) > limit or (key == 'title' and not value.strip()):
            raise ValidationError(f'{key} 格式错误或超过 {limit} 字')
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            raise ValidationError(f'{key} 含无效字符')
        fields[key] = value.strip()
    if uploaded is not None:
        name = PurePosixPath(uploaded.name.replace('\\', '/')).name
        if (not name or len(name) > 255 or '\x00' in name
                or PurePosixPath(name).suffix.lower() not in ALLOWED_SUFFIXES):
            raise ValidationError('不支持的文件名或文件类型')
        if not 0 < uploaded.size <= MAX_FILE_BYTES:
            raise ValidationError('文件不能为空，且不能超过 50MB')
        fields.update(original_name=name, size=uploaded.size)
    return fields


def storeUpload(uploaded, originalName):
    name = uuid.uuid4().hex + PurePosixPath(originalName).suffix.lower()
    try:
        return materialStorage().save(name, uploaded)
    except OSError:
        deleteStoredFile(name)
        raise ValidationError('文件保存失败，请稍后重试')


def deleteStoredFile(name):
    try:
        materialStorage().delete(name)
    except OSError:
        # 数据已撤下，下载授权不再存在；保留随机存储标识供运维清理。
        logger.exception('班级资料文件清理失败：%s', name)


def createMaterial(user, teachingClass, payload, uploaded):
    fields = validateMaterial(payload, uploaded, creating=True)
    name = storeUpload(uploaded, fields['original_name'])
    try:
        with transaction.atomic():
            requireTeachingWrite(teachingClass)
            return TeachingMaterial.objects.create(
                teaching_class=teachingClass, uploaded_by=user, storage_name=name, **fields)
    except Exception:
        deleteStoredFile(name)
        raise


def getMaterial(user, materialId, *, student=False):
    material = TeachingMaterial.objects.select_related('uploaded_by', 'teaching_class').filter(pk=materialId).first()
    if material is None:
        raise Http404('资料不存在')
    check = getStudentTeachingClass if student else getTeacherTeachingClass
    try:
        check(user, material.teaching_class_id)
    except Http404:
        raise Http404('资料不存在') from None
    return material


def updateMaterial(user, material, payload, uploaded):
    fields = validateMaterial(payload, uploaded, creating=False)
    teachingClass = material.teaching_class
    newName = None
    if uploaded is not None:
        newName = storeUpload(uploaded, fields['original_name'])
        fields['storage_name'] = newName
        fields['uploaded_by'] = user
    try:
        with transaction.atomic():
            requireTeachingWrite(teachingClass)
            if not TeachingMaterial.objects.filter(pk=material.pk).update(id=F('id')):
                raise Http404('资料不存在')
            material.refresh_from_db()
            oldName = material.storage_name
            for key, value in fields.items():
                setattr(material, key, value)
            material.save()
            if newName:
                transaction.on_commit(lambda: deleteStoredFile(oldName))
        return material
    except Exception:
        if newName:
            deleteStoredFile(newName)
        raise


def removeMaterial(material):
    teachingClass = material.teaching_class
    with transaction.atomic():
        requireTeachingWrite(teachingClass)
        if not TeachingMaterial.objects.filter(pk=material.pk).update(id=F('id')):
            raise Http404('资料不存在')
        material.refresh_from_db()
        name = material.storage_name
        material.delete()
        transaction.on_commit(lambda: deleteStoredFile(name))


def openMaterial(material):
    try:
        return materialStorage().open(material.storage_name, 'rb')
    except FileNotFoundError:
        raise Http404('资料文件不存在，请联系教师重新上传')
