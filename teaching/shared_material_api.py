from rest_framework.response import Response
from rest_framework.views import APIView
from users.permissions import IsTeacherGroup, PermRequired
from .serializers import TeachingMaterialSerializer
from .shared_material_service import publishSharedMaterial


class TeacherPublishCourseMaterialView(APIView):
    permission_classes = [IsTeacherGroup, PermRequired]
    permCode = 'material:class_manage'

    def post(self, request, teachingClassId):
        material = publishSharedMaterial(request.user, teachingClassId, request.data)
        return Response(TeachingMaterialSerializer(material).data, status=201)
