"""教师工作台序列化器（列表项轻量数据；详情复用 academics 的序列化器）"""
from rest_framework import serializers

from academics.models import ClassMembership, TeachingClass
from academics.serializers import (CourseBriefSerializer,
                                   DepartmentBriefSerializer)


class TeacherTeachingClassSerializer(serializers.ModelSerializer):
    """我的教学班列表项：名称、学期、课程、学院、学生数

    学生数经「组成行政班 → 当前成员关系」推导。
    """
    course = CourseBriefSerializer(source='course_college.course', read_only=True)
    department = DepartmentBriefSerializer(
        source='course_college.department', read_only=True)
    studentCount = serializers.SerializerMethodField()

    class Meta:
        model = TeachingClass
        fields = ['id', 'name', 'term', 'course', 'department', 'studentCount']

    def get_studentCount(self, obj):
        """当前在班成员数 = 组成行政班的成员关系聚合（多班合班自动去重场景下
        按成员关系计数，一人一班约束保证不重复）"""
        return ClassMembership.objects.filter(
            admin_class__teaching_classes=obj,
            left_at__isnull=True).count()
