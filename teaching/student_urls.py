"""学生工作台页面路由（挂在 /student/ 前缀下，开发预览用）"""
from django.urls import path

from . import views

urlpatterns = [
    # 根路径重定向到第一个有权限的菜单
    path('', views.renderStudentHome, name='student_home'),
    # 我的作业（homework:view）
    path('homeworks/', views.renderStudentMenuPage, {'key': 'homeworks'},
         name='student_homeworks'),
    # 作业详情 + 提交（homework:view）
    path('homeworks/<int:homeworkId>/', views.renderStudentHomeworkDetail,
         name='student_homework_detail'),
    # 班级通知（notice:view）
    path('notices/', views.renderStudentMenuPage, {'key': 'notices'},
         name='student_notices'),
    # 班级资料（material:view）
    path('materials/', views.renderStudentMenuPage, {'key': 'materials'},
         name='student_materials'),
    # 学生画像（analysis:view_self）
    path('analysis/', views.renderStudentMenuPage, {'key': 'analysis'},
         name='student_analysis'),
    # 个人资料（profile:edit）
    path('profile/', views.renderStudentMenuPage, {'key': 'profile'},
         name='student_profile'),
]
