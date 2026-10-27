"""Django 后台写入保留业务审计，同时保留原生 LogEntry。"""
from .audit_service import organizationState, recordAudit


def adminState(obj):
    state = organizationState(obj)
    allowed = {'username', 'email', 'is_staff', 'is_superuser', 'must_change_password',
               'school_code', 'school_number', 'identity_type', 'verification_status',
               'user_id', 'identity_id', 'title', 'academic_status', 'student_profile_id',
               'admin_class_id', 'joined_at', 'left_at'}
    state.update({field.attname: getattr(obj, field.attname) for field in obj._meta.fields
                  if field.attname in allowed})
    for field in ('groups', 'user_permissions', 'permissions'):
        if hasattr(obj, field):
            state[field + 'Ids'] = sorted(getattr(obj, field).values_list('pk', flat=True))
    return state


class AuditAdminMixin:
    def save_model(self, request, obj, form, change):
        before = type(obj).objects.filter(pk=obj.pk).first() if change else None
        request._businessAuditBefore = adminState(before) if before else {}
        super().save_model(request, obj, form, change)

    def log_addition(self, request, obj, message):
        result = super().log_addition(request, obj, message)
        recordAudit(request.user, 'admin.create', obj, after=adminState(obj))
        return result

    def log_change(self, request, obj, message):
        result = super().log_change(request, obj, message)
        recordAudit(request.user, 'admin.update', obj,
                    before=getattr(request, '_businessAuditBefore', {}), after=adminState(obj))
        return result

    def log_deletions(self, request, queryset):
        for obj in queryset:
            recordAudit(request.user, 'admin.delete', obj, before=adminState(obj))
        return super().log_deletions(request, queryset)
