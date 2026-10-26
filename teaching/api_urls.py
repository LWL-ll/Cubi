"""教师工作台 API 路由（挂在 /api/teacher/ 前缀下）"""
from django.urls import path
from . import api

from .api import (PublicProblemCreateView, TeacherHomeworkDetailView,
                  TeacherHomeworkListCreateView, TeacherProblemDetailView,
                  TeacherProblemListCreateView, TeacherTeachingClassDetailView,
                  TeacherTeachingClassListView)

urlpatterns = [
    path('problems/import/', api.TeacherProblemImportView.as_view()),
    path('problems/imports/', api.TeacherProblemImportHistoryView.as_view()),
    path('teaching-classes/<int:teachingClassId>/dashboard/', api.TeacherClassDashboardView.as_view()),
    path('teaching-classes/<int:teachingClassId>/students/', api.TeacherClassStudentsView.as_view()),
    path('teaching-classes/<int:teachingClassId>/students/<int:studentId>/', api.TeacherStudentDetailView.as_view()),
    path('teaching-classes/<int:teachingClassId>/submissions/', api.TeacherSubmissionsView.as_view()),
    path('submissions/<int:submissionId>/', api.TeacherSubmissionDetailView.as_view()),
    path('submissions/<int:submissionId>/review/', api.TeacherSubmissionReviewView.as_view()),
    path('homeworks/<int:homeworkId>/grades/', api.TeacherHomeworkGradesView.as_view()),
    path('homeworks/<int:homeworkId>/grades/export/', api.TeacherHomeworkExportView.as_view()),
    path('teaching-classes/<int:teachingClassId>/materials/', api.TeacherMaterialListCreateView.as_view()),
    path('materials/<int:materialId>/', api.TeacherMaterialDetailView.as_view()),
    path('materials/<int:materialId>/download/', api.TeacherMaterialDownloadView.as_view()),
    # 我的教学班级
    path('teaching-classes/', TeacherTeachingClassListView.as_view()),
    path('teaching-classes/<int:teachingClassId>/',
         TeacherTeachingClassDetailView.as_view()),
    # 教学班作业（组卷，12.4）
    path('teaching-classes/<int:teachingClassId>/homeworks/',
         TeacherHomeworkListCreateView.as_view()),
    path('homeworks/<int:homeworkId>/', TeacherHomeworkDetailView.as_view()),
    # 题库（个人题 + 公共题库，12.1）
    path('problems/', TeacherProblemListCreateView.as_view()),
    path('problems/<int:problemId>/', TeacherProblemDetailView.as_view()),
    # 公共题库录题（课程组专用，12.3）
    path('public-problems/', PublicProblemCreateView.as_view()),
]
