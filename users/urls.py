from django.urls import path

from . import api, views

urlpatterns = [
    # 根路径直接进入登录页
    path('', views.renderLoginPage, name='index'),
    # 登录
    path('login/', views.renderLoginPage, name='login'),
    # 登出（仅接受 POST）
    path('logout/', views.logoutView, name='logout'),
    # 登录后首页（权限总览，各角色功能入口占位）
    path('home/', views.renderHomePage, name='home'),
    # ---- 前后端分离 API ----
    path('api/auth/login/', api.apiLogin, name='api_login'),
    path('api/auth/logout/', api.apiLogout, name='api_logout'),
    path('api/auth/me/', api.apiMe, name='api_me'),
]
