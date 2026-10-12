from django.urls import path

from . import views

urlpatterns = [
    # 批量导入（课程/教师/班级学生）
    path('import/', views.renderImportPage, name='office_import'),
    # 课程管理（课程 -> 学院 -> 班级 三层结构）
    path('courses/', views.renderCourseList, name='office_course_list'),
    path('courses/<int:courseId>/', views.renderCourseDetail, name='office_course_detail'),
    # 课程×学院 设置页（组长、教师池、班级）
    path('courses/<int:courseId>/colleges/<int:collegeId>/',
         views.renderCourseCollege, name='office_course_college'),
    # 班级详情（必须从 课程 -> 学院 -> 班级 进入，路径自带课程上下文）
    path('courses/<int:courseId>/colleges/<int:collegeId>/clazzes/<int:clazzId>/',
         views.renderClassDetail, name='office_class_detail'),
]
