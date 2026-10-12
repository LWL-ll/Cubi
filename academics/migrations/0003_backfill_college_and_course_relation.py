"""数据迁移：回填班级所属学院，并把旧的 课程-班级 直接关系迁到 课程×学院 三层结构

背景：课程设置从「课程直接挂班级」重构为「课程 -> 学院 -> 班级」，
学院由学号格式解析自动生成。本迁移负责存量数据：
1. 为已有班级回填所属学院（优先用班级学生的学号解析，无学生则按班级名前缀映射）
2. 把 Course.clazzes 旧关系复制为 CourseCollege 记录（course + college + clazzes）
"""
from django.db import migrations

# 学院代码映射（与 academics/student_no.py 占位规则一致；迁移自包含，不 import 业务模块）
COLLEGE_CODES = {
    '10': ('计算机学院', '计科'),
    '11': ('外国语学院', '英语'),
    '12': ('数学学院', '数学'),
}

# 班级名前缀 -> 学院名（学号解析失败时的兜底映射）
CLAZZ_PREFIX_COLLEGE = {
    '计科': '计算机学院',
    '英语': '外国语学院',
    '数学': '数学学院',
}


def forward(apps, schema_editor):
    """回填学院并迁移旧的课程-班级关系（幂等：重复执行不会产生脏数据）"""
    Clazz = apps.get_model('academics', 'Clazz')
    College = apps.get_model('academics', 'College')
    Course = apps.get_model('academics', 'Course')
    CourseCollege = apps.get_model('academics', 'CourseCollege')

    collegeByName = {}

    def getCollege(name):
        """按学院名取对象，没有则创建（迁移内缓存）"""
        if name not in collegeByName:
            collegeByName[name] = College.objects.get_or_create(name=name)[0]
        return collegeByName[name]

    # 1. 回填班级所属学院
    for clazz in Clazz.objects.filter(college__isnull=True):
        collegeName = None
        # 优先：取本班任意学生的学号按占位规则解析学院
        for student in clazz.students.all():
            no = student.username
            if len(no) == 8 and no.isdigit():
                code = no[4:6]
                if code in COLLEGE_CODES:
                    collegeName = COLLEGE_CODES[code][0]
                    break
        # 兜底：按班级名前缀映射
        if collegeName is None:
            for prefix, name in CLAZZ_PREFIX_COLLEGE.items():
                if clazz.name.startswith(prefix):
                    collegeName = name
                    break
        if collegeName is not None:
            clazz.college = getCollege(collegeName)
            clazz.save()

    # 2. 旧的 课程-班级 直接关系 迁到 课程×学院（班级无学院时跳过）
    for course in Course.objects.all():
        for clazz in course.clazzes.all():
            if clazz.college is None:
                continue
            cc, _ = CourseCollege.objects.get_or_create(
                course=course, college=clazz.college)
            cc.clazzes.add(clazz)


def backward(apps, schema_editor):
    """回滚：数据由后续流程自然重建，无需反向操作"""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('academics', '0002_college_clazz_college_coursecollege_course_colleges'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
