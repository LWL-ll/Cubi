"""教务基础数据维护；停用不删除历史，已有归属数据限制结构变更。"""
from collections.abc import Mapping

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.db.models.deletion import ProtectedError
from django.http import Http404

from users.models import StudentProfile, TeacherProfile, User
from users.permissions import hasPerm
from .models import AdminClass, Course, Department, Major, TeachingClass
from .term_service import lockMembershipTerms


MODELS = {'departments': Department, 'majors': Major, 'courses': Course,
          'admin-classes': AdminClass}
FIELDS = {
    'departments': {'code', 'name', 'isActive'},
    'majors': {'code', 'name', 'isActive', 'departmentId'},
    'courses': {'code', 'name', 'isActive'},
    'admin-classes': {'code', 'name', 'isActive', 'majorId', 'enrollmentYear'},
}


def objectData(payload, allowed, *, required=()):
    if (not isinstance(payload, Mapping) or not payload
            or set(payload) - set(allowed) or set(required) - set(payload)):
        raise ValidationError('请求体须为包含必填字段的对象，且只能包含允许的字段')
    return payload


def integerId(value, name='id'):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    if type(value) is not int or not 0 < value < 2 ** 63:
        raise ValidationError(f'{name} 须为正整数')
    return value


def textValue(value, maximum, name, *, blank=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValidationError(f'{name} 须为不超过 {maximum} 字的文本')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValidationError(f'{name} 包含无效字符') from None
    if '\x00' in value:
        raise ValidationError(f'{name} 包含无效字符')
    value = value.strip()
    if not blank and not value:
        raise ValidationError(f'{name} 不能为空')
    return value


def yearValue(value):
    if type(value) is not int or not 1 <= value <= 9999:
        raise ValidationError('enrollmentYear 须为 1 至 9999 的整数')
    return value


def requireOffice(actor, kind):
    permission = 'class:manage' if kind == 'admin-classes' else 'course:manage'
    if not actor.is_authenticated or not actor.is_active or not hasPerm(actor, permission):
        raise PermissionDenied('没有基础数据管理权限')


def baseData(obj):
    data = {'id': obj.pk, 'code': obj.code, 'name': obj.name, 'isActive': obj.is_active}
    if isinstance(obj, Major):
        data['departmentId'] = obj.department_id
    if isinstance(obj, AdminClass):
        data.update(majorId=obj.major_id, enrollmentYear=obj.enrollment_year)
    return data


def getBase(kind, objectId):
    obj = MODELS[kind].objects.filter(pk=integerId(objectId)).first()
    if obj is None:
        raise Http404('基础数据不存在')
    return obj


def _guardTeachingHistory(kind, obj):
    from .term_service import requireClassWrite

    lookups = {
        'departments': {'course_college__department_id': obj.pk},
        'majors': {'admin_classes__major_id': obj.pk},
        'courses': {'course_college__course_id': obj.pk},
        'admin-classes': {'admin_classes__pk': obj.pk},
    }
    for teachingClass in TeachingClass.objects.filter(**lookups[kind]).distinct().order_by('term', 'pk'):
        requireClassWrite(teachingClass, allowClosed=True)


def _hasClassRelations(obj):
    return (obj.memberships.exists() or obj.teaching_classes.exists()
            or obj.course_colleges.exists())


def _setFields(kind, obj, payload):
    model = MODELS[kind]
    for key in ('name', 'code'):
        if key in payload:
            setattr(obj, key, textValue(payload[key], model._meta.get_field(key).max_length,
                                       key, blank=kind == 'admin-classes' and key == 'code'))
    if 'isActive' in payload:
        if type(payload['isActive']) is not bool:
            raise ValidationError('isActive 须为布尔值')
        obj.is_active = payload['isActive']
    if kind == 'majors' and 'departmentId' in payload:
        departmentId = integerId(payload['departmentId'], 'departmentId')
        Department.objects.filter(pk=departmentId).update(id=F('id'))
        target = Department.objects.filter(pk=departmentId).first()
        if target is None:
            raise ValidationError('所属学院不存在')
        if obj.pk and obj.department_id != target.pk and (
                obj.admin_classes.exists() or obj.student_profiles.exists()):
            raise ValidationError('专业已有班级或学籍，不能更改所属学院')
        if not target.is_active and (not obj.pk or obj.department_id != target.pk or obj.is_active):
            raise ValidationError('所属学院未启用')
        obj.department = target
    if kind == 'admin-classes':
        targetId = integerId(payload.get('majorId', obj.major_id), 'majorId')
        Major.objects.filter(pk=targetId).update(id=F('id'))
        major = Major.objects.select_related('department').filter(pk=targetId).first()
        if major is None:
            raise ValidationError('所属专业不存在')
        year = yearValue(payload['enrollmentYear']) if 'enrollmentYear' in payload else obj.enrollment_year
        structural = obj.major_id != major.pk or obj.enrollment_year != year
        if obj.pk and structural and _hasClassRelations(obj):
            raise ValidationError('行政班已有成员历史或课程关联，不能更改专业、入学年份')
        if (not obj.pk or structural or payload.get('isActive') is True) and (
                not major.is_active or not major.department.is_active):
            raise ValidationError('所属专业或学院未启用')
        obj.major = major
        obj.enrollment_year = year
    if kind == 'majors' and payload.get('isActive') is True and not obj.department.is_active:
        raise ValidationError('所属学院未启用')


@transaction.atomic
def saveBase(actor, kind, payload, objectId=None):
    from .audit_service import recordAudit

    lockMembershipTerms(None)
    requireOffice(actor, kind)
    required = ('name', 'code') if kind != 'admin-classes' else ('name', 'majorId', 'enrollmentYear')
    if kind == 'majors':
        required += ('departmentId',)
    objectData(payload, FIELDS[kind], required=required if objectId is None else ())
    before = None
    if objectId is not None:
        obj = getBase(kind, objectId)
        if set(payload) != {'isActive'}:
            _guardTeachingHistory(kind, obj)
        if not MODELS[kind].objects.filter(pk=obj.pk).update(id=F('id')):
            raise Http404('基础数据不存在')
        obj = getBase(kind, objectId)
        before = baseData(obj)
    else:
        obj = MODELS[kind]()
    _setFields(kind, obj, payload)
    if kind == 'courses' and before and before['code'] != obj.code and obj.course_colleges.exists():
        raise ValidationError('课程已有学院关联，不能更改课程编号')
    try:
        obj.full_clean()
        obj.save()
    except IntegrityError:
        raise ValidationError('编码或名称与现有数据冲突') from None
    recordAudit(actor, 'base.create' if before is None else 'base.update', obj,
                before=before, after=baseData(obj))
    return obj


@transaction.atomic
def deleteBase(actor, kind, objectId):
    from .audit_service import recordAudit

    lockMembershipTerms(None)
    requireOffice(actor, kind)
    obj = getBase(kind, objectId)
    _guardTeachingHistory(kind, obj)
    if not MODELS[kind].objects.filter(pk=obj.pk).update(id=F('id')):
        raise Http404('基础数据不存在')
    obj = getBase(kind, objectId)
    if kind == 'departments' and (obj.majors.exists() or obj.course_colleges.exists()
                                  or TeacherProfile.objects.filter(department=obj).exists()):
        raise ValidationError('学院已有专业、课程或教师档案，不能删除')
    if kind == 'majors' and (obj.admin_classes.exists() or StudentProfile.objects.filter(major=obj).exists()):
        raise ValidationError('专业已有行政班或学籍，不能删除')
    if kind == 'courses' and obj.course_colleges.exists():
        raise ValidationError('课程已有学院关联，不能删除')
    if kind == 'admin-classes' and _hasClassRelations(obj):
        raise ValidationError('行政班已有成员历史或课程关联，不能删除')
    recordAudit(actor, 'base.delete', obj, before=baseData(obj))
    try:
        obj.delete()
    except ProtectedError:
        raise ValidationError('基础数据仍被引用，不能删除') from None


@transaction.atomic
def updateStudentAcademic(actor, profile, payload):
    """学籍异动先结束当前成员；恢复在读不隐式恢复班级关系。"""
    from django.utils import timezone
    from users.account_service import getManagedAccount
    from .audit_service import recordAudit
    from .services import enrollStudent

    lockMembershipTerms(profile)
    requireOffice(actor, 'admin-classes')
    objectData(payload, {'academicStatus', 'majorId', 'enrollmentYear', 'adminClassId'})
    initialTarget = None
    if 'adminClassId' in payload:
        initialTarget = AdminClass.objects.filter(
            pk=integerId(payload['adminClassId'], 'adminClassId')).first()
        if initialTarget is None:
            raise ValidationError('行政班不存在')
    lockMembershipTerms(profile, initialTarget)
    User.objects.filter(pk=profile.identity.user_id).update(id=F('id'))
    getManagedAccount(actor, profile.identity.user_id)
    if not StudentProfile.objects.filter(pk=profile.pk).update(id=F('id')):
        raise Http404('学生不存在')
    profile = StudentProfile.objects.select_related('identity__user', 'major__department').get(pk=profile.pk)
    before = {'academicStatus': profile.academic_status, 'majorId': profile.major_id,
              'enrollmentYear': profile.enrollment_year}
    status = payload.get('academicStatus', profile.academic_status)
    if not isinstance(status, str) or status not in dict(StudentProfile._meta.get_field('academic_status').choices):
        raise ValidationError('academicStatus 无效')
    majorId = integerId(payload.get('majorId', profile.major_id), 'majorId')
    Major.objects.filter(pk=majorId).update(id=F('id'))
    major = Major.objects.select_related('department').filter(pk=majorId).first()
    if major is None:
        raise ValidationError('所属专业不存在')
    year = yearValue(payload.get('enrollmentYear', profile.enrollment_year))
    structural = majorId != profile.major_id or year != profile.enrollment_year
    if structural and (not major.is_active or not major.department.is_active):
        raise ValidationError('所属专业或学院未启用')
    target = None
    if 'adminClassId' in payload:
        if status != StudentProfile.ACADEMIC_ACTIVE:
            raise ValidationError('非在读学生不能分配行政班')
        targetId = integerId(payload['adminClassId'], 'adminClassId')
        AdminClass.objects.filter(pk=targetId).update(id=F('id'))
        target = AdminClass.objects.select_related('major__department').filter(pk=targetId).first()
        if target is None:
            raise ValidationError('行政班不存在')
        if target.major_id != majorId or target.enrollment_year != year:
            raise ValidationError('目标行政班须与变更后的专业、入学年份一致')
        if not target.is_active or not major.is_active or not major.department.is_active:
            raise ValidationError('目标行政班、专业或学院未启用')
    current = profile.memberships.filter(left_at__isnull=True).first()
    before['adminClassId'] = current.admin_class_id if current else None
    today = timezone.localdate()
    closing = current is not None and (status != StudentProfile.ACADEMIC_ACTIVE or structural)
    if closing:
        if current.joined_at > today:
            raise ValidationError('原入班日期晚于今天，不能办理当日学籍异动')
        current.left_at = today
        current.save(update_fields=['left_at'])
    profile.academic_status = status
    profile.major = major
    profile.enrollment_year = year
    profile.save(update_fields=['academic_status', 'major', 'enrollment_year', 'updated_at'])
    if target is not None:
        enrollStudent(profile, target, today)
    current = profile.memberships.filter(left_at__isnull=True).first()
    recordAudit(actor, 'student.academic_update', profile, before=before, after={
        'academicStatus': status, 'majorId': majorId, 'enrollmentYear': year,
        'adminClassId': current.admin_class_id if current else None})
    return profile
