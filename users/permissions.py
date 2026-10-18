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
    # 课程组（2026-10-13 按《课题组.xmind》重新设计）
    'knowledge:manage': 'knowledge_manage',
    'material:class_visible': 'material_class_visible',
    'question_bank:manage': 'question_bank_manage',
    'chapter:manage': 'chapter_manage',
    # 教师（仅限教学班级，2026-10-13 按《教师.xmind》重新设计）
    'notice:manage': 'notice_manage',
    'analysis:view': 'analysis_view',
    'material:class_manage': 'material_class_manage',
    'homework:publish': 'homework_publish',
    'homework:bank': 'homework_bank',
    'homework:remind': 'homework_remind',
    'homework:comment': 'homework_comment',
    'homework:answer': 'homework_answer',
    'homework:ai': 'homework_ai',
    # 学生
    'homework:view': 'homework_view',
    'notice:view': 'notice_view',
    'material:view': 'material_view',
    'analysis:view_self': 'analysis_view_self',
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

# 分组判定优先级（按顺序匹配，取第一个命中的分组）
# 课程组组长同时也是教师，组长身份优先显示
GROUP_PRIORITY = ['admin', 'course_team', 'teacher', 'student']

# 每个分组拥有的权限码（按 xmind 分配；数据迁移据此建组）
GROUP_PERMISSIONS = {
    'admin': ['class:manage', 'class:assign_teacher', 'course:manage',
              'course:assign_class', 'user:assign_role', 'profile:edit'],
    # 课程组权限（2026-10-13 按《课题组.xmind》重新设计）
    'course_team': ['knowledge:manage', 'material:class_visible',
                    'question_bank:manage', 'chapter:manage', 'profile:edit'],
    # 教师权限（2026-10-13 按《教师.xmind》重新设计，所有权限仅限教学班级）
    'teacher': ['notice:manage', 'analysis:view', 'material:class_manage',
                'homework:publish', 'homework:bank', 'homework:remind',
                'homework:comment', 'homework:answer', 'homework:ai',
                'profile:edit'],
    'student': ['homework:view', 'notice:view', 'material:view',
                'analysis:view_self', 'profile:edit'],
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
    """返回用户所在分组的 key；超级用户返回 'superuser'；无分组返回 None

    按 GROUP_PRIORITY 显式优先级匹配（组长同时也是教师时返回课程组）。
    """
    if user.is_superuser:
        return 'superuser'
    for key in GROUP_PRIORITY:
        if user.groups.filter(name=GROUP_NAMES[key]).exists():
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


class IsTeacherGroup(BasePermission):
    """DRF 权限类：仅「教师」分组（或超级用户）可访问（教师工作台 API 用）

    与页面的 requireTeacherGroup 装饰器对应：课程组组长同时也是教师，
    故教师组存在即放行。
    """
    def has_permission(self, request, view):
        user = request.user
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return user.groups.filter(name=GROUP_NAMES['teacher']).exists()


class IsStudentGroup(BasePermission):
    """DRF 权限类：仅「学生」分组（或超级用户）可访问（学生工作台 API 用）

    与页面的 requireStudentGroup 装饰器对应：学生 API 的统一入口校验，
    对象级边界（所在班级）由 getStudentClazz 在业务接口内校验。
    """
    def has_permission(self, request, view):
        user = request.user
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return user.groups.filter(name=GROUP_NAMES['student']).exists()


class IsCourseTeamGroup(BasePermission):
    """DRF 权限类：仅「课程组」分组（或超级用户）可访问（课程组 API 用）

    课程组功能（知识库/课程组资料/题库/章节）落地时的 API 入口校验。
    """
    def has_permission(self, request, view):
        user = request.user
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return user.groups.filter(name=GROUP_NAMES['course_team']).exists()
