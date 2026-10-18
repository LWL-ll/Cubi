"""教师工作台页面路由（挂在 /teacher/ 前缀下，开发预览用）"""
from django.urls import path

from . import views

urlpatterns = [
    # 我的教学班级列表
    path('', views.renderTeacherTeachingClassList, name='teacher_teaching_class_list'),
    # 教学班详情（Tab：通知/学情分析/班级资料/作业/批改与AI）
    path('teaching-classes/<int:teachingClassId>/',
         views.renderTeacherTeachingClassDetail,
         name='teacher_teaching_class_detail'),
    # 教师个人题库（占位，后续轮次实现）
    path('question-bank/', views.renderTeacherQuestionBank, name='teacher_question_bank'),
]
