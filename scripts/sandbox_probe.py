"""go-judge 沙箱镜像一次性实测脚本（M8 部署验收用，不进业务链路）

用法（conda codemind 环境）：
    OJ_AUTH_TOKEN=xxx python scripts/sandbox_probe.py

实测项（docs/OJ对接设计.md 部署契约）：
  1. GET /config：cgroup 启用状态（验收红线：失败即拒绝启动判题）
  2. gcc 编译 + 运行：冻结编译命令实测、版本号、copyOutCached 产物复用
  3. python3 运行 + 版本 + 标准库抽样（json/re/math/sys/os）
  4. 绝对路径确认（which gcc / which python3）
  5. 内存限制生效抽查（cgroup 强制执行验证）

输出结构化报告，结论冻结进 deploy/judge-env.md。
凭证只从环境变量读取（settings.OJ_AUTH_TOKEN），严禁硬编码。
"""
import json
import os
import sys
import urllib.request

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'codemind.settings')
django.setup()

from django.conf import settings  # noqa: E402

from teaching import judge  # noqa: E402

# 与 tasks/tasks_judge.py 冻结值保持一致（M5 编译命令冻结，勿改）
COMPILE_CMD = ['gcc', 'main.c', '-o', 'main', '-O2', '-std=c11', '-lm']
HELLO_C = '#include <stdio.h>\nint main(void) { printf("HELLO_PROBE\\n"); return 0; }\n'

results = []  # (项目, 结论 PASS/FAIL, 说明)


def record(item, passed, detail):
    results.append((item, 'PASS' if passed else 'FAIL', detail))
    print(f'[{results[-1][1]}] {item}：{detail}')


def fetchConfig():
    """GET /config：cgroup 启用状态与关键配置（验收红线）"""
    url = settings.OJ_BASE_URL.rstrip('/') + '/config'
    request = urllib.request.Request(url, method='GET')
    with urllib.request.urlopen(request, timeout=settings.OJ_RUN_TIMEOUT) as resp:
        return json.loads(resp.read())


def probeConfig():
    config = fetchConfig()
    cgroup = config.get('cgroup') or {}
    enabled = cgroup.get('enable', False)
    record('cgroup 启用', bool(enabled),
           f"enable={enabled}, file={cgroup.get('file')}")
    record('工作目录', 'path' in config or 'srcPrefix' in config,
           f"config 键：{sorted(config.keys())}")


def probeGcc():
    """gcc 编译 + 运行 + 版本：冻结编译命令实测 + copyOutCached 复用验证"""
    fileId = judge.uploadFile(HELLO_C.encode('utf-8'))
    # 版本探测与编译分开两次 /run（版本探测输出进 stdout）
    versionRun = [{
        'args': ['gcc', '--version'],
        'env': ['PATH=/usr/bin:/bin'],
        'files': [{'content': ''}, {'name': 'stdout', 'max': 65536},
                  {'name': 'stderr', 'max': 65536}],
        'copyIn': {}, 'copyOut': ['stdout'],
        'cpuLimit': 5_000_000_000, 'clockLimit': 10_000_000_000,
        'memoryLimit': 268_435_456, 'procLimit': 32, 'copyOutMax': 16_777_216,
    }]
    versionResult = judge.submitRun(versionRun)[0]
    versionText = versionResult.get('files', {}).get('stdout', '').splitlines()
    versionLine = versionText[0] if versionText else '（无输出）'
    record('gcc --version', versionResult['status'] == 'Accepted', versionLine)

    compileRun = [{
        'args': COMPILE_CMD,
        'env': ['PATH=/usr/bin:/bin'],
        'files': [{'content': ''}, {'name': 'stdout', 'max': 8_388_608},
                  {'name': 'stderr', 'max': 8_388_608}],
        'copyIn': {'main.c': {'fileId': fileId}},
        'copyOutCached': ['main'],
        'cpuLimit': 2_000_000_000, 'clockLimit': 10_000_000_000,
        'memoryLimit': 268_435_456, 'procLimit': 32,
        'copyOutMax': 33_554_432,
    }]
    compileResult = judge.submitRun(compileRun)[0]
    binaryId = compileResult.get('fileIds', {}).get('main')
    record('gcc 编译（冻结命令）', compileResult['status'] == 'Accepted',
           f"stderr={compileResult.get('files', {}).get('stderr', '')!r}, "
           f"binaryFileId={binaryId}")

    # 用编译产物运行两次：第一次验证执行，第二次验证 copyOutCached 产物可复用
    outputs = []
    for runIndex in range(2):
        run = [{
            'args': ['./main'],
            'env': ['PATH=/usr/bin:/bin'],
            'files': [{'content': ''}, {'name': 'stdout', 'max': 8_388_608},
                      {'name': 'stderr', 'max': 8_388_608}],
            'copyIn': {'main': {'fileId': binaryId}},
            'cpuLimit': 2_000_000_000, 'clockLimit': 5_000_000_000,
            'memoryLimit': 268_435_456, 'procLimit': 32,
            'copyOutMax': 16_777_216,
        }]
        runResult = judge.submitRun(run)[0]
        outputs.append(runResult.get('files', {}).get('stdout', ''))
        if runIndex == 0:
            binaryId = runResult.get('fileIds', {}).get('main')
    ok = outputs[0] == 'HELLO_PROBE\n' and outputs[1] == 'HELLO_PROBE\n'
    record('C 程序运行 ×2（产物复用）', ok,
           f'stdout={outputs!r}')
    judge.deleteFile(fileId)


def probePython():
    """python3 版本 + 标准库抽样 + 内联运行"""
    versionRun = [{
        'args': ['python3', '--version'],
        'env': ['PATH=/usr/bin:/bin'],
        'files': [{'content': ''}, {'name': 'stdout', 'max': 65536},
                  {'name': 'stderr', 'max': 65536}],
        'copyIn': {}, 'copyOut': ['stdout'],
        'cpuLimit': 2_000_000_000, 'clockLimit': 5_000_000_000,
        'memoryLimit': 268_435_456, 'procLimit': 32, 'copyOutMax': 16_777_216,
    }]
    versionResult = judge.submitRun(versionRun)[0]
    versionText = versionResult.get('files', {}).get('stdout', '').strip()
    record('python3 --version', versionResult['status'] == 'Accepted',
           versionText)

    stdlibCode = ('import json, re, math, sys, os\n'
                  'print("STDLIB_OK", sys.version_info[:2], os.path.sep)\n')
    stdlibRun = [{
        'args': ['python3', 'main.py'],
        'env': ['PATH=/usr/bin:/bin'],
        'files': [{'content': ''}, {'name': 'stdout', 'max': 8_388_608},
                  {'name': 'stderr', 'max': 8_388_608}],
        'copyIn': {'main.py': {'content': stdlibCode}},
        'cpuLimit': 2_000_000_000, 'clockLimit': 5_000_000_000,
        'memoryLimit': 268_435_456, 'procLimit': 32,
        'copyOutMax': 16_777_216,
    }]
    stdlibResult = judge.submitRun(stdlibRun)[0]
    stdout = stdlibResult.get('files', {}).get('stdout', '').strip()
    record('python3 标准库抽样', 'STDLIB_OK' in stdout, stdout)


def probePaths():
    """绝对路径确认：which gcc / which python3（部署契约字段）"""
    outputs = {}
    for tool in ('gcc', 'python3'):
        run = [{
            'args': ['which', tool],
            'env': ['PATH=/usr/bin:/bin'],
            'files': [{'content': ''}, {'name': 'stdout', 'max': 65536},
                      {'name': 'stderr', 'max': 65536}],
            'copyIn': {}, 'copyOut': ['stdout'],
            'cpuLimit': 2_000_000_000, 'clockLimit': 5_000_000_000,
            'memoryLimit': 268_435_456, 'procLimit': 32,
            'copyOutMax': 16_777_216,
        }]
        result = judge.submitRun(run)[0]
        outputs[tool] = result.get('files', {}).get('stdout', '').strip()
    ok = outputs['gcc'].startswith('/') and outputs['python3'].startswith('/')
    record('绝对路径', ok, f'gcc={outputs["gcc"]}, python3={outputs["python3"]}')


def probeMemoryLimit():
    """内存限制生效抽查：分配 300MB 但 memoryLimit=64MB → 必须 Memory Limit Exceeded"""
    bigAlloc = 'data = bytearray(300 * 1024 * 1024)\nprint(len(data))\n'
    run = [{
        'args': ['python3', 'main.py'],
        'env': ['PATH=/usr/bin:/bin'],
        'files': [{'content': ''}, {'name': 'stdout', 'max': 8_388_608},
                  {'name': 'stderr', 'max': 8_388_608}],
        'copyIn': {'main.py': {'content': bigAlloc}},
        'cpuLimit': 2_000_000_000, 'clockLimit': 5_000_000_000,
        'memoryLimit': 67_108_864, 'procLimit': 32,   # 64MB：cgroup 强制生效
        'copyOutMax': 16_777_216,
    }]
    result = judge.submitRun(run)[0]
    record('内存限制强制（64MB 档）', result['status'] == 'Memory Limit Exceeded',
           f"status={result['status']}（期望 Memory Limit Exceeded）")


def main():
    if not settings.OJ_AUTH_TOKEN:
        print('FAIL：OJ_AUTH_TOKEN 环境变量未设置，无法实测沙箱')
        sys.exit(1)
    print(f'实测目标：{settings.OJ_BASE_URL}\n')
    probeConfig()
    probeGcc()
    probePython()
    probePaths()
    probeMemoryLimit()
    print('\n===== 汇总 =====')
    for item, verdict, detail in results:
        print(f'{verdict}  {item}  {detail}')
    failed = [item for item, verdict, _ in results if verdict == 'FAIL']
    print(f'\n共 {len(results)} 项，失败 {len(failed)} 项：{failed or "无"}')
    # cgroup 是验收红线：失败即拒绝启动判题（14.3）
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
