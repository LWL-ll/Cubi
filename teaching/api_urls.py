"""教师工作台 API 路由（挂在 /api/teacher/ 前缀下）"""
from django.urls import path
from . import api
from . import communication_api as communication
from .shared_material_api import TeacherPublishCourseMaterialView

from .api import (PublicProblemCreateView, TeacherHomeworkDetailView,
                  TeacherHomeworkListCreateView, TeacherProblemDetailView,
                  TeacherProblemListCreateView, TeacherTeachingClassDetailView,
                  TeacherTeachingClassListView)

urlpatterns = [
    path('teaching-classes/<int:teachingClassId>/materials/from-course/', TeacherPublishCourseMaterialView.as_view()),
    path('teaching-classes/<int:teachingClassId>/notices/', communication.TeacherNoticeListCreateView.as_view()),
    path('notices/<int:noticeId>/', communication.TeacherNoticeDetailView.as_view()),
    path('notices/<int:noticeId>/readers/', communication.TeacherNoticeReadersView.as_view()),
    path('homeworks/<int:homeworkId>/reminders/', communication.TeacherHomeworkRemindersView.as_view()),
    path('homeworks/<int:homeworkId>/answers/', communication.TeacherHomeworkAnswersView.as_view()),
    path('homeworks/<int:homeworkId>/answers/publish/', communication.TeacherHomeworkAnswerPublishView.as_view()),
    path('homeworks/<int:homeworkId>/answers/<int:problemId>/', communication.TeacherHomeworkAnswerDetailView.as_view()),
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
