# 手写迁移：V1 → V2 数据回填（无损）。
# 结构重命名见 0005，旧字段清理与字段收紧见 0007。
# 详见 docs/数据库表重构设计.md 第七节数据迁移方案。
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def fillV2Data(apps, schemaEditor):
    """旧数据迁入 V2 新表，顺序：
    学院编码 → 未分类专业 → 班级专业/年级 → 学生身份+资料+成员关系
    → 教师身份+资料 → 教学班生成
    """
    import re
    from datetime import date

    # 历史模型（0006 执行中的状态：旧字段 college/students/teachers 仍在，
    # 0005 重命名后通过新模型名访问）
    Department = apps.get_model('academics', 'Department')
    Major = apps.get_model('academics', 'Major')
    AdminClass = apps.get_model('academics', 'AdminClass')
    ClassMembership = apps.get_model('academics', 'ClassMembership')
    CourseCollege = apps.get_model('academics', 'CourseCollege')
    TeachingClass = apps.get_model('academics', 'TeachingClass')
    SchoolIdentity = apps.get_model('users', 'SchoolIdentity')
    StudentProfile = apps.get_model('users', 'StudentProfile')
    TeacherProfile = apps.get_model('users', 'TeacherProfile')

    # 1) 学院编码：D + 两位零填充 id（业务编码只要求唯一）
    for department in Department.objects.order_by('id'):
        department.code = f'D{department.id:02d}'
        department.save(update_fields=['code'])

    # 2) 每个学院建「未分类专业」XX00：旧数据没有专业信息，班级先挂这里，
    #    教务端上线后按学号规则（2 位专业码）把班级改挂到真实专业
    majorByDepartment = {}
    for department in Department.objects.all():
        major, _ = Major.objects.get_or_create(
            department=department, code='XX00', defaults={'name': '未分类专业'})
        majorByDepartment[department.id] = major

    # 3) 班级补专业、年级：
    #    尾部「2 位年级 + 2 位班序」结构优先（2301 → 2023 级 01 班），
    #    否则匹配名字中独立的 20xx 四位年份（后不紧跟数字，如「2023级」）
    for adminClass in AdminClass.objects.all():
        if adminClass.major_id is None:
            adminClass.major = majorByDepartment.get(adminClass.college_id)
        if adminClass.enrollment_year is None:
            shortMatch = re.search(r'(\d{2})(\d{2})\D*$', adminClass.name)
            yearMatch = re.search(r'(20\d{2})(?!\d)', adminClass.name)
            if shortMatch:
                adminClass.enrollment_year = 2000 + int(shortMatch.group(1))
            elif yearMatch:
                adminClass.enrollment_year = int(yearMatch.group(1))
        adminClass.save()

    # 4) 学生三连：学校身份 → 学生资料 → 班级成员关系（从旧 students M2M 迁入）
    #    username 存的就是学号原值，直接作为 school_number
    for adminClass in AdminClass.objects.all():
        for user in adminClass.students.all():
            identity, _ = SchoolIdentity.objects.get_or_create(
                user=user, school_code='NSU', identity_type='student',
                defaults={'school_number': user.username})
            enrollmentYear = adminClass.enrollment_year
            if enrollmentYear is None:
                # 班级名解析不出年份时退化学号前 4 位，再不行兜底 2023
                enrollmentYear = (int(user.username[:4])
                                  if user.username[:4].isdigit() else 2023)
            profile, _ = StudentProfile.objects.get_or_create(
                identity=identity,
                defaults={'major': adminClass.major,
                          'enrollment_year': enrollmentYear})
            ClassMembership.objects.get_or_create(
                student_profile=profile, admin_class=adminClass,
                defaults={'joined_at': date(enrollmentYear, 9, 1)})

    # 5) 教师：身份 + 资料（教师池 = 旧 Clazz.teachers ∪ CourseCollege.teachers）
    teacherUsers = set()
    for adminClass in AdminClass.objects.all():
        teacherUsers.update(adminClass.teachers.all())
    for courseCollege in CourseCollege.objects.all():
        teacherUsers.update(courseCollege.teachers.all())
    for user in teacherUsers:
        identity, _ = SchoolIdentity.objects.get_or_create(
            user=user, school_code='NSU', identity_type='teacher',
            defaults={'school_number': user.username})
        TeacherProfile.objects.get_or_create(identity=identity)

    # 6) 教学班：每个（课程×学院 × 行政班）组合生成一个，教师继承课程教师池，
    #    学期取当前学期；多班合班、增删教师由教务在后台调整
    for courseCollege in CourseCollege.objects.all():
        for adminClass in courseCollege.admin_classes.all():
            teachingClass, _ = TeachingClass.objects.get_or_create(
                course_college=courseCollege, term='2026-2027-1',
                name=f'{courseCollege.course.name}-{adminClass.name}')
            teachingClass.admin_classes.add(adminClass)
            teachingClass.teachers.set(courseCollege.teachers.all())


class Migration(migrations.Migration):

    dependencies = [
        ('academics', '0005_adminclass_department_major_teachingclass_and_more'),
        ('users', '0012_schoolidentity_studentprofile_teacherprofile'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 补齐 0005 暂缓的外键/M2M（表都是空的，非空外键可直接添加）
        migrations.AddField(
            model_name='classmembership',
            name='student_profile',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='memberships', to='users.studentprofile', verbose_name='学生'),
        ),
        migrations.AddField(
            model_name='teachingclass',
            name='admin_classes',
            field=models.ManyToManyField(blank=True, related_name='teaching_classes', to='academics.adminclass', verbose_name='组成行政班'),
        ),
        migrations.AddField(
            model_name='teachingclass',
            name='course_college',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='teaching_classes', to='academics.coursecollege', verbose_name='课程×学院'),
        ),
        migrations.AddField(
            model_name='teachingclass',
            name='teachers',
            field=models.ManyToManyField(blank=True, related_name='teaching_classes', to=settings.AUTH_USER_MODEL, verbose_name='授课教师'),
        ),
        migrations.AlterUniqueTogether(
            name='teachingclass',
            unique_together={('course_college', 'term', 'name')},
        ),
        # 数据回填：必须在删除旧字段（0007）之前执行
        migrations.RunPython(fillV2Data, migrations.RunPython.noop),
    ]
