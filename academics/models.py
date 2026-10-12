from django.conf import settings
from django.db import models


class College(models.Model):
    """学院：学生导入时按学号格式解析自动生成（get_or_create），无单独导入入口"""
    name = models.CharField('学院名称', max_length=50, unique=True)

    class Meta:
        verbose_name = '学院'
        verbose_name_plural = '学院'

    def __str__(self):
        return self.name


class Clazz(models.Model):
    """班级：学生与教师都挂在班级上；班级属于学院（按学号格式解析分配）"""
    name = models.CharField('班级名称', max_length=50, unique=True)
    # 所属学院：由学生学号解析自动分配（历史数据可能为空，导入学生时自动补）
    college = models.ForeignKey(
        College, verbose_name='所属学院', null=True, blank=True,
        on_delete=models.PROTECT, related_name='clazzes')
    # 班级学生（学生账号，一个学生属于一个班级）
    students = models.ManyToManyField(
        settings.AUTH_USER_MODEL, related_name='study_clazzes',
        blank=True, verbose_name='学生')
    # 班级教师（教务分配的任课教师）
    teachers = models.ManyToManyField(
        settings.AUTH_USER_MODEL, related_name='teach_clazzes',
        blank=True, verbose_name='教师')

    class Meta:
        verbose_name = '班级'
        verbose_name_plural = '班级'

    def __str__(self):
        return self.name


class Course(models.Model):
    """课程：教务导入课程后，按 课程 -> 学院 -> 班级 三层结构设置"""
    code = models.CharField('课程编号', max_length=20, unique=True)
    name = models.CharField('课程名称', max_length=50)
    # 课程开设的学院（与 CourseCollege 关联表配套使用）
    colleges = models.ManyToManyField(
        College, through='CourseCollege', related_name='courses',
        verbose_name='开设学院')

    class Meta:
        verbose_name = '课程'
        verbose_name_plural = '课程'

    def __str__(self):
        return f'{self.name}（{self.code}）'


class CourseCollege(models.Model):
    """课程×学院 关联表：课程组组长、本课程教师池、本课程班级都挂在这一层"""
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name='course_colleges',
        verbose_name='课程')
    college = models.ForeignKey(
        College, on_delete=models.CASCADE, related_name='course_colleges',
        verbose_name='学院')
    # 课程组组长（教务用工号指定；指定后自动加入“课程组”分组）
    leader = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='课程组组长', null=True,
        blank=True, on_delete=models.SET_NULL, related_name='led_course_colleges')
    # 本课程教师池（导入后自动分配到本课程已关联的班级）
    teachers = models.ManyToManyField(
        settings.AUTH_USER_MODEL, verbose_name='本课程教师', blank=True,
        related_name='course_college_teach')
    # 本课程×学院 下开设的班级
    clazzes = models.ManyToManyField(
        Clazz, verbose_name='本课程班级', blank=True, related_name='course_colleges')

    class Meta:
        # 同一课程在同一学院只能有一条设置记录
        unique_together = [('course', 'college')]
        verbose_name = '课程学院'
        verbose_name_plural = '课程学院'

    def __str__(self):
        return f'{self.course.name} - {self.college.name}'
