"""权限声明遗漏默认拒绝，系统检查覆盖实际挂载的嵌套路由。"""
from types import ModuleType, SimpleNamespace

from django.test import SimpleTestCase, override_settings
from django.urls import include, path
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from users.checks import check_api_permission_declarations
from users.permissions import PermRequired


class MissingPermissionView(APIView):
    permission_classes = [PermRequired]

    def get(self, request):
        return Response({'ok': True})


class CodedPermissionView(MissingPermissionView):
    permCode = 'homework:view'


class ServicePermissionView(MissingPermissionView):
    servicePermissionCheck = 'getReadableProblem / getManageableProblem'


class PermissionDeclarationTests(SimpleTestCase):
    def callView(self, viewClass, superuser=False, allowed=True):
        request = APIRequestFactory().get('/test/')
        user = SimpleNamespace(is_authenticated=True, is_superuser=superuser,
                               has_perm=lambda code: allowed)
        force_authenticate(request, user=user)
        return viewClass.as_view()(request)

    def checkRoutes(self, patterns):
        urlconf = ModuleType('permission_test_urls')
        urlconf.urlpatterns = patterns
        with override_settings(ROOT_URLCONF=urlconf):
            return check_api_permission_declarations(None)

    def test_missing_or_invalid_declarations_reject_even_superusers(self):
        declarations = [
            {}, {'permCode': 'unknown:permission'}, {'permCode': ''},
            {'permCode': ['homework:view']}, {'servicePermissionCheck': True},
            {'servicePermissionCheck': ''}, {'servicePermissionCheck': '   '},
            {'permCode': 'homework:view', 'servicePermissionCheck': 'service'},
        ]
        for attributes in declarations:
            viewClass = type('InvalidPermissionView', (MissingPermissionView,), attributes)
            for superuser in (False, True):
                with self.subTest(attributes=attributes, superuser=superuser):
                    self.assertEqual(self.callView(viewClass, superuser=superuser).status_code, 403)

    def test_known_code_keeps_existing_permission_semantics(self):
        self.assertEqual(self.callView(CodedPermissionView).status_code, 200)
        self.assertEqual(self.callView(CodedPermissionView, allowed=False).status_code, 403)
        self.assertEqual(self.callView(CodedPermissionView, superuser=True, allowed=False).status_code, 200)

    def test_service_delegation_still_requires_login(self):
        self.assertEqual(self.callView(ServicePermissionView).status_code, 200)
        request = APIRequestFactory().get('/test/')
        self.assertEqual(ServicePermissionView.as_view()(request).status_code, 403)

    def test_real_routes_pass_check(self):
        self.assertEqual(check_api_permission_declarations(None), [])

    def test_nested_routes_report_missing_code(self):
        patterns = [path('nested/', include([
            path('missing/', MissingPermissionView.as_view()),
            path('coded/', CodedPermissionView.as_view()),
            path('service/', ServicePermissionView.as_view()),
        ]))]
        errors = self.checkRoutes(patterns)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, 'users.E001')
        self.assertIn('nested/missing/', errors[0].obj)

    def test_routes_report_unknown_and_conflicting_declarations(self):
        conflict = type('ConflictView', (CodedPermissionView,),
                        {'servicePermissionCheck': 'service'})
        errors = self.checkRoutes([
            path('unknown/', CodedPermissionView.as_view(permCode='unknown:permission')),
            path('conflict/', conflict.as_view()),
        ])
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(error.id == 'users.E001' for error in errors))

    def test_permission_subclasses_are_checked(self):
        class ChildPermission(PermRequired):
            pass

        viewClass = type('ChildView', (MissingPermissionView,),
                         {'permission_classes': [ChildPermission]})
        errors = self.checkRoutes([path('child/', viewClass.as_view())])
        self.assertEqual(len(errors), 1)

    def test_unrelated_permission_classes_are_not_restricted(self):
        viewClass = type('OtherView', (APIView,), {'permission_classes': []})
        self.assertEqual(self.checkRoutes([path('other/', viewClass.as_view())]), [])
