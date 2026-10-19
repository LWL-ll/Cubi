"""Django 后台：题目管理与题库批量导入（仅系统管理员）

权限口径（2026-10-20）：
- 公共题库只有 is_superuser 可增删改；普通 staff 即使有 admin 权限也不放行
- 批量导入页挂在 Problem 的 change_list 下：/admin/teaching/problem/import/
"""
import logging

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.urls import path, reverse
from rest_framework.exceptions import ValidationError

from tasks.models import AsyncTask

from .models import Problem
from .problem_import import formatErrorDetail, importProblemZip

logger = logging.getLogger('codemind.problem')


@admin.register(Problem)
class ProblemAdmin(admin.ModelAdmin):
    """题目后台：列表/搜索/筛选 + 批量导入入口（仅超级用户）"""

    list_display = ('id', 'title', 'is_public', 'tier', 'revision',
                    'source_code', 'owner', 'updated_at')
    list_filter = ('is_public', 'tier')
    search_fields = ('title', 'source_code')
    ordering = ('-updated_at', '-id')
    # 导入溯源与修订号不允许在后台直接改（改题由 save_model 自动 +1）
    readonly_fields = ('source_code', 'source_hash', 'revision',
                       'created_at', 'updated_at')

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def save_model(self, request, obj, form, change):
        """后台改题同样执行「修改即 revision+1」"""
        if change:
            obj.revision += 1
        super().save_model(request, obj, form, change)

    def get_urls(self):
        urls = super().get_urls()
        customUrls = [
            path('import/', self.admin_site.admin_view(self.import_view),
                 name='teaching_problem_import'),
        ]
        return customUrls + urls

    def import_view(self, request):
        """题库批量导入页：GET 表单 / POST 执行导入"""
        if not request.user.is_superuser:
            raise PermissionDenied

        result = None
        if request.method == 'POST':
            zipFile = request.FILES.get('file')
            if zipFile is None:
                messages.error(request, '请选择 ZIP 文件')
            else:
                overwrite = request.POST.get('overwrite') == 'on'
                try:
                    result = importProblemZip(
                        request.user, zipFile, overwrite=overwrite)
                except ValidationError as exc:
                    messages.error(request, formatErrorDetail(exc))
                except Exception:
                    logger.exception('后台题库导入失败')
                    messages.error(request, '导入失败，请检查文件后重试')

        recentBatches = AsyncTask.objects.filter(
            type=AsyncTask.TYPE_IMPORT).order_by('-created_at')[:10]
        context = {
            **self.admin_site.each_context(request),
            'title': '题库批量导入',
            'opts': self.model._meta,
            'result': result,
            'recentBatches': recentBatches,
            'importUrl': reverse('admin:teaching_problem_import'),
        }
        return render(request, 'admin/teaching/problem/import.html', context)
