from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.admin.utils import unquote
from django.contrib.auth.models import Group
from django.db import transaction

from .models import SchoolIdentity, StudentProfile, TeacherProfile, User


class SuperuserOnlyAdminMixin:
    """账号和角色管理仅限系统管理员，原生模型权限不能用于提权。"""

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser


admin.site.unregister(Group)


@admin.register(Group)
class CustomGroupAdmin(SuperuserOnlyAdminMixin, GroupAdmin):
    pass


@admin.register(User)
class CustomUserAdmin(SuperuserOnlyAdminMixin, UserAdmin):
    """后台用户管理：展示姓名、分组，支持建号/改密/分组分配"""
    list_display = ('username', 'name', 'is_superuser', 'is_staff',
                    'is_active', 'list_groups')
    list_filter = ('is_superuser', 'is_staff', 'is_active', 'groups')
    search_fields = ('username', 'name')

    @transaction.atomic
    def user_change_password(self, request, id, form_url=''):
        target = self.get_object(request, unquote(id)) if request.method == 'POST' else None
        oldHash = target.password if target is not None else None
        response = super().user_change_password(request, id, form_url)
        # Django 负责权限、表单校验和旧会话失效；这里只补重置后的改密标记。
        # 不能只看重定向：后台拒绝冲突表单时也会返回 302。
        if target is not None and target.pk != request.user.pk and response.status_code == 302:
            target.refresh_from_db(fields=['password'])
            if target.password != oldHash and target.has_usable_password():
                type(target).objects.filter(pk=target.pk).update(must_change_password=True)
        return response

    def list_groups(self, obj):
        """列表页显示用户所属分组名（可多组）"""
        return '、'.join(group.name for group in obj.groups.all())
    list_groups.short_description = '分组'


@admin.register(SchoolIdentity)
class SchoolIdentityAdmin(admin.ModelAdmin):
    """后台学校身份管理：学号/工号绑定"""
    list_display = ('school_number', 'identity_type', 'user', 'verification_status')
    list_filter = ('identity_type', 'verification_status')
    search_fields = ('school_number', 'user__username')


@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    """后台学生资料管理"""
    list_display = ('identity', 'major', 'enrollment_year', 'academic_status')
    list_filter = ('academic_status',)
    search_fields = ('identity__school_number',)


@admin.register(TeacherProfile)
class TeacherProfileAdmin(admin.ModelAdmin):
    """后台教师资料管理"""
    list_display = ('identity', 'department', 'title')
    search_fields = ('identity__school_number',)
