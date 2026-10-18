from django.apps import AppConfig


class TeachingConfig(AppConfig):
    """教师工作台应用：班级通知、学情分析、班级资料、作业、批改、个人题库

    设计约定：
    - 教师的所有功能仅限教学班级（对象级校验见 helpers.getTeacherClazz）
    - 页面仅作开发预览，主线是前后端分离 API（/api/teacher/ 前缀）
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'teaching'
    verbose_name = '教师教学'
