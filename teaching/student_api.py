"""学生专用字段与本人聚合；不复用包含全班记录的教师响应。"""
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import IsStudentGroup, PermRequired
from .serializers import StudentNoticeSerializer, TeachingMaterialSerializer
from .student_scope import StudentScope
from .student_service import (classData, contextData, dashboard, homeworkData, homeworkQuery,
    learningAnalysis, ownGrades, personalRows, queryFields, resourceQuery, teachingClasses)


def paginated(request, query, serialize, **extra):
    paginator = PageNumberPagination()
    paginator.page_size = 20
    page = list(paginator.paginate_queryset(query, request))
    response = paginator.get_paginated_response(serialize(page))
    response.data.update(extra)
    return response


class StudentAPIView(APIView):
    permission_classes = [IsStudentGroup, PermRequired]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not request.user.is_active:
            raise PermissionDenied('账号已停用')

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        return response


class StudentContextView(StudentAPIView):
    permCode = 'profile:edit'

    def get(self, request):
        queryFields(request.query_params, set())
        return Response(contextData(StudentScope(request.user)))


class StudentTeachingClassesView(StudentAPIView):
    permCode = 'homework:view'

    def get(self, request):
        scope = StudentScope(request.user)
        return paginated(request, teachingClasses(scope, request.query_params),
                         lambda page: [classData(scope, tc) for tc in page])


class StudentHomeworksView(StudentAPIView):
    permCode = 'homework:view'

    def get(self, request):
        scope, now = StudentScope(request.user), timezone.now()

        def serialize(page):
            rows = personalRows(scope, page)
            return [homeworkData(scope, item, rows[item.pk], now) for item in page]

        return paginated(request, homeworkQuery(scope, request.query_params, now), serialize, asOf=now)


class StudentOwnGradesView(StudentAPIView):
    permCode = 'homework:view'

    def get(self, request, homeworkId):
        queryFields(request.query_params, set())
        return Response(ownGrades(StudentScope(request.user), homeworkId, timezone.now()))


class StudentNoticesView(StudentAPIView):
    permCode = 'notice:view'

    def get(self, request):
        scope = StudentScope(request.user)
        query, unread = resourceQuery(scope, request.query_params, 'notices')
        return paginated(request, query, lambda page: StudentNoticeSerializer(page, many=True).data,
                         unreadCount=unread)


class StudentMaterialsView(StudentAPIView):
    permCode = 'material:view'

    def get(self, request):
        query, _ = resourceQuery(StudentScope(request.user), request.query_params, 'materials')
        return paginated(request, query, lambda page: TeachingMaterialSerializer(page, many=True).data)


class StudentDashboardView(StudentAPIView):
    servicePermissionCheck = 'student_service.dashboard: each card checks its original feature permission'

    def get(self, request):
        queryFields(request.query_params, set())
        return Response(dashboard(StudentScope(request.user), timezone.now()))


class StudentAnalysisView(StudentAPIView):
    permCode = 'analysis:view_self'

    def get(self, request):
        return Response(learningAnalysis(StudentScope(request.user), request.query_params, timezone.now()))
