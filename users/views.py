from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .permissions import GROUP_NAMES, getGroupKey


def renderLoginPage(request):
    """登录视图：GET 渲染页面，POST 认证账号密码

    登录失败由 django-axes 自动记录，连续失败触发账号锁定
    """
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        # 输入为空直接提示，避免无意义的认证调用
        if not username or not password:
            return render(request, 'users/login.html', {'error': '请输入账号和密码'})
        # authenticate 校验账号密码（axes 后端同时记录失败次数）
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect('home')
        return render(request, 'users/login.html', {'error': '账号或密码错误'})
    return render(request, 'users/login.html')


def logoutView(request):
    """登出视图：仅接受 POST，防止被恶意链接/CSRF 强制登出"""
    if request.method == 'POST':
        logout(request)
        return redirect('login')
    return redirect('home')


@login_required
def renderHomePage(request):
    """登录后首页：显示当前用户信息、所属分组和拥有的权限点（权限总览）"""
    user = request.user
    groupKey = getGroupKey(user)
    if groupKey == 'superuser':
        groupName = '超级用户'
    else:
        groupName = GROUP_NAMES.get(groupKey, '未分组')

    # 收集当前用户拥有的权限中文名（超级用户自动拥有全部）
    permNames = []
    for codename, permName in user._meta.permissions:
        if user.has_perm('users.' + codename):
            permNames.append(permName)

    return render(request, 'users/home.html', {
        'user': user,
        'groupName': groupName,
        'groupKey': groupKey,
        'permNames': permNames,
    })
