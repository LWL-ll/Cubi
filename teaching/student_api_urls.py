from django.urls import path
from . import student_api as api

urlpatterns = [
    path('context/', api.StudentContextView.as_view()),
    path('teaching-classes/', api.StudentTeachingClassesView.as_view()),
    path('homeworks/', api.StudentHomeworksView.as_view()),
    path('homeworks/<int:homeworkId>/grades/', api.StudentOwnGradesView.as_view()),
    path('notices/', api.StudentNoticesView.as_view()),
    path('materials/', api.StudentMaterialsView.as_view()),
    path('dashboard/', api.StudentDashboardView.as_view()),
    path('analysis/', api.StudentAnalysisView.as_view()),
]
