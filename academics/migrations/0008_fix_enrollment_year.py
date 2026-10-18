# 手写迁移（补丁）：修正 0006 数据回填时班级名的年级解析错误。
# 「计科2301」这类名字被当时的 (\d{4}) 正则误读成 4 位年份，
# enrollment_year 写成了 2301（正确应为 2023 级）。
# 换算规则：2000 + 值的前两位（2301 → 2023），月日不变。
# 0006 文件本身也已同步修正，本迁移只修已回填出错的库。
from django.db import migrations


def fixEnrollmentYear(apps, schemaEditor):
    """把误读的年级换算回来：2301 → 2023"""
    AdminClass = apps.get_model('academics', 'AdminClass')
    StudentProfile = apps.get_model('users', 'StudentProfile')
    ClassMembership = apps.get_model('academics', 'ClassMembership')

    # 班级年级：正常年份不会超过 2100，误读值都是 23xx 形式
    for adminClass in AdminClass.objects.filter(enrollment_year__gte=2100):
        adminClass.enrollment_year = 2000 + adminClass.enrollment_year // 100
        adminClass.save(update_fields=['enrollment_year'])

    # 学生资料年级（错值来自班级）
    for profile in StudentProfile.objects.filter(enrollment_year__gte=2100):
        profile.enrollment_year = 2000 + profile.enrollment_year // 100
        profile.save(update_fields=['enrollment_year'])

    # 入班日期：年份同步换算，月日保留
    for membership in ClassMembership.objects.filter(joined_at__year__gte=2100):
        membership.joined_at = membership.joined_at.replace(
            year=2000 + membership.joined_at.year // 100)
        membership.save(update_fields=['joined_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('academics', '0007_tighten_v2_fields'),
        ('users', '0013_tighten_student_profile_major'),
    ]

    operations = [
        migrations.RunPython(fixEnrollmentYear, migrations.RunPython.noop),
    ]
