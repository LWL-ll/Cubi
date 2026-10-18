from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import SchoolIdentity, StudentProfile, TeacherProfile, User


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
