"""教务账号查询、资料维护和系统管理员重置密码接口。"""
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from academics.base_service import textValue
from .account_service import (accountData, getManagedAccount, managedAccounts,
                             resetAccountPassword, updateAccount)
from .permissions import GROUP_NAMES, PermRequired


class AccountView(APIView):
    permission_classes = [PermRequired]
    permCode = 'class:manage'

    def handle_exception(self, exc):
        if isinstance(exc, ValidationError):
            return Response({'error': '；'.join(exc.messages)}, status=400)
        return super().handle_exception(exc)


class AccountListView(AccountView):
    def get(self, request):
        accounts = managedAccounts(request.user)
        if 'q' in request.query_params:
            query = textValue(request.query_params['q'], 100, 'q', blank=True)
            accounts = accounts.filter(Q(username__icontains=query) | Q(name__icontains=query)
                                       | Q(identities__school_number__icontains=query))
        if 'role' in request.query_params:
            role = request.query_params['role']
            if role not in ('teacher', 'student'):
                raise ValidationError('role 只支持 teacher 或 student')
            accounts = accounts.filter(groups__name=GROUP_NAMES[role])
        if 'isActive' in request.query_params:
            value = request.query_params['isActive']
            if value not in ('true', 'false'):
                raise ValidationError('isActive 只能为 true 或 false')
            accounts = accounts.filter(is_active=value == 'true')
        accounts = accounts.distinct().order_by('username', 'pk')
        paginator = PageNumberPagination()
        paginator.page_size = 20
        page = paginator.paginate_queryset(accounts, request)
        return paginator.get_paginated_response([accountData(account) for account in page])


class AccountDetailView(AccountView):
    def get(self, request, accountId):
        return Response(accountData(getManagedAccount(request.user, accountId)))

    def patch(self, request, accountId):
        return Response(accountData(updateAccount(request.user, accountId, request.data)))


@method_decorator(sensitive_post_parameters('password'), name='dispatch')
class AccountPasswordResetView(AccountView):
    def post(self, request, accountId):
        account = resetAccountPassword(request.user, accountId, request.data)
        return Response({'id': account.pk, 'mustChangePassword': True})
