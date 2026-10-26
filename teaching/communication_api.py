"""教师通知、作业提醒与参考答案接口；前端暂不接入。"""
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import PermRequired
from .api import SubmissionPagination
from .answer_service import (deleteAnswer, listStudentAnswers, listTeacherAnswers,
                             publishAnswers, saveAnswer)
from .communication_service import (createNotice, deleteNotice, getNotice,
    getStudentReminder, listClassNotices, listStudentReminders, markNoticeRead,
    markReminderRead, noticeReadership, previewReminderRecipients,
    sendHomeworkReminders, updateNotice, validateEmptyPayload)
from .helpers import getStudentTeachingClass, getTeacherTeachingClass
from .homework_service import getManageableHomework
from .serializers import (ClassNoticeSerializer, HomeworkReminderSerializer,
                          StudentNoticeSerializer)


class TeacherNoticeListCreateView(APIView):
    permission_classes = [PermRequired]
    permCode = 'notice:manage'

    def get(self, request, teachingClassId):
        teachingClass = getTeacherTeachingClass(request.user, teachingClassId)
        paginator = SubmissionPagination()
        page = paginator.paginate_queryset(listClassNotices(teachingClass), request)
        return paginator.get_paginated_response(ClassNoticeSerializer(page, many=True).data)

    def post(self, request, teachingClassId):
        teachingClass = getTeacherTeachingClass(request.user, teachingClassId)
        notice = createNotice(request.user, teachingClass, request.data)
        return Response(ClassNoticeSerializer(notice).data, status=201)


class TeacherNoticeDetailView(APIView):
    permission_classes = [PermRequired]
    permCode = 'notice:manage'

    def get(self, request, noticeId):
        return Response(ClassNoticeSerializer(getNotice(request.user, noticeId)).data)

    def patch(self, request, noticeId):
        notice = getNotice(request.user, noticeId)
        return Response(ClassNoticeSerializer(updateNotice(request.user, notice, request.data)).data)

    def delete(self, request, noticeId):
        deleteNotice(getNotice(request.user, noticeId))
        return Response(status=204)


class TeacherNoticeReadersView(APIView):
    permission_classes = [PermRequired]
    permCode = 'notice:manage'

    def get(self, request, noticeId):
        data = noticeReadership(getNotice(request.user, noticeId))
        paginator = SubmissionPagination()
        page = paginator.paginate_queryset(data['students'], request)
        response = paginator.get_paginated_response(page)
        response.data.update(noticeId=noticeId, summary=data['summary'])
        return response


class StudentNoticeListView(APIView):
    permission_classes = [PermRequired]
    permCode = 'notice:view'

    def get(self, request, teachingClassId):
        teachingClass = getStudentTeachingClass(request.user, teachingClassId)
        paginator = SubmissionPagination()
        page = paginator.paginate_queryset(listClassNotices(teachingClass, request.user), request)
        return paginator.get_paginated_response(StudentNoticeSerializer(page, many=True).data)


class StudentNoticeDetailView(APIView):
    permission_classes = [PermRequired]
    permCode = 'notice:view'

    def get(self, request, noticeId):
        return Response(StudentNoticeSerializer(
            getNotice(request.user, noticeId, student=True)).data)


class StudentNoticeReadView(APIView):
    permission_classes = [PermRequired]
    permCode = 'notice:view'

    def post(self, request, noticeId):
        notice = getNotice(request.user, noticeId, student=True)
        validateEmptyPayload(request.data)
        receipt = markNoticeRead(request.user, notice)
        return Response({'readAt': receipt.read_at})


class TeacherHomeworkRemindersView(APIView):
    permission_classes = [PermRequired]
    permCode = 'homework:remind'

    def get(self, request, homeworkId):
        homework = getManageableHomework(request.user, homeworkId)
        return Response(previewReminderRecipients(homework))

    def post(self, request, homeworkId):
        homework = getManageableHomework(request.user, homeworkId)
        return Response(sendHomeworkReminders(request.user, homework, request.data))


class StudentReminderListView(APIView):
    permission_classes = [PermRequired]
    permCode = 'notice:view'

    def get(self, request):
        paginator = SubmissionPagination()
        page = paginator.paginate_queryset(listStudentReminders(request.user), request)
        return paginator.get_paginated_response(HomeworkReminderSerializer(page, many=True).data)


class StudentReminderReadView(APIView):
    permission_classes = [PermRequired]
    permCode = 'notice:view'

    def post(self, request, reminderId):
        reminder = getStudentReminder(request.user, reminderId)
        validateEmptyPayload(request.data)
        return Response(HomeworkReminderSerializer(markReminderRead(request.user, reminder)).data)


class TeacherHomeworkAnswersView(APIView):
    permission_classes = [PermRequired]
    permCode = 'homework:answer'

    def get(self, request, homeworkId):
        return Response(listTeacherAnswers(request.user, homeworkId))


class TeacherHomeworkAnswerDetailView(APIView):
    permission_classes = [PermRequired]
    permCode = 'homework:answer'

    def put(self, request, homeworkId, problemId):
        return Response(saveAnswer(request.user, homeworkId, problemId, request.data))

    def delete(self, request, homeworkId, problemId):
        deleteAnswer(request.user, homeworkId, problemId)
        return Response(status=204)


class TeacherHomeworkAnswerPublishView(APIView):
    permission_classes = [PermRequired]
    permCode = 'homework:answer'

    def post(self, request, homeworkId):
        return Response(publishAnswers(request.user, homeworkId, request.data))


class StudentHomeworkAnswersView(APIView):
    permission_classes = [PermRequired]
    permCode = 'homework:view'

    def get(self, request, homeworkId):
        return Response(listStudentAnswers(request.user, homeworkId))
