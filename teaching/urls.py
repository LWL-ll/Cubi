"""教师工作台页面路由（挂在 /teacher/ 前缀下，开发预览用）"""
from django.urls import path

from . import views

urlpatterns = [
    # 我的教学班级列表
    path('', views.renderTeacherClazzList, name='teacher_clazz_list'),
    # 班级详情（Tab：通知/学情分析/班级资料/作业/批改与AI）
    path('clazzes/<int:clazzId>/', views.renderTeacherClazzDetail, name='teacher_clazz_detail'),
    # 教师个人题库（占位，后续轮次实现）
    path('question-bank/', views.renderTeacherQuestionBank, name='teacher_question_bank'),
]
