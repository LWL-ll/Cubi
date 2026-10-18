from django.apps import AppConfig


class UsersConfig(AppConfig):
    name = 'users'

    def ready(self):
        # 注册权限一致性系统检查（manage.py check 时自动执行）
        from . import checks  # noqa: F401
