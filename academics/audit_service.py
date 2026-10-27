"""业务审计只接收调用方挑选的字段，不收集密码、文件、试题或请求原文。"""
from collections.abc import Mapping
from datetime import date, datetime

from .models import AuditEvent


SENSITIVE_KEYS = ('password', 'secret', 'token', 'storage', 'file', 'content',
                  'code_body', 'judge_snapshot', 'tests', 'reference_answer')


def _safe(value):
    if isinstance(value, Mapping):
        return {str(key): _safe(item) for key, item in value.items()
                if not any(part in str(key).lower() for part in SENSITIVE_KEYS)}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value[:10000]]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError('审计值必须是显式选择的 JSON 数据')


def recordAudit(actor, action, obj, before=None, after=None):
    """随业务写事务提交；业务回滚时审计同时回滚。"""
    return AuditEvent.objects.create(
        actor=actor, actor_username=actor.username if actor else '', action=action,
        object_type=obj._meta.label_lower if obj is not None else 'operation',
        object_id=str(obj.pk or '') if obj is not None else '',
        before=_safe(before or {}), after=_safe(after or {}))


def organizationState(obj):
    if obj is None:
        return {}
    fields = {'id', 'name', 'code', 'is_active', 'major_id', 'department_id',
              'enrollment_year', 'course_id', 'course_college_id', 'term', 'leader_id'}
    result = {field.attname: getattr(obj, field.attname) for field in obj._meta.fields
              if field.attname in fields}
    for field in ('teachers', 'admin_classes'):
        if hasattr(obj, field):
            result[field + 'Ids'] = sorted(getattr(obj, field).values_list('pk', flat=True))
    if obj._meta.label_lower == 'academics.course':
        result['departmentIds'] = sorted(obj.departments.values_list('pk', flat=True))
    return result
