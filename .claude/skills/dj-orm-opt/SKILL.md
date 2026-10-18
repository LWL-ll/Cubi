---
name: "dj-orm-opt"
description: "Django ORM 查询性能审查。分析你的查询代码，找出性能问题（N+1、全表扫描、低效查询、索引缺失），给出优化方案和最佳实践参考。触发：列表页慢时、写完复杂查询后、性能调优时。"
---

# dj-orm-opt - ORM 查询优化器

## 用法

- **查询审查**: 把你的 QuerySet 代码贴给我，说"用 dj-orm-opt 看一下"
- **优化编写**: 说"用 dj-orm-opt 帮我写一个查询 XX 的代码"
- **索引建议**: 说"用 dj-orm-opt 给这个 Model 推荐索引"

---

## 一、常见性能问题及优化

### 问题 1: N+1 查询（最常见）

**❌ 有问题的代码：**
```python
# 循环中访问关联字段，每次都查一次数据库
books = Book.objects.all()
for book in books:
    print(book.author.name)  # 每本书查一次 author 表！
# 1 + N 次查询（1次查书，N次查作者）
```

**✅ 优化方案：**

| 关联类型 | 优化方法 | 说明 |
|----------|----------|------|
| 一对一 / 外键 | `select_related()` | JOIN 查询，一次查出 |
| 多对多 / 反向外键 | `prefetch_related()` | 单独查一次，Python 中组装 |

```python
# 外键关联
books = Book.objects.select_related('author').all()
for book in books:
    print(book.author.name)  # 不额外查询

# 多对多关联
books = Book.objects.prefetch_related('tags').all()
for book in books:
    print(book.tags.all())  # 不额外查询

# 两者结合
books = Book.objects.select_related('author').prefetch_related('tags').all()
```

**深层嵌套 N+1：**
```python
# ❌ 嵌套的 N+1
classes = Class.objects.all()
for cls in classes:
    for student in cls.student_set.all():
        print(student.name)

# ✅ 用 prefetch_related
classes = Class.objects.prefetch_related('student_set').all()
```

---

### 问题 2: 取了不需要的数据

**❌ 有问题的代码：**
```python
# 取了所有字段，但只用了 title
books = Book.objects.all()
for book in books:
    print(book.title)

# 用 len() 触发全量加载
count = len(Book.objects.all())

# 用 if queryset 判断存在
if Book.objects.filter(title='xxx'):
    ...
```

**✅ 优化方案：**

```python
# 只取需要的字段
books = Book.objects.only('title').all()
books = Book.objects.values('id', 'title')  # 返回字典
books = Book.objects.values_list('id', 'title')  # 返回元组
books = Book.objects.defer('content')  # 排除大字段

# 计数
count = Book.objects.count()  # SELECT COUNT(*)

# 判断存在
if Book.objects.filter(title='xxx').exists():
    ...
```

---

### 问题 3: 循环过滤 / Python 层过滤

**❌ 有问题的代码：**
```python
# 全查出来再 Python 过滤
books = Book.objects.all()
python_books = [b for b in books if b.category == 'python']

# 循环查
ids = [1, 2, 3]
books = []
for id in ids:
    books.append(Book.objects.get(id=id))
```

**✅ 优化方案：**
```python
# 数据库层面过滤
python_books = Book.objects.filter(category='python')

# 批量查询
books = Book.objects.filter(id__in=[1, 2, 3])
```

---

### 问题 4: 缺少索引

**❌ 有问题的代码：**
```python
# 经常按这些字段过滤，但没有索引
Book.objects.filter(author_id=123)
Book.objects.filter(status='published')
Book.objects.filter(created_at__gte='2024-01-01')
Book.objects.order_by('-created_at')
```

**✅ 优化方案：**

```python
# models.py 中加索引
class Book(models.Model):
    title = models.CharField(max_length=200, db_index=True)  # 单列索引
    author = models.ForeignKey(Author, on_delete=models.CASCADE, db_index=True)
    status = models.CharField(max_length=20, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        indexes = [
            # 联合索引（按过滤频率排序字段）
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['author', 'status']),
        ]
```

**索引设计原则：**
1. 经常出现在 `WHERE` 条件里的字段加索引
2. 经常出现在 `ORDER BY` 里的字段加索引
3. 联合索引遵循最左前缀原则
4. 区分度高的字段放前面
5. 索引不是越多越好，会减慢写入

---

### 问题 5: 批量操作低效

**❌ 有问题的代码：**
```python
# 循环创建
for title in titles:
    Book.objects.create(title=title, author_id=1)

# 循环更新
for book in books:
    book.status = 'published'
    book.save()
```

**✅ 优化方案：**
```python
# 批量创建
books = [
    Book(title=title, author_id=1)
    for title in titles
]
Book.objects.bulk_create(books)

# 批量更新
Book.objects.filter(author_id=1).update(status='published')

# 批量更新（不同值不同更新量，用 bulk_update）
books = list(Book.objects.filter(author_id=1))
for i, book in enumerate(books):
    book.view_count = i
Book.objects.bulk_update(books, ['view_count'])
```

---

## 二、查询优化技巧

### 2.1 select_related vs prefetch_related

| 特性 | select_related | prefetch_related |
|------|---------------|------------------|
| 适用关系 | 一对一、外键 | 多对多、反向外键 |
| 实现方式 | SQL JOIN | 单独查询 + Python 组装 |
| 查询次数 | 1 次 | 主查询 + 1次关联查询 |
| 过滤关联 | 不支持（只能简单关联） | 支持 Prefetch 对象自定义 |

```python
# 自定义 prefetch_related 查询
from django.db.models import Prefetch

# 只预取已发布的评论
prefetch = Prefetch(
    'comments',
    queryset=Comment.objects.filter(status='approved'),
    to_attr='approved_comments'
)

posts = Post.objects.prefetch_related(prefetch).all()
```

### 2.2 注解和聚合

```python
from django.db.models import Count, Sum, Avg, F, Q, Case, When, Value

# 统计每个作者的书的数量
authors = Author.objects.annotate(book_count=Count('book'))

# 带条件的统计
authors = Author.objects.annotate(
    published_count=Count('book', filter=Q(book__status='published'))
)

# F 表达式：用字段值做计算和比较
# 库存 < 安全库存的产品
Product.objects.filter(stock__lt=F('safety_stock'))

# 自增（原子操作）
Book.objects.filter(id=1).update(view_count=F('view_count') + 1)

# Case When 条件表达式
books = Book.objects.annotate(
    level=Case(
        When(score__gte=90, then=Value('优秀')),
        When(score__gte=80, then=Value('良好')),
        When(score__gte=60, then=Value('及格')),
        default=Value('不及格'),
        output_field=models.CharField()
    )
)
```

### 2.3 分页和切片

```python
# 分页 - 用 Django Paginator 或 DRF Pagination
from django.core.paginator import Paginator

paginator = Paginator(Book.objects.all(), 20)
page = paginator.get_page(1)

# 切片（在数据库层面 LIMIT）
first_10 = Book.objects.all()[:10]  # LIMIT 10
next_10 = Book.objects.all()[10:20]  # LIMIT 10 OFFSET 10
```

### 2.4 大查询集处理

```python
# 数据量太大时用 iterator() 避免内存爆炸
# 普通：一次性加载所有到内存
for book in Book.objects.all():
    process(book)

# 优化：分批次加载
for book in Book.objects.iterator(chunk_size=1000):
    process(book)

# 按 id 范围分页处理（大数据量导出等场景）
def process_all_books():
    last_id = 0
    batch_size = 1000
    
    while True:
        batch = Book.objects.filter(id__gt=last_id).order_by('id')[:batch_size]
        if not batch:
            break
        
        for book in batch:
            process(book)
        
        last_id = batch.last().id
```

### 2.5 原始 SQL 的正确用法

```python
# 参数化查询（安全）
Book.objects.raw('SELECT * FROM book WHERE author_id = %s', [author_id])

# 执行自定义 SQL
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute(
        'SELECT title, view_count FROM book WHERE author_id = %s',
        [author_id]
    )
    rows = cursor.fetchall()
```

---

## 三、查询性能检查清单

拿到一段查询代码，按这个顺序检查：

1. **有没有 N+1？** — 循环中有没有访问关联字段？
   - 加 `select_related` 或 `prefetch_related`

2. **取了多余字段吗？** — 是不是只用了几个字段却取了所有？
   - 用 `only()` / `defer()` / `values()`

3. **计数/判断存在用对了吗？**
   - 用 `.count()` 不用 `len()`
   - 用 `.exists()` 不用 `if queryset`

4. **循环里有查询吗？** — 能不能提到循环外面？
   - 用 `in_bulk()` / `id__in` 批量查

5. **过滤在数据库层还是 Python 层？**
   - 尽量在数据库层 filter

6. **批量操作用了循环吗？**
   - 用 `bulk_create` / `bulk_update` / `update()`

7. **索引够吗？** — 过滤和排序字段有没有索引？
   - 在 Meta.indexes 里加

8. **数据量大会不会 OOM？**
   - 用 `iterator()` 或按 id 分页

---

## 四、性能调试工具

### 4.1 Django Debug Toolbar

最常用的工具，能看到每个页面的查询次数、时间、SQL语句。

### 4.2 查询日志

```python
# settings.py 开发环境打开查询日志
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'loggers': {
        'django.db.backends': {
            'level': 'DEBUG',
            'handlers': ['console'],
        },
    },
}
```

### 4.3 代码中统计查询次数

```python
from django.db import connection, reset_queries

reset_queries()

# ... 执行你的代码 ...

print(f"查询次数: {len(connection.queries)}")
for q in connection.queries:
    print(f"[{q['time']}s] {q['sql']}")
```

---

## 五、常见查询模式参考

### Top N 查询

```python
# 取前10名
top_students = Student.objects.order_by('-score')[:10]
```

### 分组统计

```python
from django.db.models import Count

# 每个班级的学生数
classes = Class.objects.annotate(student_count=Count('student'))
```

### 去重查询

```python
# 去重的分类
categories = Book.objects.values_list('category', flat=True).distinct()
```

### 存在性检查（代替 JOIN）

```python
# 查询有评论的文章（用 EXISTS 比 JOIN 快）
posts = Post.objects.filter(
    pk__in=Comment.objects.values('post_id')
)

# 或者用 Exists
from django.db.models import Exists, OuterRef

posts = Post.objects.annotate(
    has_comments=Exists(
        Comment.objects.filter(post_id=OuterRef('pk'))
    )
).filter(has_comments=True)
```

### 随机抽样

```python
# 小数据量
random_book = Book.objects.order_by('?').first()

# 大数据量（更高效）
import random
count = Book.objects.count()
random_index = random.randint(0, count - 1)
random_book = Book.objects.all()[random_index]
```
