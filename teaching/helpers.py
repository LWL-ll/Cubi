"""教学工作台安全工具：教学班级对象级校验 + 页面入口校验

《教师.xmind》核心约束：教师的所有权限仅限教学班级。
学生权限的边界同理：学生只能访问自己所在班级的数据。
分组权限码只回答「能不能用这个功能」；
这里的 getTeacherClazz/getStudentClazz 回答「能不能对这个班用」——
所有教学功能接口（API 和页面）都必须先过这一层校验，
否则 404（不暴露班级存在性）。
"""
from functools import wraps

from django.http import Http404, HttpResponseForbidden
from django.shortcuts import redirect

from academics.models import Clazz

# 教师/学生分组中文名（与 users/permissions.py 的 GROUP_NAMES 保持一致）
TEACHER_GROUP_NAME = '教师'
STUDENT_GROUP_NAME = '学生'


def getTeacherClazz(user, clazzId):
    """取教师的教学班级：班级教师表必须包含当前用户，否则 404

    超级用户直接放行（管理员排查问题用）。
    """
    clazz = Clazz.objects.filter(id=clazzId).first()
    if clazz is None:
        raise Http404('班级不存在')
    if not (user.is_superuser or clazz.teachers.filter(id=user.id).exists()):
        # 统一 404：不让外人探测班级是否存在
        raise Http404('班级不存在')
    return clazz


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


def getStudentClazz(user, clazzId):
    """取学生所在班级：班级学生表必须包含当前用户，否则 404

    学生权限边界 = 自己所在班级，所有学生功能接口（API 和页面）
    涉及班级数据时都必须先过这一层，统一 404 不暴露班级存在性。
    """
    clazz = Clazz.objects.filter(id=clazzId).first()
    if clazz is None:
        raise Http404('班级不存在')
    if not (user.is_superuser or clazz.students.filter(id=user.id).exists()):
        raise Http404('班级不存在')
    return clazz


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
