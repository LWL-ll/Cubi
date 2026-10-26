from django.urls import path

from . import api, views

urlpatterns = [
    # 根路径直接进入登录页
    path('', views.renderLoginPage, name='index'),
    # 登录
    path('login/', views.renderLoginPage, name='login'),
    # 登出（仅接受 POST）
    path('logout/', views.logoutView, name='logout'),
    # 修改密码页（首次登录强制改密的页面端闭环）
    path('change-password/', views.renderChangePasswordPage,
         name='change_password_page'),
    # 登录后首页（权限总览，各角色功能入口占位）
    path('home/', views.renderHomePage, name='home'),
    # ---- 前后端分离 API ----
    path('api/auth/login/', api.apiLogin, name='api_login'),
    path('api/auth/logout/', api.apiLogout, name='api_logout'),
    path('api/auth/me/', api.apiMe, name='api_me'),
    path('api/auth/profile/', api.ProfileView.as_view(), name='api_profile'),
    path('api/auth/change-password/', api.apiChangePassword,
         name='api_change_password'),
]
