---
name: "dj-permcheck"
description: "Django 权限审查技能。检查代码中的权限是否正确、有没有越权漏洞（水平越权/垂直越权），给出修复方案和标准实现参考。覆盖功能权限、数据权限、对象级权限、多级角色权限。触发：写完权限代码时、自查越权漏洞时、权限设计评审时。"
---

# dj-permcheck - 权限校验助手

## 用法

- **权限审查**: 把代码贴给我，说"用 dj-permcheck 查一下权限"，我帮你检查有没有越权漏洞
- **代码生成**: 说"用 dj-permcheck 生成一个 XX 权限的代码"，我给你标准模板

## 权限检查清单

### 1. 功能权限检查

| 检查项 | 说明 | 严重程度 |
|--------|------|----------|
| 认证检查 | 敏感接口是否都要求登录 | High |
| 权限类配置 | ViewSet/APIView 是否设置了 permission_classes | High |
| 权限粒度 | 不同操作是否有不同权限 | Medium |
| 默认拒绝 | 是否默认拒绝，明确允许 | High |
| Admin 权限 | Django Admin 是否只允许管理员 | High |
| 方法级权限 | 危险操作（删除等）是否有权限二次检查 | High |

### 2. 数据权限检查（最容易出问题）

| 检查项 | 说明 | 严重程度 |
|--------|------|----------|
| 水平越权 | 用户A能否访问/修改用户B的数据 | Critical |
| 垂直越权 | 普通用户能否访问管理员功能 | Critical |
| 对象所有权 | 是否检查了资源的所属关系 | Critical |
| 组织隔离 | 不同学校/班级/组织的数据是否隔离 | Critical |
| 查询集过滤 | list 接口是否按权限过滤 queryset | High |
| 详情接口权限 | retrieve 是否检查对象权限 | High |
| 批量操作权限 | 批量操作是否逐条检查权限 | High |

### 3. 常见越权漏洞模式

#### 漏洞模式 1: 直接用 ID 查，不检查所有者

```python
# ❌ 危险：水平越权
@login_required
def get_order(request, order_id):
    order = Order.objects.get(id=order_id)  # 任何人只要知道ID就能看
    return Response(...)

# ✅ 正确：加上所有者过滤
@login_required
def get_order(request, order_id):
    order = get_object_or_404(Order, id=order_id, user=request.user)
    return Response(...)
```

#### 漏洞模式 2: list 接口不过滤

```python
# ❌ 危险：返回所有人的数据
class OrderViewSet(viewsets.ModelViewSet):
    queryset = Order.objects.all()  # 所有订单都返回了

# ✅ 正确：按用户过滤
class OrderViewSet(viewsets.ModelViewSet):
    def get_queryset(self):
        qs = Order.objects.all()
        if not self.request.user.is_staff:
            qs = qs.filter(user=self.request.user)  # 普通用户只看自己的
        return qs
```

#### 漏洞模式 3: 更新时不检查权限

```python
# ❌ 危险：只要登录就能改任何人的
def update_order(request, order_id):
    order = Order.objects.get(id=order_id)
    order.status = request.data['status']  # 直接改了
    order.save()

# ✅ 正确：先检查所有权
def update_order(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if order.user != request.user and not request.user.is_staff:
        raise PermissionDenied
    # ... 再修改
```

#### 漏洞模式 4: 通过外键绕过

```python
# ❌ 危险：通过子资源绕过父资源权限
class OrderItemViewSet(viewsets.ModelViewSet):
    queryset = OrderItem.objects.all()
    # 用户可能通过 order_item 猜到其他用户的 order

# ✅ 正确：通过父资源过滤
def get_queryset(self):
    return OrderItem.objects.filter(order__user=self.request.user)
```

#### 漏洞模式 5: 批量操作漏检

```python
# ❌ 危险：批量删除只传ID列表，不检查所有权
@action(detail=False, methods=['post'])
def batch_delete(self, request):
    ids = request.data.get('ids', [])
    Order.objects.filter(id__in=ids).delete()  # 全删了！

# ✅ 正确：先过滤有权限的
@action(detail=False, methods=['post'])
def batch_delete(self, request):
    ids = request.data.get('ids', [])
    qs = Order.objects.filter(id__in=ids, user=request.user)  # 只删自己的
    qs.delete()
```

## 权限代码模板

### 模板 1: 基础对象权限类

```python
# permissions.py
from rest_framework import permissions


class IsOwnerOrReadOnly(permissions.BasePermission):
    """
    对象级权限：所有者可修改，其他人只读
    """
    def has_object_permission(self, request, view, obj):
        # 读权限对所有认证用户开放
        if request.method in permissions.SAFE_METHODS:
            return True
        # 写权限需要是所有者或管理员
        return obj.owner == request.user or request.user.is_staff


class IsOwner(permissions.BasePermission):
    """只有所有者可以访问和操作"""
    def has_object_permission(self, request, view, obj):
        return obj.owner == request.user


class IsAdminOrOwner(permissions.BasePermission):
    """管理员或所有者"""
    def has_object_permission(self, request, view, obj):
        return request.user.is_staff or obj.owner == request.user
```

### 模板 2: 多级角色权限（教辅平台场景）

```python
# permissions.py
from rest_framework import permissions

# 角色层级：平台管理员 > 学校管理员 > 老师 > 学生
ROLE_HIERARCHY = {
    'platform_admin': 100,
    'school_admin': 80,
    'teacher': 50,
    'student': 10,
}


class RoleRequired(permissions.BasePermission):
    """要求最低角色级别"""
    
    def __init__(self, min_role=None):
        self.min_role = min_role
    
    def __call__(self):
        return self
    
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # 平台管理员拥有所有权限
        if request.user.role == 'platform_admin':
            return True
        
        if not self.min_role:
            return True
        
        user_level = ROLE_HIERARCHY.get(request.user.role, 0)
        required_level = ROLE_HIERARCHY.get(self.min_role, 0)
        return user_level >= required_level


class SchoolDataPermission(permissions.BasePermission):
    """
    学校数据隔离权限
    
    - 平台管理员: 所有学校
    - 学校管理员: 自己学校
    - 老师: 自己学校
    - 学生: 自己学校
    """
    
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated
    
    def has_object_permission(self, request, view, obj):
        user = request.user
        
        # 平台管理员通过
        if user.role == 'platform_admin':
            return True
        
        # 其他角色必须同校
        if hasattr(obj, 'school_id'):
            return obj.school_id == user.school_id
        if hasattr(obj, 'school'):
            return obj.school_id == user.school_id
        
        return False


class TeacherClassPermission(permissions.BasePermission):
    """老师只能管理自己班级的数据"""
    
    def has_object_permission(self, request, view, obj):
        user = request.user
        
        if user.role == 'platform_admin':
            return True
        if user.role == 'school_admin' and obj.school_id == user.school_id:
            return True
        if user.role == 'teacher':
            # 检查是否是自己班级的
            return obj.class_id in user.teacher_classes.values_list('id', flat=True)
        
        return False
```

### 模板 3: 查询集数据隔离（最重要！）

```python
# mixins.py
from django.db import models


class SchoolDataMixin:
    """
    学校数据隔离 Mixin
    
    自动按学校过滤查询集，确保不同学校数据互不可见
    """
    
    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        
        if not user.is_authenticated:
            return qs.none()
        
        # 平台管理员看全部
        if user.role == 'platform_admin':
            return qs
        
        # 其他角色按学校过滤
        # 假设模型有 school 或 school_id 字段
        if hasattr(qs.model, 'school_id'):
            return qs.filter(school_id=user.school_id)
        if hasattr(qs.model, 'school'):
            return qs.filter(school_id=user.school_id)
        
        # 如果模型没有学校字段，返回空（安全起见）
        return qs.none()


class OwnerDataMixin:
    """
    所有者数据隔离 Mixin
    
    用户只能看到自己的数据，管理员看全部
    """
    
    owner_field = 'user'  # 所有者字段名
    
    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        
        if not user.is_authenticated:
            return qs.none()
        
        if user.is_staff or user.role in ['platform_admin', 'school_admin']:
            return qs
        
        # 普通用户只看自己的
        return qs.filter(**{self.owner_field: user})
```

### 模板 4: 权限装饰器（函数视图用）

```python
# decorators.py
from functools import wraps
from django.core.exceptions import PermissionDenied


def require_role(role):
    """要求用户具有指定角色（或更高）"""
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                raise PermissionDenied
            
            role_levels = {'platform_admin': 100, 'school_admin': 80, 'teacher': 50, 'student': 10}
            user_level = role_levels.get(request.user.role, 0)
            required_level = role_levels.get(role, 0)
            
            if user_level < required_level:
                raise PermissionDenied
            
            return view_func(request, *args, **kwargs)
        return wrapped
    return decorator


def check_object_owner(owner_field='user'):
    """
    检查对象所有权
    
    用法:
        @check_object_owner('user')
        def my_view(request, obj_id):
            ...
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            # 从 kwargs 中获取对象（视图需要先查询）
            # 这个装饰器通常和 get_object_or_404 配合
            return view_func(request, *args, **kwargs)
        return wrapped
    return decorator
```

### 模板 5: 字段级权限（Serializer）

```python
# serializers.py
from rest_framework import serializers


class PermissionBasedFieldMixin:
    """
    基于权限的字段控制
    
    根据用户角色决定哪些字段可见/可写
    """
    
    # 需要管理员才能看到的字段
    admin_only_fields = []
    # 只读字段（普通用户不能改）
    read_only_for_non_admin = []
    
    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get('request')
        user = request.user if request else None
        
        if not user or not user.is_authenticated:
            return fields
        
        # 非管理员移除管理员字段
        if not (user.is_staff or user.role == 'platform_admin'):
            for field_name in self.admin_only_fields:
                if field_name in fields:
                    del fields[field_name]
        
        # 非管理员字段设为只读
        if not (user.is_staff or user.role == 'platform_admin'):
            for field_name in self.read_only_for_non_admin:
                if field_name in fields:
                    fields[field_name].read_only = True
        
        return fields


# 使用示例
class UserSerializer(PermissionBasedFieldMixin, serializers.ModelSerializer):
    admin_only_fields = ['is_active', 'role', 'date_joined']
    read_only_for_non_admin = ['username', 'email']
    
    class Meta:
        model = User
        fields = '__all__'
```

## 权限审查快速检查步骤

拿到一段代码，按这个顺序查：

1. **先看 list 接口的 get_queryset** — 有没有按用户/学校/组织过滤？
2. **再看 detail 接口的对象权限** — 有没有 has_object_permission 或手动检查？
3. **看 create/update/delete** — 有没有权限检查？创建时有没有设置所有者？
4. **看自定义 action** — 批量操作、特殊操作有没有权限检查？
5. **看嵌套资源** — 子资源有没有继承父资源的权限？
6. **看 Serializer** — 敏感字段有没有按权限隐藏？

## 常见的权限误区

1. ❌ "我加了 login_required 就安全了" — 不对，还有水平越权
2. ❌ "前端按钮藏起来了用户就点不到" — 不对，API 必须自己校验
3. ❌ "用了 Django Guardian 就没问题了" — 不对，用错了一样有漏洞
4. ❌ "list 接口加个过滤就行，detail 不用查" — 不对，detail 也要查
5. ❌ "管理员能看所有，所以不用过滤" — 不对，学校管理员也只能看自己学校
