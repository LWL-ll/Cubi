"""课程组管理与任课教师只读 API；没有学生入口。"""
from django.db.models import Q
from django.http import FileResponse, Http404
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from academics.models import CourseCollege
from users.permissions import IsTeacherGroup, PermRequired

from .models import CourseChapter, CourseKnowledge, CourseMaterial
from .serializers import (ChapterInput, ChapterOrderInput, ChapterSerializer,
                          DocumentInput, KnowledgeInput, KnowledgeListSerializer, KnowledgeSerializer,
                          MaterialInput, MaterialSerializer, SafeText, UploadInput)
from .services import (createKnowledge, getCourseCollege, getManagedObject,
                       getTeacherCourseCollege, importKnowledge, readableKnowledge,
                       readableChapters, readableMaterials, removeResource, reorderChapters,
                       reparseDocument, saveChapter, saveDocument, saveMaterial,
                       teacherCourseColleges, updateKnowledge)
from .storage import openFile


def validated(serializerClass, data, *, partial=False):
    serializer = serializerClass(data=data, partial=partial)
    serializer.is_valid(raise_exception=True)
    return dict(serializer.validated_data)


def courseData(obj):
    return {'id': obj.pk, 'course': {'id': obj.course_id, 'code': obj.course.code,
                                    'name': obj.course.name},
            'department': {'id': obj.department_id, 'code': obj.department.code,
                           'name': obj.department.name}}


def paginated(request, queryset, serializerClass, *, search=True):
    if search and 'q' in request.query_params:
        query = SafeText(max_length=100, allow_blank=True).run_validation(request.query_params['q'])
        if query:
            queryFilter = Q(title__icontains=query)
            if queryset.model is CourseKnowledge:
                queryFilter |= Q(content__icontains=query)
            queryset = queryset.filter(queryFilter)
    paginator = PageNumberPagination()
    paginator.page_size = 20
    rows = paginator.paginate_queryset(queryset, request)
    return paginator.get_paginated_response(serializerClass(rows, many=True).data)


class ManagedAPIView(APIView):
    permission_classes = [PermRequired]


class CourseCollegeListView(ManagedAPIView):
    permCode = 'knowledge:manage'

    def get(self, request):
        queryset = CourseCollege.objects.select_related('course', 'department').order_by('id')
        if not request.user.is_superuser:
            queryset = queryset.filter(leader=request.user)
        paginator = PageNumberPagination()
        paginator.page_size = 20
        rows = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response([courseData(row) for row in rows])


class KnowledgeListView(ManagedAPIView):
    permCode = 'knowledge:manage'

    def get(self, request, ccId):
        cc = getCourseCollege(request.user, ccId, self.permCode)
        queryset = CourseKnowledge.objects.filter(course_college=cc)
        for name, choices in (('kind', CourseKnowledge.KIND_CHOICES),
                              ('status', CourseKnowledge.STATUS_CHOICES)):
            if name in request.query_params:
                value = request.query_params[name]
                if value not in dict(choices):
                    raise ValidationError({name: '筛选值无效'})
                queryset = queryset.filter(**{name: value})
        return paginated(request, queryset, KnowledgeListSerializer)

    def post(self, request, ccId):
        cc = getCourseCollege(request.user, ccId, self.permCode)
        obj = createKnowledge(request.user, cc, validated(KnowledgeInput, request.data))
        return Response(KnowledgeSerializer(obj).data, status=201)


class KnowledgeDetailView(ManagedAPIView):
    permCode = 'knowledge:manage'

    def get(self, request, objId):
        obj = getManagedObject(request.user, CourseKnowledge, objId, self.permCode)
        return Response(KnowledgeSerializer(obj).data)

    def patch(self, request, objId):
        obj = getManagedObject(request.user, CourseKnowledge, objId, self.permCode)
        inputClass = DocumentInput if obj.kind == 'document' else KnowledgeInput
        fields = validated(inputClass, request.data, partial=True)
        if 'file' in fields:
            obj = saveDocument(request.user, obj.course_college, fields, obj=obj)
        else:
            obj = updateKnowledge(request.user, obj, fields)
        return Response(KnowledgeSerializer(obj).data)

    def delete(self, request, objId):
        obj = getManagedObject(request.user, CourseKnowledge, objId, self.permCode)
        removeResource(request.user, obj, self.permCode)
        return Response(status=204)


class KnowledgeImportView(ManagedAPIView):
    permCode = 'knowledge:manage'

    def post(self, request, ccId):
        cc = getCourseCollege(request.user, ccId, self.permCode)
        fields = validated(UploadInput, request.data)
        objects = importKnowledge(request.user, cc, fields['file'])
        return Response({'count': len(objects), 'knowledgeIds': [obj.pk for obj in objects]}, status=201)


class DocumentCreateView(ManagedAPIView):
    permCode = 'knowledge:manage'

    def post(self, request, ccId):
        cc = getCourseCollege(request.user, ccId, self.permCode)
        obj = saveDocument(request.user, cc, validated(DocumentInput, request.data))
        return Response(KnowledgeSerializer(obj).data, status=201)


class DocumentReparseView(ManagedAPIView):
    permCode = 'knowledge:manage'

    def post(self, request, objId):
        if request.data:
            raise ValidationError('重新解析请求体应为空')
        obj = getManagedObject(request.user, CourseKnowledge, objId, self.permCode)
        return Response(KnowledgeSerializer(reparseDocument(request.user, obj)).data)


class KnowledgeDownloadView(ManagedAPIView):
    permCode = 'knowledge:manage'

    def get(self, request, objId):
        obj = getManagedObject(request.user, CourseKnowledge, objId, self.permCode)
        return FileResponse(openFile(obj), as_attachment=True, filename=obj.original_name)


class MaterialListView(ManagedAPIView):
    permCode = 'material:class_visible'

    def get(self, request, ccId):
        cc = getCourseCollege(request.user, ccId, self.permCode)
        return paginated(request, CourseMaterial.objects.filter(
            course_college=cc).prefetch_related('visible_classes'), MaterialSerializer)

    def post(self, request, ccId):
        cc = getCourseCollege(request.user, ccId, self.permCode)
        obj = saveMaterial(request.user, cc, validated(MaterialInput, request.data))
        return Response(MaterialSerializer(obj).data, status=201)


class MaterialDetailView(ManagedAPIView):
    permCode = 'material:class_visible'

    def get(self, request, objId):
        obj = getManagedObject(request.user, CourseMaterial, objId, self.permCode)
        return Response(MaterialSerializer(obj).data)

    def patch(self, request, objId):
        obj = getManagedObject(request.user, CourseMaterial, objId, self.permCode)
        obj = saveMaterial(request.user, obj.course_college,
                           validated(MaterialInput, request.data, partial=True), obj=obj)
        return Response(MaterialSerializer(obj).data)

    def delete(self, request, objId):
        obj = getManagedObject(request.user, CourseMaterial, objId, self.permCode)
        removeResource(request.user, obj, self.permCode)
        return Response(status=204)


class MaterialDownloadView(ManagedAPIView):
    permCode = 'material:class_visible'

    def get(self, request, objId):
        obj = getManagedObject(request.user, CourseMaterial, objId, self.permCode)
        return FileResponse(openFile(obj), as_attachment=True, filename=obj.original_name)


class ChapterListView(ManagedAPIView):
    permCode = 'chapter:manage'

    def get(self, request, ccId):
        cc = getCourseCollege(request.user, ccId, self.permCode)
        return paginated(request, CourseChapter.objects.filter(course_college=cc).prefetch_related(
            'knowledge_items', 'materials'), ChapterSerializer)

    def post(self, request, ccId):
        cc = getCourseCollege(request.user, ccId, self.permCode)
        obj = saveChapter(request.user, cc, validated(ChapterInput, request.data))
        return Response(ChapterSerializer(obj).data, status=201)


class ChapterDetailView(ManagedAPIView):
    permCode = 'chapter:manage'

    def get(self, request, objId):
        obj = getManagedObject(request.user, CourseChapter, objId, self.permCode)
        return Response(ChapterSerializer(obj).data)

    def patch(self, request, objId):
        obj = getManagedObject(request.user, CourseChapter, objId, self.permCode)
        obj = saveChapter(request.user, obj.course_college,
                          validated(ChapterInput, request.data, partial=True), obj=obj)
        return Response(ChapterSerializer(obj).data)

    def delete(self, request, objId):
        obj = getManagedObject(request.user, CourseChapter, objId, self.permCode)
        removeResource(request.user, obj, self.permCode)
        return Response(status=204)


class ChapterOrderView(ManagedAPIView):
    permCode = 'chapter:manage'

    def post(self, request, ccId):
        cc = getCourseCollege(request.user, ccId, self.permCode)
        fields = validated(ChapterOrderInput, request.data)
        objects = reorderChapters(request.user, cc, fields['chapter_ids'])
        return Response(ChapterSerializer(objects, many=True).data)


class TeacherAPIView(APIView):
    permission_classes = [IsTeacherGroup]


class TeacherCourseCollegeListView(TeacherAPIView):
    def get(self, request):
        paginator = PageNumberPagination()
        paginator.page_size = 20
        rows = paginator.paginate_queryset(teacherCourseColleges(request.user).order_by('id'), request)
        return paginator.get_paginated_response([courseData(row) for row in rows])


class TeacherKnowledgeListView(TeacherAPIView):
    def get(self, request, ccId):
        cc = getTeacherCourseCollege(request.user, ccId)
        return paginated(request, readableKnowledge(request.user, cc), KnowledgeListSerializer)


class TeacherKnowledgeDetailView(TeacherAPIView):
    def get(self, request, objId):
        obj = CourseKnowledge.objects.filter(pk=objId, is_visible=True, status='ready').first()
        if obj is None:
            raise Http404('资源不存在')
        getTeacherCourseCollege(request.user, obj.course_college_id)
        return Response(KnowledgeSerializer(obj).data)


class TeacherKnowledgeDownloadView(TeacherAPIView):
    def get(self, request, objId):
        obj = CourseKnowledge.objects.filter(pk=objId, is_visible=True, status='ready').first()
        if obj is None:
            raise Http404('资源不存在')
        getTeacherCourseCollege(request.user, obj.course_college_id)
        return FileResponse(openFile(obj), as_attachment=True, filename=obj.original_name)


class TeacherMaterialListView(TeacherAPIView):
    def get(self, request, ccId):
        cc = getTeacherCourseCollege(request.user, ccId)
        return paginated(request, readableMaterials(request.user, cc).prefetch_related(
            'visible_classes'), MaterialSerializer)


def teacherMaterial(user, objId):
    obj = CourseMaterial.objects.filter(pk=objId).first()
    if obj is None:
        raise Http404('资源不存在')
    cc = getTeacherCourseCollege(user, obj.course_college_id)
    obj = readableMaterials(user, cc).filter(pk=objId).first()
    if obj is None:
        raise Http404('资源不存在')
    return obj


class TeacherMaterialDetailView(TeacherAPIView):
    def get(self, request, objId):
        return Response(MaterialSerializer(teacherMaterial(request.user, objId)).data)


class TeacherMaterialDownloadView(TeacherAPIView):
    def get(self, request, objId):
        obj = teacherMaterial(request.user, objId)
        return FileResponse(openFile(obj), as_attachment=True, filename=obj.original_name)


def teacherChapterData(obj):
    return {'id': obj.pk, 'courseCollegeId': obj.course_college_id,
            'title': obj.title, 'description': obj.description, 'position': obj.position,
            'isVisible': obj.is_visible,
            'knowledgeIds': [item.pk for item in obj.readable_knowledge],
            'materialIds': [item.pk for item in obj.readable_materials]}


class TeacherChapterListView(TeacherAPIView):
    def get(self, request, ccId):
        cc = getTeacherCourseCollege(request.user, ccId)
        paginator = PageNumberPagination()
        paginator.page_size = 20
        rows = paginator.paginate_queryset(readableChapters(request.user, cc), request)
        return paginator.get_paginated_response([teacherChapterData(obj) for obj in rows])


class TeacherChapterDetailView(TeacherAPIView):
    def get(self, request, objId):
        obj = CourseChapter.objects.select_related('course_college').filter(
            pk=objId, is_visible=True).first()
        if obj is None:
            raise Http404('资源不存在')
        cc = getTeacherCourseCollege(request.user, obj.course_college_id)
        obj = readableChapters(request.user, cc).filter(pk=objId).first()
        if obj is None:
            raise Http404('资源不存在')
        return Response(teacherChapterData(obj))
