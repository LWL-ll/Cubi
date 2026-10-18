# 手写迁移：V2 收尾——0006 已把数据回填完毕，
# 这里删除旧字段、把临时可空的字段收紧为最终态。
# 详见 docs/数据库表重构设计.md 第七节数据迁移方案。
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('academics', '0006_classmembership_student_profile_and_more'),
    ]

    operations = [
        # 旧字段清理：学生关系已迁入 ClassMembership，
        # 教师任课已迁入 TeachingClass.teachers，学院归属已迁入 major
        migrations.RemoveField(model_name='adminclass', name='college'),
        migrations.RemoveField(model_name='adminclass', name='students'),
        migrations.RemoveField(model_name='adminclass', name='teachers'),
        # 学院编码收紧为唯一（0006 已回填，不会冲突），同时去掉迁移临时默认值
        migrations.AlterField(
            model_name='department',
            name='code',
            field=models.CharField(max_length=32, unique=True, verbose_name='学院编码'),
        ),
        # 班级编码去掉迁移临时默认值（业务编码由教务端后续生成）
        migrations.AlterField(
            model_name='adminclass',
            name='code',
            field=models.CharField(blank=True, max_length=32, verbose_name='班级编码'),
        ),
        # 专业收紧为非空（0006 已给每个班级挂上未分类专业）
        migrations.AlterField(
            model_name='adminclass',
            name='major',
            field=models.ForeignKey(blank=True, on_delete=django.db.models.deletion.PROTECT, related_name='admin_classes', to='academics.major', verbose_name='所属专业'),
        ),
        # 学院收紧为非空（重命名自旧 college 外键，原有数据本来就有值）
        migrations.AlterField(
            model_name='coursecollege',
            name='department',
            field=models.ForeignKey(blank=True, on_delete=django.db.models.deletion.CASCADE, related_name='course_colleges', to='academics.department', verbose_name='学院'),
        ),
        # 班级名去掉旧的全局唯一约束，改为「专业 + 年级 + 名称」联合唯一
        # （不同年级允许出现同名班级，如 23 级与 24 级的「1班」）
        migrations.AlterField(
            model_name='adminclass',
            name='name',
            field=models.CharField(max_length=50, verbose_name='班级名称'),
        ),
        migrations.AlterUniqueTogether(
            name='adminclass',
            unique_together={('major', 'enrollment_year', 'name')},
        ),
    ]
