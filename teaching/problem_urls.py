"""学生 OJ API 路由（挂在 /api/ 前缀下）

端点（docs/OJ对接设计.md 12.2 / 12.4）：
- GET /api/problems/<id>/：学生题面详情（homework:view）
  不按 is_public 过滤——作业里引用的教师个人题学生要能读。
- GET /api/homeworks/：我的作业列表（三态 + 我的提交概况）
- GET /api/homeworks/<id>/：作业详情（题目白名单 + 我的提交摘要；
  开始前 404 防提前看题）
"""
from django.urls import path

from .api import (StudentHomeworkDetailView, StudentHomeworkListView,
                  StudentProblemDetailView, StudentMaterialListView,
                  StudentMaterialDownloadView)

urlpatterns = [
    path('teaching-classes/<int:teachingClassId>/materials/', StudentMaterialListView.as_view()),
    path('materials/<int:materialId>/download/', StudentMaterialDownloadView.as_view()),
    path('problems/<int:problemId>/', StudentProblemDetailView.as_view()),
    path('homeworks/', StudentHomeworkListView.as_view()),
    path('homeworks/<int:homeworkId>/', StudentHomeworkDetailView.as_view()),
]
