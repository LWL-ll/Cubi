"""go-judge 沙箱镜像一次性实测脚本（M8 部署验收用，不进业务链路）

用法（conda codemind 环境）：
    OJ_AUTH_TOKEN=xxx python scripts/sandbox_probe.py

实测项（docs/OJ对接设计.md 部署契约）：
  1. GET /config：完整配置诊断 + cgroup 资源强制判定（验收红线）
  2. POST /file：原始响应形态诊断（实测返回纯字符串 fileId）
  3. gcc 编译 + 运行：冻结编译命令实测、版本号、copyOutCached 产物复用
  4. python3 运行 + 版本 + 标准库抽样（json/re/math/sys/os）
  5. 绝对路径确认（which gcc / which python3）
  6. 内存限制生效抽查（cgroup 强制执行验证）
  7. GET /version：沙箱确切版本号（新版 API 端点验证）

所有 /run 调用都走裸调诊断：被 HTTP 400 拒绝时打印错误响应体原文
（用于定位请求格式与新版本 go-judge 的差异），并短路跳过后续实测。
凭证只从环境变量读取（settings.OJ_AUTH_TOKEN），严禁硬编码。
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

# 脚本位于 scripts/ 子目录：直接运行时 sys.path[0] 是 scripts/，
# 需把项目根目录插入 sys.path 才能 import codemind.settings
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    """GET /config：读原始 JSON（诊断用，不经过业务客户端）"""
    url = settings.OJ_BASE_URL.rstrip('/') + '/config'
    request = urllib.request.Request(url, method='GET')
    with urllib.request.urlopen(request, timeout=settings.OJ_RUN_TIMEOUT) as resp:
        return json.loads(resp.read())


def probeConfig():
    """GET /config：完整配置落盘诊断 + 新版 runnerConfig 形态判定（验收红线）"""
    config = fetchConfig()
    print('[DIAG] /config 完整内容：')
    print(json.dumps(config, indent=2, ensure_ascii=False))
    runner = config.get('runnerConfig') or {}
    controllers = runner.get('cgroupControllers') or []
    # 新版 go-judge 不再暴露顶层 cgroup 键，资源强制看 runnerConfig
    # 的控制器清单：memory + cpu 齐备即视为红线 PASS（14.3）
    enforced = 'memory' in controllers and 'cpu' in controllers
    record('cgroup 资源强制', enforced,
           f"cgroupControllers={controllers}, cgroupType={runner.get('cgroupType')}, "
           f"seccomp={runner.get('seccomp')}")


def probeVersion():
    """GET /version：记录沙箱确切版本号（新版 go-judge 端点，回填部署契约用）"""
    url = settings.OJ_BASE_URL.rstrip('/') + '/version'
    headers = {'Authorization': f'Bearer {settings.OJ_AUTH_TOKEN}'}
    request = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(request,
                                    timeout=settings.OJ_RUN_TIMEOUT) as resp:
            raw = resp.read()
        print(f'[DIAG] GET /version → HTTP 200，原始响应：{raw!r}')
        try:
            version = json.loads(raw)
        except json.JSONDecodeError:
            version = raw.decode('utf-8', errors='replace')
        record('沙箱版本', bool(version), str(version))
    except urllib.error.HTTPError as e:
        print(f'[DIAG] GET /version → HTTP {e.code}，响应体：{e.read()!r}')
        record('沙箱版本', False, f'HTTP {e.code}')


def rawUpload(fileContent):
    """裸调 POST /file 并打印原始响应（诊断真实返回形态，不进业务客户端解析）"""
    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\n'.encode('utf-8')
        + 'Content-Disposition: form-data; name="file"; '
          'filename="probe.c"\r\n'.encode('utf-8')
        + b'Content-Type: application/octet-stream\r\n\r\n'
        + fileContent
        + f'\r\n--{boundary}--\r\n'.encode('utf-8')
    )
    statusCode, rawBody = judge._doRequest(
        'POST', '/file', body, f'multipart/form-data; boundary={boundary}')
    print(f'[DIAG] POST /file → HTTP {statusCode}，原始响应：{rawBody!r}')
    parsed = json.loads(rawBody)
    if isinstance(parsed, str):
        return parsed          # 实测形态：纯字符串 fileId
    return parsed.get('fileId') or parsed.get('ids', {}).get('file') or ''


def rawRunDiag(runRequest, label):
    """裸调 POST /run 诊断：打印原始响应（含 400 错误响应体原文）

    返回解析后的 Result 数组；被 4xx 拒绝时返回 None（错误原文已打印）。
    """
    # 新版 API 形态：/run 期望单个 Request 对象，cmd 命令数组包在对象内
    # （旧版直接发裸数组，实测被 400 拒：cannot unmarshal array into model.Request）
    body = json.dumps({'cmd': runRequest}).encode('utf-8')
    url = settings.OJ_BASE_URL.rstrip('/') + '/run'
    headers = {'Authorization': f'Bearer {settings.OJ_AUTH_TOKEN}',
               'Content-Type': 'application/json'}
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method='POST')
    try:
        with urllib.request.urlopen(request,
                                    timeout=settings.OJ_RUN_TIMEOUT) as resp:
            raw = resp.read()
        print(f'[DIAG] {label} → HTTP 200，原始响应（前 500 字符）：{raw[:500]!r}')
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        print(f'[DIAG] {label} → HTTP {e.code}，错误响应体原文：{e.read()!r}')
        return None


def probeGcc():
    """gcc 编译 + 运行 + 版本：冻结编译命令实测 + copyOutCached 复用验证"""
    fileId = rawUpload(HELLO_C.encode('utf-8'))
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
    versionResults = rawRunDiag(versionRun, 'gcc --version 探测')
    if versionResults is None:
        record('gcc --version', False, '/run 被拒（见上方错误原文）')
        judge.deleteFile(fileId)
        return
    versionResult = versionResults[0]
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
    compileResults = rawRunDiag(compileRun, 'gcc 编译（冻结命令）')
    if compileResults is None:
        record('gcc 编译（冻结命令）', False, '/run 被拒（见上方错误原文）')
        judge.deleteFile(fileId)
        return
    compileResult = compileResults[0]
    binaryId = compileResult.get('fileIds', {}).get('main')
    record('gcc 编译（冻结命令）', compileResult['status'] == 'Accepted',
           f"stderr={compileResult.get('files', {}).get('stderr', '')!r}, "
           f"binaryFileId={binaryId}")
    # 编译没产出 main：跳过运行探测（拿 None 去 copyIn 会被新 API 400 拒，
    # 那是探测脚本自己的连锁反应，不是沙箱问题）
    if binaryId is None:
        record('C 程序运行 ×2（产物复用）', False, '编译未产出 main，跳过')
        judge.deleteFile(fileId)
        return

    # 用编译产物运行两次：第一次验证执行，第二次验证 copyOutCached 产物可复用；
    # 两次都引用编译返回的同一个 fileId（运行结果里没有 fileIds，
    # 不能从运行结果里重新取，否则下一次 copyIn 拿 None 被 400 拒）
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
        runResults = rawRunDiag(run, f'C 程序运行第 {runIndex + 1} 次')
        if runResults is None:
            record('C 程序运行 ×2（产物复用）', False, '/run 被拒')
            judge.deleteFile(fileId)
            return
        runResult = runResults[0]
        outputs.append(runResult.get('files', {}).get('stdout', ''))
    ok = outputs[0] == 'HELLO_PROBE\n' and outputs[1] == 'HELLO_PROBE\n'
    record('C 程序运行 ×2（产物复用）', ok, f'stdout={outputs!r}')
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
    versionResults = rawRunDiag(versionRun, 'python3 --version 探测')
    if versionResults is None:
        record('python3 --version', False, '/run 被拒（见上方错误原文）')
        return
    versionResult = versionResults[0]
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
    stdlibResults = rawRunDiag(stdlibRun, 'python3 标准库抽样')
    if stdlibResults is None:
        record('python3 标准库抽样', False, '/run 被拒（见上方错误原文）')
        return
    stdlibResult = stdlibResults[0]
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
        results = rawRunDiag(run, f'which {tool} 探测')
        if results is None:
            record('绝对路径', False, f'which {tool} /run 被拒')
            return
        outputs[tool] = results[0].get('files', {}).get('stdout', '').strip()
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
    results = rawRunDiag(run, '内存限制抽查（64MB 档）')
    if results is None:
        record('内存限制强制（64MB 档）', False, '/run 被拒（见上方错误原文）')
        return
    result = results[0]
    record('内存限制强制（64MB 档）', result['status'] == 'Memory Limit Exceeded',
           f"status={result['status']}（期望 Memory Limit Exceeded）")


def main():
    if not settings.OJ_AUTH_TOKEN:
        print('FAIL：OJ_AUTH_TOKEN 环境变量未设置，无法实测沙箱')
        sys.exit(1)
    print(f'实测目标：{settings.OJ_BASE_URL}\n')
    probeConfig()
    probeVersion()
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
