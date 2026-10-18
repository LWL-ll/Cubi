# 手写迁移：学生资料的专业收紧为非空。
# 数据回填见 academics 0006（每个学生都随班级挂上了未分类专业）。
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('academics', '0007_tighten_v2_fields'),
        ('users', '0012_schoolidentity_studentprofile_teacherprofile'),
    ]

    operations = [
        migrations.AlterField(
            model_name='studentprofile',
            name='major',
            field=models.ForeignKey(blank=True, on_delete=django.db.models.deletion.PROTECT, related_name='student_profiles', to='academics.major', verbose_name='所属专业'),
        ),
    ]
