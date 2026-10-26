"""个人资料白名单更新；学校身份、角色及任教关系不接受自助修改。"""
from collections.abc import Mapping

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import F
from rest_framework.exceptions import ValidationError

from users.models import SchoolIdentity, TeacherProfile, User


def profileData(user):
    identities = list(user.identities.order_by('id').values(
        'school_code', 'school_number', 'identity_type', 'verification_status'))
    teacher = TeacherProfile.objects.select_related('department').filter(
        identity__user=user, identity__identity_type=SchoolIdentity.IDENTITY_TEACHER).first()
    return {
        'username': user.username, 'name': user.name, 'email': user.email,
        'identities': identities,
        'teacher': None if teacher is None else {
            'title': teacher.title, 'departmentId': teacher.department_id,
            'departmentName': teacher.department.name if teacher.department else None},
    }


def updateProfile(user, payload):
    if not isinstance(payload, Mapping) or not payload or set(payload) - {'name', 'email', 'title'}:
        raise ValidationError('只允许修改 name、email、title')
    fields = {}
    for key, maximum in (('name', 50), ('email', 254), ('title', 50)):
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, str) or len(value) > maximum:
            raise ValidationError({key: f'须为不超过 {maximum} 字的文本'})
        value = value.strip()
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            raise ValidationError({key: '包含无效字符'})
        if key == 'name' and not value:
            raise ValidationError({'name': '姓名不能为空'})
        if key == 'email' and value:
            try:
                validate_email(value)
            except DjangoValidationError:
                raise ValidationError({'email': '邮箱格式错误'})
        fields[key] = value
    with transaction.atomic():
        User.objects.filter(pk=user.pk).update(id=F('id'))
        if 'title' in fields:
            teacher = TeacherProfile.objects.filter(
                identity__user=user,
                identity__identity_type=SchoolIdentity.IDENTITY_TEACHER).first()
            if teacher is None:
                raise ValidationError({'title': '没有可修改的教师档案'})
            teacher.title = fields.pop('title')
            teacher.save(update_fields=['title', 'updated_at'])
        if fields:
            User.objects.filter(pk=user.pk).update(**fields)
    user.refresh_from_db()
    return profileData(user)
