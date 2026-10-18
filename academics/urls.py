from django.urls import path

from . import views

urlpatterns = [
    # 批量导入（课程/教师/班级学生）
    path('import/', views.renderImportPage, name='office_import'),
    # 课程管理（课程 -> 学院 -> 行政班 -> 教学班 四层结构）
    path('courses/', views.renderCourseList, name='office_course_list'),
    path('courses/<int:courseId>/', views.renderCourseDetail, name='office_course_detail'),
    # 课程×学院 设置页（组长、教师池、行政班、教学班组班）
    path('courses/<int:courseId>/departments/<int:departmentId>/',
         views.renderCourseCollege, name='office_course_college'),
    # 行政班详情（必须从 课程 -> 学院 进入，路径自带课程上下文）
    path('courses/<int:courseId>/departments/<int:departmentId>/classes/<int:adminClassId>/',
         views.renderClassDetail, name='office_class_detail'),
    # 教学班详情（教师分配、组成班级展示）
    path('courses/<int:courseId>/departments/<int:departmentId>/teaching-classes/<int:teachingClassId>/',
         views.renderTeachingClassDetail, name='office_teaching_class_detail'),
]
