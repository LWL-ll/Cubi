from django.conf import settings
from django.db import models


class CourseKnowledge(models.Model):
    """手工知识条目或原始文档及其可检索正文；文件始终在私有存储。"""
    KIND_CHOICES = [('entry', '知识条目'), ('document', '文档')]
    STATUS_CHOICES = [('ready', '已解析'), ('failed', '解析失败')]
    course_college = models.ForeignKey(
        'academics.CourseCollege', on_delete=models.PROTECT, related_name='knowledge_items')
    title = models.CharField(max_length=100)
    content = models.TextField(blank=True)
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default='entry')
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='ready')
    parse_error = models.CharField(max_length=300, blank=True)
    is_visible = models.BooleanField(default=True)
    storage_name = models.CharField(max_length=100, blank=True)
    original_name = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                   on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-id']


class CourseMaterial(models.Model):
    VISIBILITY_CHOICES = [('course', '本课程任课教师'), ('classes', '指定教学班的任课教师')]
    course_college = models.ForeignKey(
        'academics.CourseCollege', on_delete=models.PROTECT, related_name='shared_materials')
    title = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    visibility = models.CharField(max_length=12, choices=VISIBILITY_CHOICES, default='course')
    visible_classes = models.ManyToManyField(
        'academics.TeachingClass', through='MaterialTeachingClass', related_name='shared_materials')
    storage_name = models.CharField(max_length=100)
    original_name = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField()
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                   on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-id']


class MaterialTeachingClass(models.Model):
    material = models.ForeignKey(CourseMaterial, on_delete=models.CASCADE)
    teaching_class = models.ForeignKey('academics.TeachingClass', on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['material', 'teaching_class'], name='unique_shared_material_class')]


class CourseChapter(models.Model):
    course_college = models.ForeignKey(
        'academics.CourseCollege', on_delete=models.PROTECT, related_name='chapters')
    title = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    position = models.PositiveIntegerField()
    is_visible = models.BooleanField(default=True)
    knowledge_items = models.ManyToManyField(CourseKnowledge, blank=True, related_name='chapters')
    materials = models.ManyToManyField(CourseMaterial, blank=True, related_name='chapters')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                   on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['position', 'id']
        constraints = [
            models.UniqueConstraint(fields=['course_college', 'position'], name='unique_course_chapter_position'),
            models.CheckConstraint(condition=models.Q(position__gte=1), name='course_chapter_position_positive'),
        ]
