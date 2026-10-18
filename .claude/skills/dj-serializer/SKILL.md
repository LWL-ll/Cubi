---
name: "dj-serializer"
description: "DRF Serializer 审查与优化。检查 Serializer 的性能问题（N+1、冗余字段）、设计问题（分层设计、验证逻辑），给出优化方案和最佳实践。包括 N+1 检测、分层设计、字段级权限、条件序列化等。触发：列表接口慢时、Serializer 结构混乱时、需要字段权限控制时。"
---

# dj-serializer - Serializer 优化器

## 用法

把你的 Serializer 代码贴给我，说"用 dj-serializer 优化一下"，我帮你检查问题并给出优化后的代码。

---

## 一、常见问题与优化

### 问题 1: N+1 查询（Serializer 版）

**❌ 有问题的代码：**
```python
class BookSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source='author.name')  # 每本书查一次作者！
    
    class Meta:
        model = Book
        fields = '__all__'

# 视图里没做 select_related
class BookViewSet(viewsets.ModelViewSet):
    queryset = Book.objects.all()
    serializer_class = BookSerializer
```

结果：列表有 20 本书，就产生 **1 + 20 = 21 次查询**

**✅ 优化方案：**

视图层加 `select_related` / `prefetch_related`：
```python
class BookViewSet(viewsets.ModelViewSet):
    
    def get_queryset(self):
        qs = Book.objects.all()
        if self.action in ['list', 'retrieve']:
            qs = qs.select_related('author')  # 一对一/外键
            qs = qs.prefetch_related('tags')   # 多对多
        return qs
```

---

### 问题 2: 一个 Serializer 走天下

**❌ 有问题的代码：**
```python
class BookSerializer(serializers.ModelSerializer):
    """所有场景都用这一个"""
    class Meta:
        model = Book
        fields = '__all__'  # 列表页也返回所有字段，浪费流量
```

问题：
- 列表页用详情的序列化器，字段太多
- 创建和更新用同一个，有些字段创建后不能改
- 不同角色看到的字段一样，没有权限控制

**✅ 优化方案：分层设计**

```python
class BookListSerializer(serializers.ModelSerializer):
    """列表用：精简字段"""
    author_name = serializers.CharField(source='author.name', read_only=True)
    
    class Meta:
        model = Book
        fields = [
            'id', 'title', 'author_name', 'status',
            'created_at',
        ]


class BookDetailSerializer(serializers.ModelSerializer):
    """详情用：完整字段 + 嵌套数据"""
    author = AuthorSerializer(read_only=True)
    tags = TagSerializer(many=True, read_only=True)
    
    class Meta:
        model = Book
        fields = '__all__'


class BookCreateSerializer(serializers.ModelSerializer):
    """创建用：只有创建时需要的字段"""
    class Meta:
        model = Book
        fields = ['title', 'content', 'author', 'tags']


class BookUpdateSerializer(serializers.ModelSerializer):
    """更新用：可修改的字段"""
    class Meta:
        model = Book
        fields = ['title', 'content', 'status', 'tags']
        read_only_fields = ['author']  # 创建后不能改作者
```

视图里动态选择：
```python
def get_serializer_class(self):
    if self.action == 'list':
        return BookListSerializer
    elif self.action == 'create':
        return BookCreateSerializer
    elif self.action in ['update', 'partial_update']:
        return BookUpdateSerializer
    return BookDetailSerializer
```

---

### 问题 3: SerializerMethodField 滥用

**❌ 有问题的代码：**
```python
class BookSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()
    tag_names = serializers.SerializerMethodField()
    comment_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Book
        fields = '__all__'
    
    def get_author_name(self, obj):
        return obj.author.name  # 每次都查！
    
    def get_tag_names(self, obj):
        return [t.name for t in obj.tags.all()]  # 每次都查！
    
    def get_comment_count(self, obj):
        return obj.comments.count()  # 每次都查！
```

每个 SerializerMethodField 如果访问关联对象，就是 N+1！

**✅ 优化方案：**

1. **能用 source 就不用 SerializerMethodField**
```python
# ❌ 慢
author_name = serializers.SerializerMethodField()
def get_author_name(self, obj):
    return obj.author.name

# ✅ 快（配合 select_related）
author_name = serializers.CharField(source='author.name', read_only=True)
```

2. **嵌套 Serializer + prefetch_related**
```python
# ❌ 慢，还有 N+1
tag_names = serializers.SerializerMethodField()
def get_tag_names(self, obj):
    return [t.name for t in obj.tags.all()]

# ✅ 快（配合 prefetch_related）
tags = TagSerializer(many=True, read_only=True)
```

3. **注解统计代替 count()**
```python
# ❌ 每个对象查一次 COUNT
comment_count = serializers.SerializerMethodField()
def get_comment_count(self, obj):
    return obj.comments.count()

# ✅ 用 annotate，一次查询搞定
# 视图层：
from django.db.models import Count
qs = Book.objects.annotate(comment_count=Count('comments'))

# Serializer 直接用字段：
comment_count = serializers.IntegerField(read_only=True)
```

---

### 问题 4: 字段级权限控制

**场景**: 普通用户看不到某些字段，管理员才能看到

**❌ 不好的做法：**
```python
# 在视图里手动删字段
def list(self, request, *args, **kwargs):
    response = super().list(request, *args, **kwargs)
    if not request.user.is_staff:
        for item in response.data['results']:
            del item['secret_field']  # 太麻烦了
    return response
```

**✅ 方案 1: Mixin 动态控制字段**

```python
class PermissionFieldMixin:
    """
    基于权限的字段控制 Mixin
    
    用法：
        class UserSerializer(PermissionFieldMixin, serializers.ModelSerializer):
            admin_only_fields = ['salary', 'id_card']
            read_only_for_normal = ['role']
    """
    
    # 管理员才能看到的字段
    admin_only_fields = []
    # 普通用户只读的字段
    read_only_for_normal = []
    
    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get('request')
        user = request.user if request else None
        
        if not user or not user.is_authenticated:
            # 未登录，移除管理员字段
            for name in self.admin_only_fields:
                fields.pop(name, None)
            return fields
        
        is_admin = user.is_staff or getattr(user, 'role', None) == 'platform_admin'
        
        if not is_admin:
            # 移除管理员专属字段
            for name in self.admin_only_fields:
                fields.pop(name, None)
            # 设为只读
            for name in self.read_only_for_normal:
                if name in fields:
                    fields[name].read_only = True
        
        return fields


# 使用示例
class UserSerializer(PermissionFieldMixin, serializers.ModelSerializer):
    admin_only_fields = ['is_active', 'last_login', 'date_joined']
    read_only_for_normal = ['username', 'email', 'role']
    
    class Meta:
        model = User
        fields = '__all__'
```

**✅ 方案 2: 不同角色用不同 Serializer**

```python
def get_serializer_class(self):
    if self.request.user.is_staff:
        return AdminBookSerializer
    return BookSerializer
```

---

### 问题 5: 条件序列化

**场景**: 有些字段只在特定条件下返回

**✅ 方案: 动态字段**

```python
class DynamicFieldsMixin:
    """
    动态字段 Mixin
    
    支持通过查询参数控制返回字段：
    GET /api/books/?fields=id,title,author
    GET /api/books/?exclude=content
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        request = self.context.get('request')
        if not request:
            return
        
        # 通过 fields 参数指定返回字段
        fields_param = request.query_params.get('fields')
        if fields_param:
            allowed = set(fields_param.split(','))
            existing = set(self.fields.keys())
            for field_name in existing - allowed:
                self.fields.pop(field_name)
        
        # 通过 exclude 参数排除字段
        exclude_param = request.query_params.get('exclude')
        if exclude_param:
            excluded = set(exclude_param.split(','))
            for field_name in excluded:
                self.fields.pop(field_name, None)
```

---

### 问题 6: 验证逻辑重复

**❌ 有问题的代码：**
```python
class BookCreateSerializer(serializers.ModelSerializer):
    def validate_title(self, value):
        if len(value) < 5:
            raise serializers.ValidationError("标题太短")
        return value

class BookUpdateSerializer(serializers.ModelSerializer):
    def validate_title(self, value):
        # 复制粘贴了一遍同样的验证
        if len(value) < 5:
            raise serializers.ValidationError("标题太短")
        return value
```

**✅ 方案: 抽取公共验证**

```python
# 方法 1: Mixin
class BookValidationMixin:
    def validate_title(self, value):
        value = value.strip()
        if len(value) < 5:
            raise serializers.ValidationError("标题至少需要5个字符")
        if len(value) > 200:
            raise serializers.ValidationError("标题不能超过200个字符")
        return value

class BookCreateSerializer(BookValidationMixin, serializers.ModelSerializer):
    ...

class BookUpdateSerializer(BookValidationMixin, serializers.ModelSerializer):
    ...


# 方法 2: Model 层验证（推荐）
class Book(models.Model):
    title = models.CharField(max_length=200)
    
    def clean(self):
        if len(self.title.strip()) < 5:
            raise ValidationError({'title': '标题至少需要5个字符'})

# Serializer 调用 full_clean
class BookSerializer(serializers.ModelSerializer):
    def validate(self, attrs):
        instance = Book(**attrs)
        instance.clean()  # 复用 Model 层验证
        return attrs
```

---

### 问题 7: 嵌套 Serializer 写入复杂

**场景**: 创建时传 ID，返回时返回完整对象

**✅ 方案: 读写分离**

```python
class BookDetailSerializer(serializers.ModelSerializer):
    """读：返回完整的作者对象"""
    author = AuthorSerializer(read_only=True)
    
    class Meta:
        model = Book
        fields = '__all__'


class BookCreateSerializer(serializers.ModelSerializer):
    """写：只传作者ID"""
    # author 字段默认就是 ID 写入
    
    class Meta:
        model = Book
        fields = ['title', 'content', 'author']
    
    def to_representation(self, instance):
        """创建成功后返回详情格式"""
        return BookDetailSerializer(instance, context=self.context).data
```

---

## 二、Serializer 性能优化清单

- [ ] 列表接口用精简的 ListSerializer
- [ ] 所有关联字段在视图层做了 select_related / prefetch_related
- [ ] 尽量用 source 代替 SerializerMethodField
- [ ] 统计数据用 annotate 代替 SerializerMethodField 里的 count()
- [ ] 大字段（content, description）列表页不返回
- [ ] 嵌套层级不要太深（最多 2-3 层）
- [ ] 列表页不用嵌套太多关联，详情页再用

---

## 三、验证最佳实践

### 字段级验证 vs 对象级验证

| 场景 | 用哪个 |
|------|--------|
| 只验证一个字段 | validate_xxx |
| 字段间有关联（开始时间 < 结束时间） | validate |
| 数据库唯一性约束 | unique=True + validate |
| 复杂业务规则 | validate 或 Model.clean |

### 常见验证模式

```python
class MySerializer(serializers.Serializer):
    
    # 1. 长度限制
    title = serializers.CharField(min_length=5, max_length=200)
    
    # 2. 取值范围
    score = serializers.IntegerField(min_value=0, max_value=100)
    
    # 3. 正则验证
    phone = serializers.RegexField(r'^1[3-9]\d{9}$', error_message='手机号格式错误')
    
    # 4. 选项验证
    status = serializers.ChoiceField(choices=['draft', 'published'])
    
    # 5. 自定义验证器
    email = serializers.EmailField()
    url = serializers.URLField()
    uuid = serializers.UUIDField()
```

---

## 四、Serializer 设计模式

### 模式 1: 请求/响应分离

```python
# 请求用：严格验证，只接受需要的字段
class BookCreateRequest(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ['title', 'content']

# 响应用：返回完整信息
class BookResponse(serializers.ModelSerializer):
    author = AuthorSerializer()
    class Meta:
        model = Book
        fields = '__all__'
```

### 模式 2: 基础 + 扩展

```python
class BookBaseSerializer(serializers.ModelSerializer):
    """公共字段和验证"""
    def validate_title(self, value):
        ...

class BookListSerializer(BookBaseSerializer):
    """列表用"""
    class Meta(BookBaseSerializer.Meta):
        fields = ['id', 'title', 'status', 'created_at']

class BookDetailSerializer(BookBaseSerializer):
    """详情用"""
    class Meta(BookBaseSerializer.Meta):
        fields = '__all__'
```

### 模式 3: 包装器模式（统一响应格式）

```python
def success_response(data=None, message='success'):
    return {
        'code': 0,
        'message': message,
        'data': data,
    }

# 在视图里用
def list(self, request, *args, **kwargs):
    queryset = self.filter_queryset(self.get_queryset())
    page = self.paginate_queryset(queryset)
    
    if page is not None:
        serializer = self.get_serializer(page, many=True)
        return success_response(self.get_paginated_response(serializer.data).data)
    
    serializer = self.get_serializer(queryset, many=True)
    return Response(success_response(serializer.data))
```
