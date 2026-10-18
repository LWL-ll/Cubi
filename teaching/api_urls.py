"""教师工作台 API 路由（挂在 /api/teacher/ 前缀下）"""
from django.urls import path

from .api import TeacherClazzDetailView, TeacherClazzListView

urlpatterns = [
    # 我的教学班级
    path('clazzes/', TeacherClazzListView.as_view()),
    path('clazzes/<int:clazzId>/', TeacherClazzDetailView.as_view()),
]
