"""教师工作台 API 路由（挂在 /api/teacher/ 前缀下）"""
from django.urls import path

from .api import TeacherTeachingClassDetailView, TeacherTeachingClassListView

urlpatterns = [
    # 我的教学班级
    path('teaching-classes/', TeacherTeachingClassListView.as_view()),
    path('teaching-classes/<int:teachingClassId>/',
         TeacherTeachingClassDetailView.as_view()),
]
