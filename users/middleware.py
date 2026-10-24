"""首次登录强制改密的全局兜底中间件

must_change_password=True 的账号除白名单路径外一律拦截：
- API 请求（/api/ 前缀）返回 403 JSON（错误信息指导前端调改密接口）
- 页面请求重定向到改密页

这是服务端强制防线：前端跳改密页只影响体验，本中间件保证
「不改密就什么都做不了」（问题清单 3.1 的兜底要求）。
"""
from django.http import JsonResponse
from django.shortcuts import redirect

# 白名单路径前缀：改密闭环所需的最小集合（登录/登出/查自身信息/改密页/
# 改密 API/静态资源）。后台也必须先完成统一改密流程。
WHITELIST_PREFIXES = [
    '/api/auth/login/',
    '/api/auth/logout/',
    '/api/auth/me/',
    '/api/auth/change-password/',
    '/login/',
    '/logout/',
    '/change-password/',
    '/static/',
]


class ForcePasswordChangeMiddleware:
    """拦截待改密账号的业务请求"""

    def __init__(self, getResponse):
        self.getResponse = getResponse

    def __call__(self, request):
        # 依赖 AuthenticationMiddleware 已解析 request.user
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated \
                and getattr(user, 'must_change_password', False):
            path = request.path
            if not any(path.startswith(prefix) for prefix in WHITELIST_PREFIXES):
                if path.startswith('/api/'):
                    return JsonResponse(
                        {'error': '首次登录须先修改密码：'
                                  'POST /api/auth/change-password/'},
                        status=403)
                return redirect('change_password_page')
        return self.getResponse(request)
