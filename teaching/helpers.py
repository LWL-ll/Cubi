"""教学工作台安全工具：教学班对象级校验 + 页面入口校验

《教师.xmind》核心约束：教师的所有权限仅限教学班级。
学生权限的边界同理：学生只能访问自己所在教学班的数据。
分组权限码只回答「能不能用这个功能」；
这里的 getTeacherTeachingClass/getStudentTeachingClass 回答
「能不能对这个教学班用」——所有教学功能接口（API 和页面）
都必须先过这一层校验，否则 404（不暴露教学班存在性）。

V2 表结构（docs/design/数据库表重构设计.md）：
- 教师 → 教学班.teachers（1 跳）
- 学生 → 学校身份 → 学生资料 → 当前成员关系 → 行政班 → 教学班（3 跳推导）
"""
from functools import wraps

from django.http import Http404, HttpResponseForbidden
from django.shortcuts import redirect

from academics.models import TeachingClass

# 教师/学生分组中文名（与 users/permissions.py 的 GROUP_NAMES 保持一致）
TEACHER_GROUP_NAME = '教师'
STUDENT_GROUP_NAME = '学生'


def getTeacherTeachingClass(user, teachingClassId):
    """取教师的教学班：教学班教师表必须包含当前用户，否则 404

    超级用户直接放行（管理员排查问题用）。
    """
    teachingClass = TeachingClass.objects.filter(id=teachingClassId).first()
    if teachingClass is None:
        raise Http404('教学班不存在')
    if not (user.is_superuser
            or teachingClass.teachers.filter(id=user.id).exists()):
        # 统一 404：不让外人探测教学班是否存在
        raise Http404('教学班不存在')
    return teachingClass


def getStudentTeachingClass(user, teachingClassId):
    """取学生所在教学班：3 跳推导（身份 → 资料 → 成员关系 → 行政班 → 教学班）

    只认「当前在班」的成员关系（left_at 为空）；
    学生不在任何组成行政班中时 404，不暴露教学班存在性。
    """
    teachingClass = TeachingClass.objects.filter(id=teachingClassId).first()
    if teachingClass is None:
        raise Http404('教学班不存在')
    if user.is_superuser:
        return teachingClass
    from .student_scope import StudentScope
    return StudentScope(user).requireContent(teachingClassId)


def requireTeacherGroup(viewFunc):
    """页面装饰器：仅「教师」分组（或超级用户）可访问教师工作台

    课程组组长同时也是教师，故课程组成员（含教师组）正常放行。
    """
    @wraps(viewFunc)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not (request.user.is_superuser
                or request.user.groups.filter(name=TEACHER_GROUP_NAME).exists()):
            return HttpResponseForbidden('403：仅教师可访问教师工作台')
        return viewFunc(request, *args, **kwargs)
    return wrapper


def requireStudentGroup(viewFunc):
    """页面装饰器：仅「学生」分组（或超级用户）可访问学生工作台"""
    @wraps(viewFunc)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not (request.user.is_superuser
                or request.user.groups.filter(name=STUDENT_GROUP_NAME).exists()):
            return HttpResponseForbidden('403：仅学生可访问学生工作台')
        return viewFunc(request, *args, **kwargs)
    return wrapper
