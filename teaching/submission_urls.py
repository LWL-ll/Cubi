"""学生提交 API 路由（挂在 /api/ 前缀下）

端点（docs/OJ对接设计.md 12.2）：
- POST /api/submissions/：提交受理（同步校验 + 快照 + 投递）
- GET  /api/submissions/：我的提交列表（分页 20，按题/作业/状态过滤）
- GET  /api/submissions/<id>/：提交详情（对象级 404）
"""
from django.urls import path

from .api import SubmissionDetailView, SubmissionListCreateView

urlpatterns = [
    path('submissions/', SubmissionListCreateView.as_view()),
    path('submissions/<int:submissionId>/', SubmissionDetailView.as_view()),
]
