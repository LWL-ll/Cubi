# 判题沙箱部署契约（judge-env）

> 本文是 OJ 判题链路的部署契约：语言/镜像实测结论 + go-judge 参数 +
> Django 侧部署步骤 + 验收红线。任何一方变更必须同步修订本文并重跑
> `scripts/sandbox_probe.py` 实测。
> 规格出处：docs/OJ对接设计.md（第二/三/四/五/十三/十四章）。

## 一、沙箱实例与认证

| 项 | 值 |
|---|---|
| 实例 | `https://oj.liuwenli.cn`（go-judge v1.13.3，云实例，不改动） |
| 认证 | `Authorization: Bearer <OJ_AUTH_TOKEN>` |
| 凭证注入 | **只从环境变量 `OJ_AUTH_TOKEN` 读取**（settings.py 已配置），严禁硬编码进仓库；泄露后立即轮换 |
| HTTP 超时 | 30s（`OJ_RUN_TIMEOUT`，大于单题最大 clockLimit 10s + 网络余量） |

## 二、镜像语言实测结论（2026-10-18 实测）

> 由 `scripts/sandbox_probe.py` 输出冻结；镜像升级后必须重跑实测并更新本表。

| 实测项 | 结论 |
|---|---|
| cgroup 启用（验收红线） | 【待实测回填：PASS/FAIL + enable/file】 |
| gcc 版本 | 【待实测回填】 |
| python3 版本 | 【待实测回填】 |
| python3 标准库抽样 | 【待实测回填：json/re/math/sys/os 是否可用】 |
| gcc 绝对路径 | 【待实测回填】 |
| python3 绝对路径 | 【待实测回填】 |
| 内存限制强制生效 | 【待实测回填：64MB 档必须 Memory Limit Exceeded】 |

**已冻结编译命令**（M5，勿改）：`gcc main.c -o main -O2 -std=c11 -lm`
（对应判题请求 `copyOutCached: ["main"]` + `args: ["./main"]`；
Python 直接 `python3 main.py` 内联，无编译步）

## 三、资源限制（服务端每请求显式携带，不依赖沙箱默认值）

| 档位 | cpuLimit | clockLimit | memoryLimit | copyOutMax |
|---|---|---|---|---|
| 入门/基础 | 2s | 5s | 256 MB | 16 MB |
| 进阶 | 5s | 10s | 512 MB | 32 MB |
| SPJ 独立档 | 2s | 5s | 256 MB | 1 MB |
| 编译档 | 档位 cpu/memory | 10s | 档位 memory | 32 MB |

固定项：`procLimit=32`、stdout/stderr 各 `max=8MB`、沙箱环境 `PATH=/usr/bin:/bin`。

## 四、go-judge 侧部署要求（沙箱服务器）

1. **限源**：`/run`、`/file` 仅应用服务器可达（nginx 模板 `deploy/nginx-oj.conf`）；
   `/config` 验收后关闭或仅限应用服务器（泄露 cgroup/路径配置会暴露攻击面）
2. **cgroup 启用**：验收红线，`GET /config` 确认 `cgroup.enable=true`，
   失败即拒绝启动判题，不降级运行（14.3）
3. **并发**：go-judge 侧 `-parallelism` 固定，仅作观测；并发准入由应用侧
   队列闸（用户未终态 JudgeTask ≤ 3）+ long 集群 workers 数控制（6.1 裁定 2）
4. **总量压测**：单请求实际内存 ≈ 程序 memoryLimit + copyOut + 并发缓存文件，
   需按 `-parallelism` × q2 workers × 用户上限联合压测后固化部署参数（13 章待办）

## 五、Django 侧部署步骤

1. **依赖**：conda 环境 codemind（Django 6.1.2 + DRF + django-q2 1.11.1）；
   `python manage.py migrate`（含判题租约回收定时任务迁移）
2. **凭证**：应用服务器环境注入 `OJ_AUTH_TOKEN`（systemd 单元或 .env 由运维保管）
3. **qcluster 双集群**（django-q2，ORM broker 无需 Redis）：
   ```bash
   python manage.py qcluster                    # 主集群：导入/杂项任务 + 租约回收定时任务
   Q_CLUSTER_NAME=long python manage.py qcluster  # long 集群：判题任务（workers=2）
   ```
   生产建议 systemd 托管两个进程；判题集群 retry=1000s > timeout=900s
   （django-q2 约束：retry 必须大于 timeout，否则任务超时被杀前就重投导致并发执行）
4. **应用服务**：runserver/gunicorn 按项目既有部署方式；SQLite 单写者，
   切 PG 后需为提交频率闸加用户行 `select_for_update`（7.1 注释）
5. **验收**：跑通 `docs/OJ验收清单.md` 全链路后上线；部署验收强制验证
   cgroup 隔离可用，失败即拒绝启动判题

## 六、变更流程

镜像升级 / 语言版本变化 / 沙箱地址迁移时：
1. 重跑 `scripts/sandbox_probe.py`（`OJ_AUTH_TOKEN=xxx python scripts/sandbox_probe.py`）
2. 用输出更新本文第二节实测表
3. 全量测试回归（`python manage.py test`，当前 230 测试）
4. 重跑 `docs/OJ验收清单.md` 端到端验收
