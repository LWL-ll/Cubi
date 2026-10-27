from django.urls import path

from . import api


urlpatterns = [
    path('course-colleges/', api.CourseCollegeListView.as_view()),
    path('course-colleges/<int:ccId>/knowledge/', api.KnowledgeListView.as_view()),
    path('course-colleges/<int:ccId>/knowledge/import/', api.KnowledgeImportView.as_view()),
    path('course-colleges/<int:ccId>/documents/', api.DocumentCreateView.as_view()),
    path('knowledge/<int:objId>/', api.KnowledgeDetailView.as_view()),
    path('knowledge/<int:objId>/reparse/', api.DocumentReparseView.as_view()),
    path('knowledge/<int:objId>/download/', api.KnowledgeDownloadView.as_view()),
    path('course-colleges/<int:ccId>/materials/', api.MaterialListView.as_view()),
    path('materials/<int:objId>/', api.MaterialDetailView.as_view()),
    path('materials/<int:objId>/download/', api.MaterialDownloadView.as_view()),
    path('course-colleges/<int:ccId>/chapters/', api.ChapterListView.as_view()),
    path('course-colleges/<int:ccId>/chapter-order/', api.ChapterOrderView.as_view()),
    path('chapters/<int:objId>/', api.ChapterDetailView.as_view()),
    path('teacher/course-colleges/', api.TeacherCourseCollegeListView.as_view()),
    path('teacher/course-colleges/<int:ccId>/knowledge/', api.TeacherKnowledgeListView.as_view()),
    path('teacher/knowledge/<int:objId>/', api.TeacherKnowledgeDetailView.as_view()),
    path('teacher/knowledge/<int:objId>/download/', api.TeacherKnowledgeDownloadView.as_view()),
    path('teacher/course-colleges/<int:ccId>/materials/', api.TeacherMaterialListView.as_view()),
    path('teacher/materials/<int:objId>/', api.TeacherMaterialDetailView.as_view()),
    path('teacher/materials/<int:objId>/download/', api.TeacherMaterialDownloadView.as_view()),
    path('teacher/course-colleges/<int:ccId>/chapters/', api.TeacherChapterListView.as_view()),
    path('teacher/chapters/<int:objId>/', api.TeacherChapterDetailView.as_view()),
]
