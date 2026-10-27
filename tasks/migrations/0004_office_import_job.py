import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0003_fix_recycle_schedule_cluster'),
        ('academics', '0010_membership_constraints'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='OfficeImportJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('import_type', models.CharField(choices=[('course', '课程'), ('teacher', '教师'), ('student', '学生自动分班'), ('class_student', '指定行政班学生'), ('course_teacher', '课程教师')], max_length=20, verbose_name='导入类型')),
                ('status', models.CharField(choices=[('pending', '等待中'), ('running', '执行中'), ('succeeded', '全部成功'), ('partial', '部分成功'), ('failed', '失败')], default='pending', max_length=12, verbose_name='状态')),
                ('original_name', models.CharField(max_length=255, verbose_name='文件名')),
                ('source_name', models.CharField(blank=True, max_length=80, verbose_name='私有存储标识')),
                ('total', models.PositiveIntegerField(default=0, verbose_name='有效数据行数')),
                ('completed', models.PositiveIntegerField(default=0, verbose_name='已处理行数')),
                ('success_count', models.PositiveIntegerField(default=0, verbose_name='成功行数')),
                ('error_count', models.PositiveIntegerField(default=0, verbose_name='失败行数')),
                ('row_cursor', models.PositiveIntegerField(default=0, verbose_name='已提交物理行游标')),
                ('errors', models.JSONField(default=list, verbose_name='逐行错误')),
                ('error', models.CharField(blank=True, max_length=200, verbose_name='批次错误')),
                ('attempt', models.PositiveIntegerField(default=1, verbose_name='执行轮次')),
                ('expires_at', models.DateTimeField(verbose_name='排队或执行截止时间')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='提交时间')),
                ('started_at', models.DateTimeField(blank=True, null=True, verbose_name='开始时间')),
                ('finished_at', models.DateTimeField(blank=True, null=True, verbose_name='结束时间')),
                ('admin_class', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='import_jobs', to='academics.adminclass', verbose_name='目标行政班')),
                ('course_college', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='import_jobs', to='academics.coursecollege', verbose_name='目标课程学院')),
                ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='office_import_jobs', to=settings.AUTH_USER_MODEL, verbose_name='提交人')),
            ],
            options={
                'verbose_name': '教务导入批次',
                'verbose_name_plural': '教务导入批次',
                'ordering': ['-created_at', '-id'],
                'indexes': [models.Index(fields=['created_by', 'created_at'], name='office_import_owner_idx'), models.Index(fields=['status', 'expires_at'], name='office_import_expiry_idx')],
            },
        ),
    ]
