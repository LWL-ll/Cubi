from django.urls import path

from .import_api import (
    OfficeImportDetailView, OfficeImportErrorDownloadView, OfficeImportErrorsView,
    OfficeImportListView, OfficeImportRetryView,
)

urlpatterns = [
    path('import-jobs/', OfficeImportListView.as_view(), name='office_import_jobs'),
    path('import-jobs/<int:jobId>/', OfficeImportDetailView.as_view(), name='office_import_job'),
    path('import-jobs/<int:jobId>/retry/', OfficeImportRetryView.as_view(), name='office_import_retry'),
    path('import-jobs/<int:jobId>/errors/', OfficeImportErrorsView.as_view(), name='office_import_errors'),
    path('import-jobs/<int:jobId>/errors/download/', OfficeImportErrorDownloadView.as_view(),
         name='office_import_errors_download'),
]
