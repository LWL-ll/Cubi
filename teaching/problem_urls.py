"""学生题面 API 路由（挂在 /api/ 前缀下）

端点（docs/OJ对接设计.md 12.2）：
- GET /api/problems/<id>/：学生题面详情（homework:view）
  不按 is_public 过滤——作业里引用的教师个人题学生要能读。
"""
from django.urls import path

from .api import StudentProblemDetailView

urlpatterns = [
    path('problems/<int:problemId>/', StudentProblemDetailView.as_view()),
]
