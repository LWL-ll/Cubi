"""权限体系：权限码与 Django Permission 的映射、分组定义、校验工具

设计说明：
- 权限点按 xmind《用户权限》翻译成权限码（如 class:manage）
- 权限码在 User.Meta.permissions 注册为 Django 原生 Permission，
  在 admin 后台直接可见、可勾选
- 四个分组（Group）由数据迁移自动创建并分配权限
"""
from functools import wraps

from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from rest_framework.permissions import BasePermission

# 权限码 -> Django Permission codename 映射表
PERM_CODES = {
    # 教务
    'class:manage': 'class_manage',
    'class:assign_teacher': 'class_assign_teacher',
    'course:manage': 'course_manage',
    'course:assign_class': 'course_assign_class',
    'user:assign_role': 'user_assign_role',
    # 课程组
    'material:manage': 'material_manage',
    'knowledge:manage': 'knowledge_manage',
    'question_bank:manage': 'question_bank_manage',
    'chapter:manage': 'chapter_manage',
    # 教师
    'notice:manage': 'notice_manage',
    'analysis:view': 'analysis_view',
    'homework:publish': 'homework_publish',
    'homework:bank': 'homework_bank',
    'homework:remind': 'homework_remind',
    'homework:collect': 'homework_collect',
    'homework:ai': 'homework_ai',
    # 学生
    'homework:view': 'homework_view',
    # 通用
    'profile:edit': 'profile_edit',
}

# 分组 key -> 中文组名
GROUP_NAMES = {
    'admin': '教务',
    'course_team': '课程组',
    'teacher': '教师',
    'student': '学生',
}

# 每个分组拥有的权限码（按 xmind 分配；数据迁移据此建组）
GROUP_PERMISSIONS = {
    'admin': ['class:manage', 'class:assign_teacher', 'course:manage',
              'course:assign_class', 'user:assign_role', 'profile:edit'],
    'course_team': ['material:manage', 'knowledge:manage',
                    'question_bank:manage', 'chapter:manage', 'profile:edit'],
    'teacher': ['notice:manage', 'analysis:view', 'material:manage',
                'homework:publish', 'homework:bank', 'homework:remind',
                'homework:collect', 'homework:ai', 'profile:edit'],
    'student': ['homework:view', 'profile:edit'],
}


def hasPerm(user, code):
    """判断用户是否拥有权限码对应的权限；超级用户直接放行"""
    if user.is_superuser:
        return True
    codename = PERM_CODES.get(code)
    if codename is None:
        return False
    return user.has_perm('users.' + codename)


def getGroupKey(user):
    """返回用户所在分组的 key；超级用户返回 'superuser'；无分组返回 None"""
    if user.is_superuser:
        return 'superuser'
    for key, name in GROUP_NAMES.items():
        if user.groups.filter(name=name).exists():
            return key
    return None


def requirePerm(code):
    """视图权限校验装饰器：未登录跳登录页，无权限返回 403

    用法：@requirePerm('class:manage') 加在视图函数上。
    服务端强制校验，前端隐藏按钮只影响体验、不作防线。
    """
    def decorator(viewFunc):
        @wraps(viewFunc)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect('login')
            if not hasPerm(request.user, code):
                return HttpResponseForbidden('403：没有权限访问该功能')
            return viewFunc(request, *args, **kwargs)
        return wrapper
    return decorator


class PermRequired(BasePermission):
    """DRF 权限类：按视图类声明的 permCode 权限码校验（前后端分离 API 用）

    用法：视图类上写 permCode = 'class:manage'，无权限时自动返回 403
    """
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        permCode = getattr(view, 'permCode', None)
        if permCode is None:
            return True
        return hasPerm(request.user, permCode)
