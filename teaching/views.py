"""教师/学生工作台页面视图（开发预览用，主线是 API）

安全约定：
- 页面入口 requireTeacherGroup/requireStudentGroup：仅对应分组（或超级用户）可进
- 教学班详情 getTeacherTeachingClass：非本人教学班一律 404
- Tab/菜单按权限码显示，且访问时再次校验权限（服务端强制）
"""
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render

from academics.models import ClassMembership, TeachingClass
from users.permissions import hasPerm

from .helpers import (getTeacherTeachingClass, requireStudentGroup,
                      requireTeacherGroup)

# 教学班详情 Tab 定义：key、显示名、所需权限码（拥有任一即可见）、图标
TEACHING_CLASS_TABS = [
    ('notice', '通知', ('notice:manage',), '📢'),
    ('analysis', '学情分析', ('analysis:view',), '📊'),
    ('material', '班级资料', ('material:class_manage',), '📁'),
    ('homework', '作业', ('homework:publish',), '📝'),
    ('review', '批改与AI', ('homework:comment', 'homework:answer', 'homework:ai'), '✍️'),
]


def getVisibleTabs(user):
    """按用户权限过滤教学班 Tab（无对应权限的 Tab 不显示）"""
    return [(key, name, icon)
            for key, name, permCodes, icon in TEACHING_CLASS_TABS
            if any(hasPerm(user, code) for code in permCodes)]


def getPermCodesOfTab(key):
    """取某个 Tab 的权限码元组；key 无效返回 None"""
    for tabKey, _, permCodes, _ in TEACHING_CLASS_TABS:
        if tabKey == key:
            return permCodes
    return None


def countTeachingClassStudents(teachingClass):
    """教学班当前学生数：组成行政班的在班成员聚合（页面视图用）"""
    return ClassMembership.objects.filter(
        admin_class__teaching_classes=teachingClass,
        left_at__isnull=True).count()


@requireTeacherGroup
def renderTeacherTeachingClassList(request):
    """我的教学班级列表：当前教师教的全部教学班（跨课程跨学院）"""
    teachingClasses = TeachingClass.objects.filter(
        teachers=request.user).distinct() \
        .select_related('course_college__course', 'course_college__department') \
        .order_by('course_college__course__name', 'name')
    # 学生数在视图层算好传给模板（3 跳推导，模板里不好写）
    items = [{
        'tc': tc,
        'studentCount': countTeachingClassStudents(tc),
    } for tc in teachingClasses]
    return render(request, 'teaching/teaching_class_list.html', {'items': items})


@requireTeacherGroup
def renderTeacherTeachingClassDetail(request, teachingClassId):
    """教师视角教学班详情：顶部教学班信息 + Tab 骨架（功能按轮次填充）

    tab 参数指定当前 Tab；无参数默认第一个有权限的 Tab。
    """
    teachingClass = getTeacherTeachingClass(request.user, teachingClassId)
    visibleTabs = getVisibleTabs(request.user)
    if not visibleTabs:
        # 教师无任何教学班内权限（理论上不会发生：教师组必有权限）
        return HttpResponseForbidden('403：当前账号没有任何班级内功能权限')

    tabKey = request.GET.get('tab', visibleTabs[0][0])
    # Tab 必须有效且当前用户有权限（服务端强制，不依赖前端）
    permCodes = getPermCodesOfTab(tabKey)
    if permCodes is None or not any(hasPerm(request.user, code) for code in permCodes):
        return HttpResponseForbidden('403：没有该功能的权限')

    return render(request, 'teaching/teaching_class_detail.html', {
        'teachingClass': teachingClass,
        'course': teachingClass.course_college.course,
        'department': teachingClass.course_college.department,
        'studentCount': countTeachingClassStudents(teachingClass),
        'visibleTabs': visibleTabs,
        'currentTab': tabKey,
    })


@requireTeacherGroup
def renderTeacherQuestionBank(request):
    """教师题库页：公共题库 + 本人个人题（M7 实装，数据走题库 API）

    公共题库自 2026-10-20 起由系统管理员独占维护，普通教师只读；
    模板按 canManagePublic 决定是否显示公共题的新建/编辑入口
    （服务端仍以权限为最终防线，前端只影响体验）。
    """
    return render(request, 'teaching/question_bank.html', {
        'canManagePublic': request.user.is_superuser,
    })


# ================= 学生工作台（框架轮：页面骨架 + 权限校验，功能按轮次填充） =================

# 学生侧边栏菜单定义：key、显示名、所需权限码、图标
# 注意：key 同时也是页面模板名（teaching/student_<key>.html），保持一致
# 班级通知/班级资料为教师与课程组下发、学生只读查看（与教师端共用同一批数据）；
# notice:view / material:view 权限码已在 users/models.py Meta.permissions 注册
STUDENT_MENUS = [
    ('homeworks', '我的作业', 'homework:view', '📝'),
    ('notices', '班级通知', 'notice:view', '📢'),
    ('materials', '班级资料', 'material:view', '📁'),
    ('analysis', '学生画像', 'analysis:view_self', '📊'),
    ('profile', '个人资料', 'profile:edit', '👤'),
]


def getVisibleStudentMenus(user):
    """按用户权限过滤学生菜单（无对应权限的菜单不显示）"""
    return [(key, name, icon)
            for key, name, permCode, icon in STUDENT_MENUS
            if hasPerm(user, permCode)]


@requireStudentGroup
def renderStudentHome(request):
    """学生工作台根路径：重定向到第一个有权限的菜单"""
    visibleMenus = getVisibleStudentMenus(request.user)
    if not visibleMenus:
        # 学生组必有 homework:view（理论上不会发生）
        return HttpResponseForbidden('403：当前账号没有任何学生功能权限')
    return redirect('student_' + visibleMenus[0][0])


@requireStudentGroup
def renderStudentMenuPage(request, key):
    """学生菜单骨架页：校验菜单权限后渲染占位页面（功能按轮次填充）

    key 必须合法且当前用户拥有对应权限码，否则 403（服务端强制，
    不依赖前端隐藏菜单）。模板名 = teaching/student_<key>.html。
    """
    for menuKey, _, permCode, _ in STUDENT_MENUS:
        if menuKey == key:
            break
    else:
        return HttpResponseForbidden('403：菜单不存在')
    if not hasPerm(request.user, permCode):
        return HttpResponseForbidden('403：没有该功能的权限')

    return render(request, 'teaching/student_' + key + '.html', {
        'visibleMenus': getVisibleStudentMenus(request.user),
        'currentKey': key,
    })


@requireStudentGroup
def renderStudentHomeworkDetail(request, homeworkId):
    """学生作业详情页：作业内容 + 提交表单 + 教师反馈区

    本轮仅页面骨架（数据由前端假数据渲染），后端就绪后数据改走
    API（homework:view），本视图只负责渲染页面壳。
    """
    return render(request, 'teaching/student_homework_detail.html', {
        'visibleMenus': getVisibleStudentMenus(request.user),
        'currentKey': 'homeworks',
        'homeworkId': homeworkId,
    })
