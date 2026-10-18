"""教师工作台序列化器（复用 academics 的班级序列化器，只加教师视角字段）"""
from rest_framework import serializers

from academics.models import Clazz, Course
from academics.serializers import CollegeBriefSerializer, CourseBriefSerializer


class TeacherClazzSerializer(serializers.ModelSerializer):
    """我的教学班级列表项：班级名、学院、课程列表、学生数

    教师列表页需要轻量数据；详情页复用 academics.ClazzSerializer。
    """
    college = CollegeBriefSerializer(read_only=True)
    courses = serializers.SerializerMethodField()
    studentCount = serializers.IntegerField(source='students.count', read_only=True)

    class Meta:
        model = Clazz
        fields = ['id', 'name', 'college', 'courses', 'studentCount']

    def get_courses(self, obj):
        """班级的课程 = 关联到本班级的所有 课程×学院 里的课程（与 ClazzSerializer 一致）"""
        courses = Course.objects.filter(course_colleges__clazzes=obj).distinct()
        return CourseBriefSerializer(courses, many=True).data
