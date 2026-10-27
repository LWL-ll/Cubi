"""用户认证相关 API：登录 / 登出 / 当前用户与权限 / 修改密码（前后端分离用）"""
from collections.abc import Mapping
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.middleware.csrf import get_token
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .permissions import GROUP_NAMES, PERM_CODES, PermRequired, getGroupKey
from .profile_service import profileData, updateProfile


def authFields(payload, names):
    if not isinstance(payload, Mapping) or set(payload) - set(names):
        raise ValueError('请求体必须是对象，且只能包含允许的字段')
    values = []
    for name in names:
        value = payload.get(name)
        limit = 150 if name == 'username' else 1024
        if not isinstance(value, str) or not value or len(value) > limit or '\x00' in value:
            raise ValueError(f'{name} 须为 1～{limit} 字文本')
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            raise ValueError(f'{name} 含无效字符') from None
        if name == 'username':
            value = value.strip()
            if not value:
                raise ValueError('username 不能为空')
        values.append(value)
    return values


@never_cache
@api_view(['GET'])
@permission_classes([AllowAny])
def apiCsrf(request):
    return Response({'csrfToken': get_token(request)})


class ProfileView(APIView):
    permission_classes = [PermRequired]
    permCode = 'profile:edit'

    def get(self, request):
        return Response(profileData(request.user))

    def patch(self, request):
        return Response(updateProfile(request.user, request.data))


@sensitive_post_parameters('password')
@api_view(['POST'])
@permission_classes([AllowAny])
def apiLogin(request):
    """登录接口：POST {username, password}

    成功建立会话并返回用户基本信息（含 mustChangePassword 标志，
    前端据此跳转改密页；后端中间件会强制兜底拦截）；失败返回 400。
    登录失败由 django-axes 自动记录，连续失败触发锁定。
    """
    # 会话认证默认只对已登录用户校验 CSRF；登录入口还必须保护匿名请求。
    SessionAuthentication().enforce_csrf(request)
    try:
        username, password = authFields(request.data, ('username', 'password'))
    except ValueError as exc:
        return Response({'error': str(exc)}, status=400)
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
        'mustChangePassword': user.must_change_password,
        'csrfToken': get_token(request),
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
    据此控制界面显示哪些功能入口（前端隐藏只影响体验，后端校验才是防线）。
    mustChangePassword 供前端判断是否强制跳改密页。
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
        'mustChangePassword': user.must_change_password,
    })


def changeUserPassword(request, user, oldPassword, newPassword):
    """改密核心逻辑（API 与页面视图共用）：校验旧密码 + 新密码强度，
    成功后更新密码、解除改密标记并保持当前会话

    返回 (True, '') 或 (False, 错误消息)。强度校验复用 Django
    全套校验器（长度/常见密码/纯数字/与用户信息相似）。
    """
    if not user.check_password(oldPassword):
        return False, '旧密码不正确'
    if newPassword == oldPassword:
        return False, '新密码不能与旧密码相同'
    try:
        validate_password(newPassword, user)
    except ValidationError as err:
        # Django 校验器返回中文消息列表，拼接展示
        return False, '；'.join(err.messages)
    user.set_password(newPassword)
    user.must_change_password = False
    user.save()
    # 改密后保持当前会话有效（会话哈希随密码更新，不把用户踢下线）
    update_session_auth_hash(request, user)
    return True, ''


@sensitive_post_parameters('oldPassword', 'newPassword')
@api_view(['POST'])
def apiChangePassword(request):
    """修改密码接口：POST {oldPassword, newPassword}

    首次登录强制改密的闭环：成功后 must_change_password 置 False，
    ForcePasswordChangeMiddleware 随之放行；会话保持不断。
    """
    try:
        oldPassword, newPassword = authFields(request.data, ('oldPassword', 'newPassword'))
    except ValueError as exc:
        return Response({'error': str(exc)}, status=400)
    ok, errMsg = changeUserPassword(
        request, request.user, oldPassword, newPassword)
    if not ok:
        return Response({'error': errMsg}, status=400)
    return Response({'message': '密码修改成功'})
