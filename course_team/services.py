"""功能权限与课程组任职同时校验；所有资源关系限定同一课程×学院。"""
import hashlib
import json

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F, Max, Prefetch, Q
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from academics.audit_service import recordAudit
from academics.models import Course, CourseCollege, TeachingClass
from users.permissions import hasPerm

from .models import CourseChapter, CourseKnowledge, CourseMaterial
from .serializers import KnowledgeInput
from .storage import deleteFile, parseDocument, readUpload, storeFile


def getCourseCollege(user, ccId, code):
    if not user.is_authenticated or not hasPerm(user, code):
        raise PermissionDenied('没有课程组管理权限')
    queryset = CourseCollege.objects.select_related('course', 'department')
    if not user.is_superuser:
        queryset = queryset.filter(leader=user)
    obj = queryset.filter(pk=ccId).first()
    if obj is None:
        raise Http404('课程组不存在')
    return obj


def lockCourseCollege(user, ccId, code):
    # 与任职变更统一为先课程、再课程组，防止负责人变更与审计外键互相等待。
    # 子查询直接放在 UPDATE 中，使事务首条数据库操作获得写锁。
    courseIds = CourseCollege.objects.filter(pk=ccId).values('course_id')
    if not Course.objects.filter(pk__in=courseIds).update(id=F('id')):
        raise Http404('课程组不存在')
    if not CourseCollege.objects.filter(pk=ccId).update(id=F('id')):
        raise Http404('课程组不存在')
    return getCourseCollege(user, ccId, code)


def teacherCourseColleges(user):
    queryset = CourseCollege.objects.select_related('course', 'department')
    if user.is_superuser:
        return queryset
    return queryset.filter(teaching_classes__teachers=user).distinct()


def getTeacherCourseCollege(user, ccId):
    obj = teacherCourseColleges(user).filter(pk=ccId).first()
    if obj is None:
        raise Http404('课程组不存在')
    return obj


def readableKnowledge(user, cc):
    return CourseKnowledge.objects.filter(course_college=cc, is_visible=True, status='ready')


def readableMaterials(user, cc):
    queryset = CourseMaterial.objects.filter(course_college=cc)
    if user.is_superuser:
        return queryset
    return queryset.filter(Q(visibility='course') | Q(
        visibility='classes', visible_classes__teachers=user)).distinct()


def readableChapters(user, cc):
    return CourseChapter.objects.filter(course_college=cc, is_visible=True).prefetch_related(
        Prefetch('knowledge_items', queryset=readableKnowledge(user, cc).only('id'),
                 to_attr='readable_knowledge'),
        Prefetch('materials', queryset=readableMaterials(user, cc).only('id'),
                 to_attr='readable_materials'))


def getManagedObject(user, model, objId, code):
    obj = model.objects.filter(pk=objId).first()
    if obj is None:
        raise Http404('资源不存在')
    getCourseCollege(user, obj.course_college_id, code)
    return obj


def metadata(obj):
    result = {'title': obj.title, 'courseCollegeId': obj.course_college_id}
    if isinstance(obj, CourseKnowledge):
        result.update(kind=obj.kind, status=obj.status, isVisible=obj.is_visible,
                      bodySha256=hashlib.sha256(obj.content.encode('utf-8')).hexdigest())
    elif isinstance(obj, CourseMaterial):
        result.update(visibility=obj.visibility, originalName=obj.original_name, size=obj.size,
                      teachingClassIds=list(
            obj.visible_classes.values_list('id', flat=True)))
    elif isinstance(obj, CourseChapter):
        result.update(position=obj.position, isVisible=obj.is_visible,
                      knowledgeIds=list(obj.knowledge_items.values_list('id', flat=True)),
                      materialIds=list(obj.materials.values_list('id', flat=True)))
    return result


def _freshObject(user, obj, code):
    lockCourseCollege(user, obj.course_college_id, code)
    fresh = type(obj).objects.filter(pk=obj.pk, course_college_id=obj.course_college_id).first()
    if fresh is None:
        raise Http404('资源不存在')
    return fresh


def createKnowledge(user, cc, fields):
    with transaction.atomic():
        cc = lockCourseCollege(user, cc.pk, 'knowledge:manage')
        obj = CourseKnowledge.objects.create(course_college=cc, updated_by=user, **fields)
        recordAudit(user, 'course_team.knowledge.create', obj, after=metadata(obj))
        return obj


def updateKnowledge(user, obj, fields):
    with transaction.atomic():
        obj = _freshObject(user, obj, 'knowledge:manage')
        if obj.kind != 'entry' and 'content' in fields:
            raise ValidationError('文档正文由源文件解析，请替换源文件')
        before = metadata(obj)
        for key, value in fields.items():
            setattr(obj, key, value)
        obj.updated_by = user
        obj.save()
        recordAudit(user, 'course_team.knowledge.update', obj, before=before, after=metadata(obj))
        return obj


def importKnowledge(user, cc, uploaded):
    getCourseCollege(user, cc.pk, 'knowledge:manage')
    name, content = readUpload(uploaded, document=True)
    if not name.lower().endswith('.json'):
        raise ValidationError({'file': '条目导入只支持 UTF-8 JSON 数组，字段为 title/content/isVisible'})
    try:
        rows = json.loads(content.decode('utf-8-sig'))
    except (UnicodeError, ValueError, RecursionError):
        raise ValidationError({'file': 'JSON 格式或 UTF-8 编码错误'}) from None
    if not isinstance(rows, list) or not 0 < len(rows) <= 1000:
        raise ValidationError({'file': 'JSON 须包含 1 至 1000 个知识条目'})
    serializer = KnowledgeInput(data=rows, many=True)
    serializer.is_valid(raise_exception=True)
    with transaction.atomic():
        cc = lockCourseCollege(user, cc.pk, 'knowledge:manage')
        objects = [CourseKnowledge(course_college=cc, updated_by=user, **row)
                   for row in serializer.validated_data]
        CourseKnowledge.objects.bulk_create(objects)
        recordAudit(user, 'course_team.knowledge.import', cc, after={
            'count': len(objects), 'knowledgeIds': [obj.pk for obj in objects]})
        return objects


def saveDocument(user, cc, fields, *, obj=None):
    getCourseCollege(user, cc.pk, 'knowledge:manage')
    fields = dict(fields)
    name, content = readUpload(fields.pop('file'), document=True)
    parsed, error = parseDocument(name, content)
    storageName = storeFile(name, content)
    try:
        with transaction.atomic():
            lockCourseCollege(user, cc.pk, 'knowledge:manage')
            if obj is None:
                obj = CourseKnowledge(course_college=cc, kind='document')
                before, oldName = None, ''
            else:
                obj = _freshObject(user, obj, 'knowledge:manage')
                if obj.kind != 'document':
                    raise ValidationError('知识条目不能直接替换为文档')
                before, oldName = metadata(obj), obj.storage_name
            for key, value in fields.items():
                setattr(obj, key, value)
            obj.content, obj.parse_error = parsed, error
            obj.status = 'failed' if error else 'ready'
            obj.storage_name, obj.original_name, obj.size = storageName, name, len(content)
            obj.updated_by = user
            obj.save()
            if oldName:
                transaction.on_commit(lambda: deleteFile(oldName))
            recordAudit(user, 'course_team.document.save', obj, before=before, after=metadata(obj))
            return obj
    except Exception:
        deleteFile(storageName)
        raise


def reparseDocument(user, obj):
    from .storage import MAX_DOCUMENT_BYTES, openFile

    with transaction.atomic():
        obj = _freshObject(user, obj, 'knowledge:manage')
        if obj.kind != 'document':
            raise ValidationError('只有文档可以重新解析')
        # _freshObject 已重新检查任职范围，之后才能打开私有文件。
        with openFile(obj) as stream:
            content = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(content) > MAX_DOCUMENT_BYTES:
            raise ValidationError('源文件超过文档大小限制')
        parsed, error = parseDocument(obj.original_name, content)
        obj.content, obj.parse_error = parsed, error
        obj.status = 'failed' if error else 'ready'
        obj.updated_by = user
        obj.save()
        recordAudit(user, 'course_team.document.reparse', obj, after=metadata(obj))
        return obj


def _materialClasses(cc, visibility, classIds):
    if visibility == 'course':
        if classIds:
            raise ValidationError({'teachingClassIds': '课程可见资料不能同时指定教学班'})
        return []
    if not classIds:
        raise ValidationError({'teachingClassIds': '指定班级可见时至少选择一个教学班'})
    rows = list(TeachingClass.objects.filter(pk__in=classIds, course_college=cc))
    if len(rows) != len(classIds):
        raise ValidationError({'teachingClassIds': '教学班不存在或不属于本课程组'})
    return rows


def _lockVisibilityTerms(classes):
    """可见范围涉及归档班时拒绝；学期锁在课程组锁之前获取。"""
    from academics.models import AcademicTerm

    codes = sorted({obj.term for obj in classes})
    for code in codes:
        AcademicTerm.objects.filter(code=code).update(id=F('id'))
    if AcademicTerm.objects.filter(code__in=codes, status='archived').exists():
        raise ValidationError('归档教学班的资料可见范围不能变更')


def _classState(classes):
    return {(row.pk, row.term, row.course_college_id) for row in classes}


def _materialScope(obj):
    """在进入写事务前读取待锁学期；锁定后必须核对快照。"""
    fresh = CourseMaterial.objects.filter(pk=obj.pk, course_college_id=obj.course_college_id).first()
    if fresh is None:
        raise Http404('资源不存在')
    return fresh.visibility, list(fresh.visible_classes.all())


def _checkMaterialScope(obj, snapshot):
    visibility, classes = snapshot
    current = list(obj.visible_classes.all())
    if obj.visibility != visibility or _classState(current) != _classState(classes):
        raise ValidationError('资料可见范围已变化，请刷新后重试')


def saveMaterial(user, cc, fields, *, obj=None):
    getCourseCollege(user, cc.pk, 'material:class_visible')
    fields = dict(fields)
    uploaded = fields.pop('file', None)
    newName = None
    if uploaded is not None:
        original, content = readUpload(uploaded)
        newName = storeFile(original, content)
        fields.update(original_name=original, size=len(content), storage_name=newName)
    elif obj is None:
        raise ValidationError({'file': '请上传资料文件'})
    try:
        # 只在请求明确修改范围时读取范围快照；元数据更新始终保留锁定后的最新范围。
        scopeWrite = obj is None or bool({'visibility', 'class_ids'} & fields.keys())
        snapshot, classes, changingVisibility = None, [], False
        if scopeWrite:
            snapshot = _materialScope(obj) if obj else ('course', [])
            oldVisibility, oldClasses = snapshot
            visibility = fields.pop('visibility', oldVisibility)
            classIds = fields.pop('class_ids', [row.pk for row in oldClasses]
                                  if visibility == 'classes' else [])
            classes = _materialClasses(cc, visibility, classIds)
            changingVisibility = (obj is None or visibility != oldVisibility
                                  or {row.pk for row in classes} != {row.pk for row in oldClasses})
        with transaction.atomic():
            if changingVisibility:
                # classes/oldClasses 已在事务外求值，首条 SQL 是学期 UPDATE。
                _lockVisibilityTerms(oldClasses + classes)
            lockCourseCollege(user, cc.pk, 'material:class_visible')
            if obj is None:
                obj = CourseMaterial(course_college=cc)
                before, oldName = None, ''
            else:
                obj = _freshObject(user, obj, 'material:class_visible')
                if scopeWrite:
                    _checkMaterialScope(obj, snapshot)
                before, oldName = metadata(obj), obj.storage_name
            if scopeWrite:
                # 教学班的学期或课程变化后，不使用未锁定的新学期继续写入。
                currentClasses = _materialClasses(cc, visibility, classIds)
                if _classState(currentClasses) != _classState(classes):
                    raise ValidationError('教学班设置已变化，请刷新后重试')
                obj.visibility = visibility
            for key, value in fields.items():
                setattr(obj, key, value)
            obj.updated_by = user
            obj.save()
            if changingVisibility:
                obj.visible_classes.set(classes)
            if newName and oldName:
                transaction.on_commit(lambda: deleteFile(oldName))
            recordAudit(user, 'course_team.material.save', obj, before=before, after=metadata(obj))
            return obj
    except Exception:
        if newName:
            deleteFile(newName)
        raise


def removeResource(user, obj, code):
    getCourseCollege(user, obj.course_college_id, code)
    snapshot = _materialScope(obj) if isinstance(obj, CourseMaterial) else None
    with transaction.atomic():
        if snapshot is not None:
            _lockVisibilityTerms(snapshot[1])
        obj = _freshObject(user, obj, code)
        if snapshot is not None:
            _checkMaterialScope(obj, snapshot)
        if not isinstance(obj, CourseChapter) and obj.chapters.exists():
            raise ValidationError('资源已关联章节，请先解除章节关联')
        name = getattr(obj, 'storage_name', '')
        recordAudit(user, 'course_team.resource.delete', obj, before=metadata(obj))
        obj.delete()
        if name:
            transaction.on_commit(lambda: deleteFile(name))


def saveChapter(user, cc, fields, *, obj=None):
    fields = dict(fields)
    with transaction.atomic():
        lockCourseCollege(user, cc.pk, 'chapter:manage')
        if obj is None:
            last = CourseChapter.objects.filter(course_college=cc).aggregate(last=Max('position'))['last'] or 0
            obj = CourseChapter(course_college=cc, position=last + 1)
            before = None
        else:
            obj = _freshObject(user, obj, 'chapter:manage')
            before = metadata(obj)
        relations = []
        for field, model, relation in (
                ('knowledge_ids', CourseKnowledge, 'knowledge_items'),
                ('material_ids', CourseMaterial, 'materials')):
            if field not in fields:
                continue
            ids = fields.pop(field)
            rows = list(model.objects.filter(pk__in=ids, course_college=cc))
            if len(rows) != len(ids):
                raise ValidationError({field: '资源不存在或不属于本课程组'})
            relations.append((relation, rows))
        for key, value in fields.items():
            setattr(obj, key, value)
        obj.updated_by = user
        obj.save()
        for relation, rows in relations:
            getattr(obj, relation).set(rows)
        recordAudit(user, 'course_team.chapter.save', obj, before=before, after=metadata(obj))
        return obj


def reorderChapters(user, cc, ids):
    with transaction.atomic():
        lockCourseCollege(user, cc.pk, 'chapter:manage')
        current = list(CourseChapter.objects.filter(course_college=cc))
        if len(ids) != len(current) or set(ids) != {obj.pk for obj in current}:
            raise ValidationError({'chapterIds': '必须提供本课程全部章节编号，且不能重复'})
        # 先移到不相交的正数区间，再写最终顺序，兼容即时唯一约束。
        offset = max((obj.position for obj in current), default=0) + len(current) + 1
        CourseChapter.objects.filter(course_college=cc).update(position=F('position') + offset)
        for index, pk in enumerate(ids, 1):
            CourseChapter.objects.filter(pk=pk, course_college=cc).update(
                position=index, updated_by=user, updated_at=timezone.now())
        recordAudit(user, 'course_team.chapter.reorder', cc, after={'chapterIds': ids})
        return CourseChapter.objects.filter(course_college=cc)
