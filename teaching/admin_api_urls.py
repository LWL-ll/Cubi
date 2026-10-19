"""管理员 API 路由（挂在 /api/admin/ 前缀下，仅超级用户可以调用）"""
from django.urls import path

from .admin_api import ProblemImportView

urlpatterns = [
    path('problems/import/', ProblemImportView.as_view()),
]
