"""教师/学生工作台页面视图（开发预览用，主线是 API）

安全约定：
- 页面入口 requireTeacherGroup/requireStudentGroup：仅对应分组（或超级用户）可进
- 班级详情 getTeacherClazz：非教学班级一律 404
- Tab/菜单按权限码显示，且访问时再次校验权限（服务端强制）
"""
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render

from academics.models import Clazz, Course
from users.permissions import hasPerm

from .helpers import getTeacherClazz, requireStudentGroup, requireTeacherGroup

# 班级详情 Tab 定义：key、显示名、所需权限码（拥有任一即可见）、图标
CLAZZ_TABS = [
    ('notice', '通知', ('notice:manage',), '📢'),
    ('analysis', '学情分析', ('analysis:view',), '📊'),
    ('material', '班级资料', ('material:class_manage',), '📁'),
    ('homework', '作业', ('homework:publish',), '📝'),
    ('review', '批改与AI', ('homework:comment', 'homework:answer', 'homework:ai'), '✍️'),
]


def getVisibleTabs(user):
    """按用户权限过滤班级 Tab（无对应权限的 Tab 不显示）"""
    return [(key, name, icon)
            for key, name, permCodes, icon in CLAZZ_TABS
            if any(hasPerm(user, code) for code in permCodes)]


def getPermCodesOfTab(key):
    """取某个 Tab 的权限码元组；key 无效返回 None"""
    for tabKey, _, permCodes, _ in CLAZZ_TABS:
        if tabKey == key:
            return permCodes
    return None


@requireTeacherGroup
def renderTeacherClazzList(request):
    """我的教学班级列表：当前教师教的全部班级（跨课程跨学院）"""
    clazzes = Clazz.objects.filter(teachers=request.user).distinct() \
        .select_related('college') \
        .prefetch_related('course_colleges__course', 'students') \
        .order_by('college__name', 'name')
    return render(request, 'teaching/clazz_list.html', {'clazzes': clazzes})


@requireTeacherGroup
def renderTeacherClazzDetail(request, clazzId):
    """教师视角班级详情：顶部班级信息 + Tab 骨架（功能按轮次填充）

    tab 参数指定当前 Tab；无参数默认第一个有权限的 Tab。
    """
    clazz = getTeacherClazz(request.user, clazzId)
    visibleTabs = getVisibleTabs(request.user)
    if not visibleTabs:
        # 教师无任何班级内权限（理论上不会发生：教师组必有权限）
        return HttpResponseForbidden('403：当前账号没有任何班级内功能权限')

    tabKey = request.GET.get('tab', visibleTabs[0][0])
    # Tab 必须有效且当前用户有权限（服务端强制，不依赖前端）
    permCodes = getPermCodesOfTab(tabKey)
    if permCodes is None or not any(hasPerm(request.user, code) for code in permCodes):
        return HttpResponseForbidden('403：没有该功能的权限')

    # 班级所属课程（经课程×学院），页面顶部徽章展示用
    courses = Course.objects.filter(course_colleges__clazzes=clazz).distinct()

    return render(request, 'teaching/clazz_detail.html', {
        'clazz': clazz,
        'courses': courses,
        'visibleTabs': visibleTabs,
        'currentTab': tabKey,
    })


@requireTeacherGroup
def renderTeacherQuestionBank(request):
    """教师个人题库（本轮占位，题库功能后续轮次实现）"""
    return render(request, 'teaching/question_bank.html')


# ================= 学生工作台（框架轮：页面骨架 + 权限校验，功能按轮次填充） =================

# 学生侧边栏菜单定义：key、显示名、所需权限码、图标
# 注意：key 同时也是页面模板名（teaching/student_<key>.html），保持一致
# 班级通知/班级资料为教师与课程组下发、学生只读查看（与教师端共用同一批数据）
# 注意：notice:view / material:view 权限码待后端权限轮注册（本轮前端先行，
# 注册前这两个菜单对学生不可见，见 users/models.py Meta.permissions）
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
