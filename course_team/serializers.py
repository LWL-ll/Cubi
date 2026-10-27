from collections.abc import Mapping

from rest_framework import serializers

from .models import CourseChapter, CourseKnowledge, CourseMaterial


class StrictInput(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, Mapping) or set(data) - set(self.fields):
            raise serializers.ValidationError({'non_field_errors': ['请求包含未知字段或不是对象']})
        return super().to_internal_value(data)


class SafeText(serializers.CharField):
    def run_validation(self, data=serializers.empty):
        if data is not serializers.empty and not isinstance(data, str):
            raise serializers.ValidationError('必须是文本')
        result = super().run_validation(data)
        try:
            result.encode('utf-8')
        except UnicodeEncodeError:
            raise serializers.ValidationError('含无效字符') from None
        return result


class ObjectId(serializers.IntegerField):
    def __init__(self, **kwargs):
        super().__init__(min_value=1, max_value=2**63 - 1, **kwargs)

    def to_internal_value(self, data):
        if isinstance(data, bool) or not (isinstance(data, int)
                or isinstance(data, str) and data.isascii() and data.isdecimal()):
            raise serializers.ValidationError('必须是正整数编号')
        return super().to_internal_value(data)


class IdList(serializers.ListField):
    def __init__(self, **kwargs):
        super().__init__(child=ObjectId(), max_length=2000, **kwargs)

    def to_internal_value(self, data):
        result = super().to_internal_value(data)
        if len(result) != len(set(result)):
            raise serializers.ValidationError('编号不能重复')
        return result


class KnowledgeInput(StrictInput):
    title = SafeText(max_length=100)
    content = SafeText(max_length=1_000_000)
    isVisible = serializers.BooleanField(source='is_visible', required=False, default=True)


class DocumentInput(StrictInput):
    title = SafeText(max_length=100)
    file = serializers.FileField()
    isVisible = serializers.BooleanField(source='is_visible', required=False, default=True)


class MaterialInput(StrictInput):
    title = SafeText(max_length=100)
    description = SafeText(max_length=2000, allow_blank=True, required=False)
    file = serializers.FileField()
    visibility = serializers.ChoiceField(choices=CourseMaterial.VISIBILITY_CHOICES, required=False)
    teachingClassIds = IdList(source='class_ids', required=False)


class ChapterInput(StrictInput):
    title = SafeText(max_length=100)
    description = SafeText(max_length=2000, allow_blank=True, required=False)
    isVisible = serializers.BooleanField(source='is_visible', required=False, default=True)
    knowledgeIds = IdList(source='knowledge_ids', required=False)
    materialIds = IdList(source='material_ids', required=False)


class ChapterOrderInput(StrictInput):
    chapterIds = IdList(source='chapter_ids')


class UploadInput(StrictInput):
    file = serializers.FileField()


class KnowledgeSerializer(serializers.ModelSerializer):
    courseCollegeId = serializers.IntegerField(source='course_college_id')
    isVisible = serializers.BooleanField(source='is_visible')
    parseError = serializers.CharField(source='parse_error')
    originalName = serializers.CharField(source='original_name')
    createdAt = serializers.DateTimeField(source='created_at')
    updatedAt = serializers.DateTimeField(source='updated_at')

    class Meta:
        model = CourseKnowledge
        fields = ['id', 'courseCollegeId', 'title', 'content', 'kind', 'status',
                  'parseError', 'isVisible', 'originalName', 'size', 'createdAt', 'updatedAt']


class KnowledgeListSerializer(KnowledgeSerializer):
    excerpt = serializers.SerializerMethodField()

    def get_excerpt(self, obj):
        return obj.content[:200]

    class Meta(KnowledgeSerializer.Meta):
        fields = [name for name in KnowledgeSerializer.Meta.fields if name != 'content'] + ['excerpt']


class MaterialSerializer(serializers.ModelSerializer):
    courseCollegeId = serializers.IntegerField(source='course_college_id')
    teachingClassIds = serializers.PrimaryKeyRelatedField(source='visible_classes', many=True, read_only=True)
    originalName = serializers.CharField(source='original_name')
    createdAt = serializers.DateTimeField(source='created_at')
    updatedAt = serializers.DateTimeField(source='updated_at')

    class Meta:
        model = CourseMaterial
        fields = ['id', 'courseCollegeId', 'title', 'description', 'visibility',
                  'teachingClassIds', 'originalName', 'size', 'createdAt', 'updatedAt']


class ChapterSerializer(serializers.ModelSerializer):
    courseCollegeId = serializers.IntegerField(source='course_college_id')
    isVisible = serializers.BooleanField(source='is_visible')
    knowledgeIds = serializers.PrimaryKeyRelatedField(source='knowledge_items', many=True, read_only=True)
    materialIds = serializers.PrimaryKeyRelatedField(source='materials', many=True, read_only=True)

    class Meta:
        model = CourseChapter
        fields = ['id', 'courseCollegeId', 'title', 'description', 'position',
                  'isVisible', 'knowledgeIds', 'materialIds']
