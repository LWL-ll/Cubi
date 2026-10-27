"""教务业务 API 路由（挂在 /api/ 前缀下）"""
from django.urls import path
from .student_api import (StudentDetailView, StudentLeaveView, StudentListView,
                          StudentMembershipListView, StudentTransferView)

from .api import (AdminClassDetailView, AdminClassListView,
                  CourseCollegeView, CourseDetailView, CourseListView,
                  DepartmentListView, ImportView, TeachingClassDetailView,
                  TeachingClassListView)
from .base_api import (OfficeDepartmentListView, OfficeDepartmentDetailView,
                       OfficeMajorListView, OfficeMajorDetailView,
                       OfficeCourseListView, OfficeCourseDetailView,
                       OfficeAdminClassListView, OfficeAdminClassDetailView)
from .student_api import StudentAcademicView
from .term_api import TermListView, TermDetailView, TermActionView, AuditListView
from users.account_api import AccountListView, AccountDetailView, AccountPasswordResetView

urlpatterns = [
    path('office/departments/', OfficeDepartmentListView.as_view()),
    path('office/departments/<int:objectId>/', OfficeDepartmentDetailView.as_view()),
    path('office/majors/', OfficeMajorListView.as_view()),
    path('office/majors/<int:objectId>/', OfficeMajorDetailView.as_view()),
    path('office/courses/', OfficeCourseListView.as_view()),
    path('office/courses/<int:objectId>/', OfficeCourseDetailView.as_view()),
    path('office/admin-classes/', OfficeAdminClassListView.as_view()),
    path('office/admin-classes/<int:objectId>/', OfficeAdminClassDetailView.as_view()),
    path('office/accounts/', AccountListView.as_view()),
    path('office/accounts/<int:accountId>/', AccountDetailView.as_view()),
    path('office/accounts/<int:accountId>/reset-password/', AccountPasswordResetView.as_view()),
    path('office/terms/', TermListView.as_view()),
    path('office/terms/<int:termId>/', TermDetailView.as_view()),
    path('office/terms/<int:termId>/current/', TermActionView.as_view(), {'action': 'current'}),
    path('office/terms/<int:termId>/close/', TermActionView.as_view(), {'action': 'close'}),
    path('office/terms/<int:termId>/archive/', TermActionView.as_view(), {'action': 'archive'}),
    path('office/audit-events/', AuditListView.as_view()),
    path('students/<int:studentId>/academic/', StudentAcademicView.as_view()),
    path('students/', StudentListView.as_view()),
    path('students/<int:studentId>/', StudentDetailView.as_view()),
    path('students/<int:studentId>/memberships/', StudentMembershipListView.as_view()),
    path('students/<int:studentId>/transfer/', StudentTransferView.as_view()),
    path('students/<int:studentId>/leave/', StudentLeaveView.as_view()),
    # 学院
    path('departments/', DepartmentListView.as_view()),
    # 课程
    path('courses/', CourseListView.as_view()),
    path('courses/<int:courseId>/', CourseDetailView.as_view()),
    # 课程×学院（组长、教师池、行政班设置）
    path('courses/<int:courseId>/departments/<int:departmentId>/',
         CourseCollegeView.as_view()),
    # 行政班
    path('admin-classes/', AdminClassListView.as_view()),
    path('admin-classes/<int:adminClassId>/', AdminClassDetailView.as_view()),
    # 教学班（组班、教师分配、解散）
    path('teaching-classes/', TeachingClassListView.as_view()),
    path('teaching-classes/<int:teachingClassId>/', TeachingClassDetailView.as_view()),
    # 批量导入（xlsx）
    path('import/', ImportView.as_view()),
]
