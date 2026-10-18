---
name: "dj-code-safety"
description: "工业级 Django 代码安全审查。检查代码中的安全漏洞和风险，按严重程度分级，给出修复方案。覆盖 OWASP Top 10、Django 特有漏洞、注入类、XSS、CSRF、文件上传、权限绕过、敏感信息泄露等。触发：写完代码要检查安全时、代码评审时、上线前安全检查。"
---

# dj-code-safety - 工业级代码安全审查

## 用法

把你写的代码贴给我，说"用 dj-code-safety 审查"，我会从安全角度全面检查，找出漏洞并给出修复方案。

---

## 审查维度与严重等级

### 严重等级定义

| 等级 | 标记 | 说明 | 修复时限 |
|------|------|------|----------|
| 🔴 Critical | 严重 | 可直接导致数据泄露、权限绕过、远程代码执行 | 立即修复 |
| 🟠 High | 高危 | 常见利用方式可突破安全边界 | 24小时内修复 |
| 🟡 Medium | 中危 | 特定条件下可利用，影响有限 | 一周内修复 |
| 🟢 Low | 低危 | 难以利用或影响极小 | 版本迭代修复 |
| 💡 Info | 信息 | 最佳实践建议，无直接风险 | 参考改进 |

---

## 一、注入类漏洞（最高优先级）

### 1.1 SQL 注入

**危险模式识别：**

```python
# ❌ CRITICAL: 字符串拼接 SQL
Model.objects.raw(f"SELECT * FROM table WHERE id={user_input}")
Model.objects.raw("SELECT ... WHERE id=%s" % user_input)
cursor.execute(f"SELECT ... {user_input}")
cursor.execute("SELECT ... " + user_input)

# ❌ CRITICAL: f-string + format
Model.objects.extra(where=[f"name='{user_input}'"])
RawSQL(f"SELECT ... {user_input}", [])

# ❌ HIGH: 列表推导式中的拼接
conditions = [f"field_{i}={val}" for i, val in enumerate(vals)]
```

**安全模式：**
```python
# ✅ ORM 查询（参数化）
Model.objects.filter(id=user_input)
Model.objects.filter(name__icontains=keyword)

# ✅ 参数化 raw SQL
Model.objects.raw("SELECT ... WHERE id=%s", [user_input])
cursor.execute("SELECT ... WHERE id=%s", [user_input])
```

**审查要点：**
- [ ] 搜索 `.raw()`, `.extra()`, `RawSQL`, `cursor.execute`
- [ ] 检查 SQL 字符串是否使用了 f-string、%、+ 拼接用户输入
- [ ] 所有变量是否通过参数传递

---

### 1.2 命令注入

**危险模式识别：**

```python
# ❌ CRITICAL: 直接执行用户可控的命令
os.system(user_input)
os.popen(user_input)
subprocess.call(user_input, shell=True)
subprocess.Popen(user_input, shell=True)
commands.getoutput(user_input)

# ❌ CRITICAL: 部分可控
subprocess.call(f"ping {ip}", shell=True)
os.system(f"ls {path}")
```

**审查要点：**
- [ ] 搜索 `subprocess`, `os.system`, `os.popen`, `shell=True`
- [ ] 命令参数是否有用户输入
- [ ] 是否使用了 shell=True
- [ ] 输入是否做了严格的白名单验证

---

### 1.3 模板注入

**危险模式识别：**

```python
# ❌ HIGH: 动态渲染用户可控的模板字符串
from django.template import Template, Context
template = Template(user_input)  # 用户输入直接作为模板
result = template.render(Context({}))

# ❌ HIGH: from_string
from django.template import engines
django_engine = engines['django']
template = django_engine.from_string(user_input)
```

**审查要点：**
- [ ] 搜索 `Template(`, `from_string(`
- [ ] 模板内容是否用户可控
- [ ] 是否有沙箱隔离措施

---

## 二、XSS 跨站脚本

### 2.1 模板中的 XSS

**危险模式识别：**

```django
{# ❌ HIGH: 关闭自动转义 #}
{% autoescape off %}
    {{ user_content }}
{% endautoescape %}

{# ❌ HIGH: safe 过滤器 #}
{{ user_content|safe }}

{# ❌ MEDIUM: 属性中未加引号 #}
<div class={{ user_input }}>

{# ❌ MEDIUM: JavaScript 变量注入 #}
<script>
    var data = "{{ user_data }}";
</script>
```

### 2.2 Python 代码中的 XSS

**危险模式识别：**

```python
# ❌ HIGH: mark_safe 用户输入
from django.utils.safestring import mark_safe
mark_safe(user_input)
SafeString(user_input)

# ❌ HIGH: format_html 中拼接用户输入
from django.utils.html import format_html
format_html(f"<div>{user_input}</div>")  # f-string 先拼接了！

# ❌ MEDIUM: HttpResponse 返回 HTML
return HttpResponse(f"<h1>{user_input}</h1>")
```

**审查要点：**
- [ ] 搜索 `mark_safe`, `SafeString`, `|safe`, `autoescape off`
- [ ] 标记为安全的内容是否包含用户输入
- [ ] 是否有必要使用 safe，能否用模板渲染代替
- [ ] JavaScript 中嵌入的变量是否做了 JSON 转义

---

## 三、CSRF 跨站请求伪造

### 3.1 CSRF 防护检查

**危险模式识别：**

```python
# ❌ HIGH: 全局禁用 CSRF
MIDDLEWARE = [
    # 'django.middleware.csrf.CsrfViewMiddleware',  # 被注释掉了
]

# ❌ HIGH: 视图级豁免过多
@csrf_exempt
def my_view(request):
    ...

# ❌ MEDIUM: ensure_csrf_cookie 缺失
# 纯前端项目（Vue/React）如果 cookie 没种上，CSRF 会失效
```

**审查要点：**
- [ ] CsrfViewMiddleware 是否在 MIDDLEWARE 中
- [ ] 搜索 `csrf_exempt`，每个豁免是否有合理理由
- [ ] API 接口是否使用了 Token 认证替代 CSRF
- [ ] 所有修改操作（POST/PUT/PATCH/DELETE）是否有 CSRF 或 Token 保护
- [ ] 前端是否正确携带 CSRF Token

---

## 四、认证与会话安全

### 4.1 密码安全

**危险模式识别：**

```python
# ❌ CRITICAL: 明文存储密码
user.password = password

# ❌ CRITICAL: 自定义弱哈希
user.password = hashlib.md5(password.encode()).hexdigest()
user.password = hashlib.sha1(password.encode()).hexdigest()

# ❌ HIGH: 密码重置 Token 不安全
token = user.id + timestamp  # 可预测
```

**安全模式：**
```python
# ✅ 使用 Django 内置密码系统
user.set_password(password)
user.check_password(raw_password)

# ✅ 密码重置用默认的 token 生成器
from django.contrib.auth.tokens import default_token_generator
token = default_token_generator.make_token(user)
```

### 4.2 登录安全

**危险模式识别：**

```python
# ❌ HIGH: 错误消息区分用户是否存在
if not User.objects.filter(username=username).exists():
    return Response({"error": "用户不存在"})
if not user.check_password(password):
    return Response({"error": "密码错误"})

# ❌ HIGH: 无登录失败限制
# 用户可以无限尝试密码
```

**审查要点：**
- [ ] 密码是否使用 `set_password` / `check_password`
- [ ] 登录失败是否区分"用户不存在"和"密码错误"（应该不区分）
- [ ] 是否有登录速率限制（django-axes 等）
- [ ] Session Cookie 的 Secure/HttpOnly/SameSite 属性
- [ ] 登出是否清除会话
- [ ] 密码重置 Token 是否安全（不可预测、有过期时间）

---

## 五、访问控制与越权（最常出问题）

### 5.1 水平越权（IDOR）

**危险模式识别：**

```python
# ❌ CRITICAL: 直接按 ID 查询，不检查所有者
@login_required
def get_order(request, order_id):
    order = Order.objects.get(id=order_id)  # 知道ID就能看
    return Response(...)

# ❌ CRITICAL: 列表接口不过滤
class OrderViewSet(viewsets.ModelViewSet):
    queryset = Order.objects.all()  # 返回所有人的订单

# ❌ CRITICAL: 更新/删除不检查权限
@login_required
def delete_order(request, order_id):
    order = Order.objects.get(id=order_id)
    order.delete()  # 任何人都能删
```

**安全模式：**
```python
# ✅ 按所有者过滤
order = get_object_or_404(Order, id=order_id, user=request.user)

# ✅ 查询集过滤
def get_queryset(self):
    qs = Order.objects.all()
    if not self.request.user.is_staff:
        qs = qs.filter(user=self.request.user)
    return qs

# ✅ 对象级权限
def has_object_permission(self, request, view, obj):
    return obj.user_id == request.user.id or request.user.is_staff
```

### 5.2 垂直越权

**危险模式识别：**

```python
# ❌ CRITICAL: 前端传角色/权限，后端直接用
user.role = request.data.get('role')  # 用户可以把自己改成管理员

# ❌ HIGH: 隐藏按钮就觉得安全了
# 管理员功能只在前端藏了按钮，后端没加权限检查
```

### 5.3 批量操作越权

**危险模式识别：**

```python
# ❌ CRITICAL: 批量操作只传 ID 列表，不检查所有权
@action(detail=False, methods=['post'])
def batch_delete(self, request):
    ids = request.data.get('ids', [])
    Order.objects.filter(id__in=ids).delete()  # 全删了！
```

**审查要点（权限必查清单）：**

- [ ] **list 接口**: `get_queryset` 是否按用户/学校/组织过滤？
- [ ] **retrieve 接口**: 是否有 `has_object_permission` 或手动检查所有权？
- [ ] **create 接口**: 创建时是否服务端设置所有者，不信任前端传的 user_id？
- [ ] **update 接口**: 是否检查了修改权限？
- [ ] **delete 接口**: 是否检查了删除权限？
- [ ] **自定义 action**: 每个自定义操作都有权限检查吗？
- [ ] **批量操作**: 批量操作是否先过滤出有权限的记录再操作？
- [ ] **嵌套资源**: 子资源是否继承了父资源的权限？
- [ ] **角色字段**: 角色/权限字段是否能被普通用户修改？
- [ ] **管理员功能**: 管理员专属功能是否加了 is_staff / 角色检查？

---

## 六、文件上传安全

### 6.1 文件上传漏洞

**危险模式识别：**

```python
# ❌ CRITICAL: 只检查扩展名，不检查内容
if filename.endswith('.jpg'):
    save(file)

# ❌ CRITICAL: 文件存储在可执行目录
upload_to = 'static/uploads'  # static 目录会被 nginx 直接执行

# ❌ HIGH: 保留原始文件名
file.save(uploaded_file.name, uploaded_file)  # 可能有路径遍历

# ❌ HIGH: 没有大小限制
# 用户可以传超大文件打满磁盘
```

**安全检查清单：**
- [ ] 文件类型验证：扩展名白名单 + MIME type + 文件头三重验证
- [ ] 文件大小限制：settings 中配置 + 代码中二次检查
- [ ] 文件名随机化：用 UUID 等随机名称，不保留原始文件名
- [ ] 存储位置隔离：MEDIA_ROOT 和 STATIC_ROOT 分开
- [ ] 服务方式安全：独立域名、强制 Content-Disposition、禁止脚本执行
- [ ] 路径遍历防护：basename 处理，禁止 `../`
- [ ] 权限控制：上传和下载都有权限检查
- [ ] 病毒扫描：生产环境建议接入杀毒引擎

---

## 七、路径遍历

**危险模式识别：**

```python
# ❌ CRITICAL: 用户输入直接拼接路径
filename = request.GET.get('file')
file_path = os.path.join(MEDIA_ROOT, filename)
with open(file_path, 'r') as f:  # 可能读到 ../../etc/passwd
    ...

# ❌ HIGH: 文件下载接受路径参数
def download(request, path):
    file_path = os.path.join(MEDIA_ROOT, path)
    return FileResponse(open(file_path, 'rb'))
```

**安全模式：**
```python
# ✅ 通过数据库 ID 查找文件，不接受路径参数
document = get_object_or_404(Document, id=doc_id)
# 再检查权限
if not document.can_access(request.user):
    raise PermissionDenied

# ✅ 路径规范化 + 目录验证
import os
safe_filename = os.path.basename(user_input)  # 只取文件名部分
full_path = os.path.abspath(os.path.join(base_dir, safe_filename))
if not full_path.startswith(os.path.abspath(base_dir)):
    raise PermissionDenied  # 跳出了基目录
```

---

## 八、开放重定向

**危险模式识别：**

```python
# ❌ MEDIUM: 直接重定向到用户传的 next
def login(request):
    # ... 登录成功 ...
    next_url = request.GET.get('next')
    return redirect(next_url)  # 可能跳到钓鱼网站
```

**安全模式：**
```python
# ✅ 验证重定向 URL 是否是本站的
from django.utils.http import url_has_allowed_host_and_scheme

next_url = request.GET.get('next', '')
if next_url and url_has_allowed_host_and_scheme(
    url=next_url,
    allowed_hosts={request.get_host()},
    require_https=request.is_secure(),
):
    return redirect(next_url)
return redirect('home')
```

---

## 九、SSRF 服务端请求伪造

**危险模式识别：**

```python
# ❌ HIGH: 服务端请求用户提供的 URL
url = request.data.get('url')
response = requests.get(url)  # 可能被用来打内网
```

**审查要点：**
- [ ] 是否有服务端发起的 HTTP 请求
- [ ] URL 是否用户可控
- [ ] 是否限制了协议（只允许 http/https）
- [ ] 是否限制了域名白名单
- [ ] 是否禁止了内网 IP 访问

---

## 十、敏感信息泄露

### 10.1 日志泄露

```python
# ❌ MEDIUM: 日志里打密码、token
logger.info(f"User {username} login with password: {password}")
logger.debug(f"Token: {token}")

# ❌ LOW: 日志里打完整的用户信息
logger.info(f"User data: {user.__dict__}")
```

### 10.2 错误页面泄露

```python
# ❌ HIGH: 生产环境 DEBUG=True
# 会暴露堆栈、配置、数据库连接信息
DEBUG = True
```

### 10.3 API 响应泄露

```python
# ❌ MEDIUM: 返回了不需要的敏感字段
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = '__all__'  # 连 password_hash 都返回了！
```

**审查要点：**
- [ ] 日志中是否包含密码、密钥、Token、身份证号等敏感信息
- [ ] 生产环境 DEBUG 是否关闭
- [ ] 序列化器是否返回了敏感字段（password, password_hash, secret 等）
- [ ] 错误响应是否泄露了堆栈或内部信息
- [ ] SECRET_KEY、数据库密码等是否硬编码在代码里

---

## 十一、Django 配置安全

**配置审查清单：**

- [ ] DEBUG = False（生产环境）
- [ ] SECRET_KEY 从环境变量加载，不硬编码
- [ ] ALLOWED_HOSTS 是具体列表，不是通配符
- [ ] CSRF_COOKIE_SECURE = True（HTTPS 环境）
- [ ] SESSION_COOKIE_SECURE = True
- [ ] SESSION_COOKIE_HTTPONLY = True
- [ ] SECURE_SSL_REDIRECT = True
- [ ] SECURE_CONTENT_TYPE_NOSNIFF = True
- [ ] X_FRAME_OPTIONS = 'DENY' 或 'SAMEORIGIN'
- [ ] 数据库密码不硬编码
- [ ] 管理员路径不是默认的 /admin/（可选，增加攻击者成本）

---

## 十二、业务逻辑安全

### 12.1 竞态条件

```python
# ❌ HIGH: 先读再写，中间可能被改
account = Account.objects.get(user=request.user)
if account.balance >= amount:
    account.balance -= amount  # 并发请求可能扣两次
    account.save()
```

**安全模式：**
```python
# ✅ 用 F 表达式原子更新
from django.db.models import F
updated = Account.objects.filter(
    user=request.user,
    balance__gte=amount
).update(balance=F('balance') - amount)

if not updated:
    raise ValidationError("余额不足")
```

### 12.2 批量操作数据一致性

```python
# ❌ MEDIUM: 多步操作没有事务包裹
order.status = 'paid'
order.save()
user.points += 100
user.save()
# 如果中间报错，状态不一致
```

**安全模式：**
```python
# ✅ 事务包裹
from django.db import transaction

@transaction.atomic
def pay_order(order_id, user_id):
    order = Order.objects.select_for_update().get(id=order_id)
    user = User.objects.select_for_update().get(id=user_id)
    # ... 操作 ...
```

---

## 审查输出格式

```
=== 代码安全审查报告 ===

【概览】
- 审查文件: xxx.py / 代码范围
- 代码行数: xxx
- 发现问题: 🔴 Critical x  🟠 High x  🟡 Medium x  🟢 Low x  💡 Info x
- 整体风险评级: 严重 / 高 / 中 / 低

【🔴 CRITICAL - 必须立即修复】
  1. [位置] 漏洞简述
     类型: SQL注入 / 水平越权 / ...
     风险: 可导致...
     代码: 关键代码片段
     修复: 
       - 方案描述
       - 修复后的代码示例

【🟠 HIGH - 强烈建议修复】
  1. ...

【🟡 MEDIUM - 建议修复】
  1. ...

【🟢 LOW - 可选优化】
  1. ...

【💡 INFO - 安全建议】
  1. ...

【修复优先级】
  P0 (立即): CRITICAL 级别的问题
  P1 (本周): HIGH 级别的问题
  P2 (版本内): MEDIUM 级别的问题
  P3 (有空再改): LOW / INFO

【验证方法】
  - 每个漏洞如何验证是否修复成功
```
