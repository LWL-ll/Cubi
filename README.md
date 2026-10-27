# CodeMind 教学平台后端

Django + DRF 教辅系统，提供教务、教师、课程组、账号与 OJ 接口。认证采用 Session 与 CSRF，数据库默认 SQLite，队列采用 django-q2 ORM broker。前端模板用于已有开发预览；非学生端新增能力以接口交付。

## 安装与启动

在支持 Django 6.1 的 Python 环境中安装 [requirements.txt](requirements.txt)。这些直接依赖版本与本轮本地验收环境一致。

```text
python -m pip install -r requirements.txt
python manage.py check
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

新环境需先迁移数据库。已有业务库升级必须先备份、确认迁移计划；本地业务库已于 2026-10-27 经用户单独授权完成本轮 5 项迁移，原有业务数据校验通过。生产环境仍需独立升级和验收。管理员在 `/admin/` 创建教务账号，再通过教务导入接口创建教师和学生。

独立运行默认导入 worker：

```text
python manage.py qcluster
```

判题还需要独立的 `long` worker。PowerShell：

```powershell
$env:Q_CLUSTER_NAME = 'long'
python manage.py qcluster
```

Linux：

```sh
Q_CLUSTER_NAME=long python manage.py qcluster
```

默认 `codemind` worker 消费教务导入及其回收排程；`long` worker 消费判题及租约回收。两个进程均需常驻。Agent 扩展默认关闭，未接入外部 LLM。

## 配置

| 配置 | 用途 |
|---|---|
| `DJANGO_SECRET_KEY` | 部署密钥，生产必须从环境注入 |
| `DJANGO_DEBUG=0` | 关闭开发调试 |
| `DJANGO_ALLOWED_HOSTS` | 逗号分隔的部署域名 |
| `OJ_BASE_URL`、`OJ_AUTH_TOKEN` | go-judge 地址与服务端凭证 |
| `Q_CLUSTER_NAME` | 选择默认、`long` 等队列集群 |
| `PRIVATE_MATERIAL_ROOT`（Django setting） | 默认项目下 `private_uploads/`；班级资料、课程资料与临时导入文件私有保存 |

设置不会自动读取 `.env`。私有目录不能由 Nginx 作为公开静态目录映射；下载必须经过授权接口。生产反向代理及沙箱要求见 [deploy/judge-env.md](deploy/judge-env.md)。

## 接口与业务说明

- [非学生端后端接口](docs/非学生端后端接口.md)：基础维护、账号学籍、学期、导入批次、审计和课程组接口。
- [完整 API 文档](docs/API设计文档.md)：认证、教师、既有学生及题库接口。
- [架构设计](docs/架构设计.md)、[权限设计](docs/用户权限设计.md)、[教学班级设计](docs/教学班级设计.md)：模型、权限和历史保留规则。
- [异步任务设计](docs/异步任务设计.md)：导入进度、重试、分段与私有文件回收。
- [待办与部署验收](docs/后端落地问题清单.md)、[OJ 验收](docs/OJ验收清单.md)：区分已实现能力与生产环境待验项目。

## 验证与协作

```text
python manage.py test --noinput
python manage.py check
python manage.py makemigrations --dry-run --check
```

测试使用测试库。本轮新增迁移为 `academics 0011/0012`、`tasks 0004/0005`、`course_team 0001`；历史学期迁移保留原编码，不推断学校日期。完成部署后应配置正式学期日期并核对当前学期。

从 `liuwenli` 新建无 `codex/` 前缀的任务分支，中文本地提交，由用户推送 GitHub 后交团队审查合并。具体操作授权规则见 [AGENTS.md](AGENTS.md)。
