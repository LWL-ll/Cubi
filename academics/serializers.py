from rest_framework import serializers

from .models import Clazz, College, Course, CourseCollege


class CollegeBriefSerializer(serializers.ModelSerializer):
    """学院简要信息（课程详情里嵌套展示用）"""

    class Meta:
        model = College
        fields = ['id', 'name']


class CollegeSerializer(serializers.ModelSerializer):
    """学院详情：含班级数量"""
    clazzCount = serializers.IntegerField(source='clazzes.count', read_only=True)

    class Meta:
        model = College
        fields = ['id', 'name', 'clazzCount']


class ClazzBriefSerializer(serializers.ModelSerializer):
    """班级简要信息（课程×学院详情里嵌套展示用）"""
    studentCount = serializers.IntegerField(source='students.count', read_only=True)
    teacherCount = serializers.IntegerField(source='teachers.count', read_only=True)

    class Meta:
        model = Clazz
        fields = ['id', 'name', 'studentCount', 'teacherCount']


class UserBriefSerializer(serializers.Serializer):
    """用户简要信息（学生/教师/组长列表用）：id、账号、姓名"""
    id = serializers.IntegerField()
    username = serializers.CharField()
    name = serializers.CharField()


class CourseCollegeSerializer(serializers.ModelSerializer):
    """课程×学院 详情：学院、组长、教师池、班级列表"""
    college = CollegeBriefSerializer(read_only=True)
    leader = UserBriefSerializer(read_only=True)
    teachers = UserBriefSerializer(many=True, read_only=True)
    clazzes = ClazzBriefSerializer(many=True, read_only=True)

    class Meta:
        model = CourseCollege
        fields = ['id', 'college', 'leader', 'teachers', 'clazzes']


class CourseSerializer(serializers.ModelSerializer):
    """课程详情：含开设学院列表"""
    colleges = CollegeBriefSerializer(many=True, read_only=True)

    class Meta:
        model = Course
        fields = ['id', 'code', 'name', 'colleges']


class CourseBriefSerializer(serializers.ModelSerializer):
    """课程简要信息（班级详情里嵌套展示用）"""

    class Meta:
        model = Course
        fields = ['id', 'code', 'name']


class ClazzSerializer(serializers.ModelSerializer):
    """班级详情：含所属学院、课程列表（经课程×学院）、学生列表、教师列表"""
    college = CollegeBriefSerializer(read_only=True)
    courses = serializers.SerializerMethodField()
    students = UserBriefSerializer(many=True, read_only=True)
    teachers = UserBriefSerializer(many=True, read_only=True)

    class Meta:
        model = Clazz
        fields = ['id', 'name', 'college', 'courses', 'students', 'teachers']

    def get_courses(self, obj):
        """班级的课程 = 关联到本班级的所有 课程×学院 里的课程"""
        courses = Course.objects.filter(course_colleges__clazzes=obj).distinct()
        return CourseBriefSerializer(courses, many=True).data
