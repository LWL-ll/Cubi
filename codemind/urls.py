"""
URL configuration for codemind project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    # 挂载 users 应用的登录/登出/首页路由
    path('', include('users.urls')),
    # 挂载教务工作台页面路由（批量导入、课程管理、班级管理，开发预览用）
    path('office/', include('academics.urls')),
    # 挂载前后端分离 API 路由（课程、班级、导入）
    path('api/', include('academics.api_urls')),
]
