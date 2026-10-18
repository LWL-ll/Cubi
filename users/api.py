"""用户认证相关 API：登录 / 登出 / 当前用户与权限（前后端分离用）"""
from django.contrib.auth import authenticate, login, logout
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .permissions import GROUP_NAMES, PERM_CODES, getGroupKey


@api_view(['POST'])
@permission_classes([AllowAny])
def apiLogin(request):
    """登录接口：POST {username, password}

    成功建立会话并返回用户基本信息；失败返回 400。
    登录失败由 django-axes 自动记录，连续失败触发锁定。
    """
    username = request.data.get('username', '').strip()
    password = request.data.get('password', '')
    if not username or not password:
        return Response({'error': '请输入账号和密码'}, status=400)
    user = authenticate(request, username=username, password=password)
    if user is None:
        return Response({'error': '账号或密码错误'}, status=400)
    login(request, user)
    groupKey = getGroupKey(user)
    return Response({
        'username': user.username,
        'name': user.name,
        'groupKey': groupKey,
        'groupName': GROUP_NAMES.get(groupKey, '超级用户'),
    })


@api_view(['POST'])
def apiLogout(request):
    """登出接口：销毁当前会话"""
    logout(request)
    return Response({'message': '已退出登录'})


@api_view(['GET'])
def apiMe(request):
    """当前用户信息 + 拥有的全部权限码

    这是用户权限划分的核心接口：前端登录后调它拿到 permCodes，
    据此控制界面显示哪些功能入口（前端隐藏只影响体验，后端校验才是防线）
    """
    user = request.user
    groupKey = getGroupKey(user)
    # 一次取出全部权限再比对，避免逐个 hasPerm 触发多次权限查询
    # get_all_permissions 返回形如 'users.class_manage' 的字符串集合（超级用户返回全部）
    permSet = user.get_all_permissions()
    permCodes = [code for code, codename in PERM_CODES.items()
                 if f'users.{codename}' in permSet]
    return Response({
        'username': user.username,
        'name': user.name,
        'groupKey': groupKey,
        'groupName': GROUP_NAMES.get(groupKey, '超级用户'),
        'permCodes': permCodes,
    })
