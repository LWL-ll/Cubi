# 手写迁移（非自动生成）：
# College → Department、Clazz → AdminClass 本质是重命名，
# 但自动检测器会误判为「删除旧表 + 新建表」，直接应用会丢失数据，
# 所以手写本迁移用 RenameModel/RenameField 保留数据。
# 数据回填见 0006，旧字段清理与字段收紧见 0007。
# 详见 docs/数据库表重构设计.md 第七节数据迁移方案。
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('academics', '0004_remove_course_clazzes'),
    ]

    operations = [
        # ---- 重命名（表/列改名，数据无损）----
        migrations.RenameModel(old_name='College', new_name='Department'),
        migrations.RenameModel(old_name='Clazz', new_name='AdminClass'),
        migrations.RenameField(
            model_name='coursecollege', old_name='college', new_name='department'),
        migrations.RenameField(
            model_name='coursecollege', old_name='clazzes', new_name='admin_classes'),
        migrations.RenameField(
            model_name='course', old_name='colleges', new_name='departments'),
        # ---- 新字段（旧字段 college/students/teachers 先保留，
        #      0006 把数据迁入新表后，0007 再删除）----
        # code 暂不加 unique：现有行都会得到默认值 ''，直接加唯一约束会冲突，
        # 0006 回填业务编码后由 0007 收紧
        migrations.AddField(
            model_name='department',
            name='code',
            field=models.CharField(default='', max_length=32, verbose_name='学院编码'),
        ),
        migrations.AddField(
            model_name='department',
            name='is_active',
            field=models.BooleanField(default=True, verbose_name='是否启用'),
        ),
        migrations.AddField(
            model_name='adminclass',
            name='code',
            field=models.CharField(blank=True, default='', max_length=32, verbose_name='班级编码'),
        ),
        migrations.AddField(
            model_name='adminclass',
            name='enrollment_year',
            field=models.SmallIntegerField(blank=True, null=True, verbose_name='年级'),
        ),
        migrations.CreateModel(
            name='Major',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=32, verbose_name='专业编码')),
                ('name', models.CharField(max_length=100, verbose_name='专业名称')),
                ('is_active', models.BooleanField(default=True, verbose_name='是否启用')),
                ('department', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='majors', to='academics.department', verbose_name='所属学院')),
            ],
            options={
                'verbose_name': '专业',
                'verbose_name_plural': '专业',
                'unique_together': {('department', 'code')},
            },
        ),
        migrations.AddField(
            model_name='adminclass',
            name='major',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='admin_classes', to='academics.major', verbose_name='所属专业'),
        ),
        # student_profile 外键在 0006 添加（users.0012 依赖本迁移，这里引用会形成环）
        migrations.CreateModel(
            name='ClassMembership',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('joined_at', models.DateField(verbose_name='入班日期')),
                ('left_at', models.DateField(blank=True, null=True, verbose_name='离班日期')),
                ('admin_class', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='memberships', to='academics.adminclass', verbose_name='行政班级')),
            ],
            options={
                'verbose_name': '班级成员',
                'verbose_name_plural': '班级成员',
            },
        ),
        # 外键与 M2M 在 0006 补齐（course_college 参与 unique_together，字段齐了才能加约束）
        migrations.CreateModel(
            name='TeachingClass',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=50, verbose_name='教学班名称')),
                ('term', models.CharField(max_length=20, verbose_name='学期')),
            ],
            options={
                'verbose_name': '教学班',
                'verbose_name_plural': '教学班',
            },
        ),
    ]
