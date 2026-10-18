# 手写迁移：把重命名带过来的旧定义对齐到 V2 模型最终态
# （RenameModel 保留旧 Meta/字段定义，此处补齐差异）。
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('academics', '0008_fix_enrollment_year'),
    ]

    operations = [
        # 旧 Clazz 的 Meta 名是「班级」，对齐为「行政班级」
        # （unique_together 已由 0007 单独处理，AlterModelOptions 不影响）
        migrations.AlterModelOptions(
            name='adminclass',
            options={'verbose_name': '行政班级', 'verbose_name_plural': '行政班级'},
        ),
        # 旧 clazzes 字段的 related_name/verbose_name 对齐 V2 定义
        migrations.AlterField(
            model_name='coursecollege',
            name='admin_classes',
            field=models.ManyToManyField(blank=True, related_name='course_colleges', to='academics.adminclass', verbose_name='本课程行政班'),
        ),
        # 旧 College.name 长度 50，V2 模型为 100
        migrations.AlterField(
            model_name='department',
            name='name',
            field=models.CharField(max_length=100, verbose_name='学院名称'),
        ),
    ]
