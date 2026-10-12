from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    """后台用户管理：展示姓名、分组，支持建号/改密/分组分配"""
    list_display = ('username', 'name', 'is_superuser', 'is_staff',
                    'is_active', 'list_groups')
    list_filter = ('is_superuser', 'is_staff', 'is_active', 'groups')
    search_fields = ('username', 'name')

    def list_groups(self, obj):
        """列表页显示用户所属分组名（可多组）"""
        return '、'.join(group.name for group in obj.groups.all())
    list_groups.short_description = '分组'
