"""教务业务 API 路由（挂在 /api/ 前缀下）"""
from django.urls import path

from .api import (ClazzDetailView, ClazzListView, CollegeListView,
                  CourseCollegeView, CourseDetailView, CourseListView, ImportView)

urlpatterns = [
    # 学院
    path('colleges/', CollegeListView.as_view()),
    # 课程
    path('courses/', CourseListView.as_view()),
    path('courses/<int:courseId>/', CourseDetailView.as_view()),
    # 课程×学院（组长、教师池、班级设置）
    path('courses/<int:courseId>/colleges/<int:collegeId>/', CourseCollegeView.as_view()),
    # 班级
    path('classes/', ClazzListView.as_view()),
    path('classes/<int:clazzId>/', ClazzDetailView.as_view()),
    # 批量导入（xlsx）
    path('import/', ImportView.as_view()),
]
