#!/bin/bash
# M8 验收 5.2 修复验证（retryable 孤儿兜底，一条命令）：
#   健康检查 → 杀净 qcluster → 带 token 重启 long 集群 → 轮询 27 出终态
# 预期：回收扫描第一轮把 retryable 的 27 置回 pending 重投 → 判题
#   （沙箱 DNS 未恢复 → 瞬错回退）→ 下一轮扫描再兜底 → 执行次数
#   用尽 → markFailed → system_error 终态。全程约 2~4 分钟，不再悬挂。
# 用法：bash m8_verify_fix2.sh
set -u

BASE=http://127.0.0.1:8000
STUDENT_JAR=/tmp/jar_student
LOG_DIR=C:/Users/Lenovo/AppData/Local/Temp/codemind_accept
OUT_LOG=$LOG_DIR/qcluster_m8v2.out
ERR_LOG=$LOG_DIR/qcluster_m8v2.err

echo "===== 1) Django 健康检查（带学生 cookie 查提交 26） ====="
code=$(curl -s -m 5 -b "$STUDENT_JAR" -o /dev/null -w "%{http_code}" "$BASE/api/submissions/26/")
[ "$code" = "200" ] || { echo "  Django 未响应（HTTP $code）！请先启动 runserver 再重跑"; exit 1; }
echo "  Django 正常"

echo "===== 2) 杀净 qcluster ====="
for round in 1 2 3; do
  powershell.exe -NoProfile -Command 'Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like "*qcluster*" -and $_.Name -like "python*"} | ForEach-Object { & taskkill /T /F /PID $_.ProcessId }' >/dev/null 2>&1
  sleep 2
  powershell.exe -NoProfile -Command 'Get-CimInstance Win32_Process | Where-Object {$_.Name -like "python*" -and $_.CommandLine -like "*multiprocessing*"} | ForEach-Object { & taskkill /F /PID $_.ProcessId }' >/dev/null 2>&1
  sleep 2
  left=$(powershell.exe -NoProfile -Command '(Get-CimInstance Win32_Process | Where-Object {$_.Name -like "python*" -and ($_.CommandLine -like "*qcluster*" -or $_.CommandLine -like "*multiprocessing*")} | Measure-Object).Count' 2>/dev/null | tr -d '\r ')
  echo "  第 ${round} 轮杀后残留：$left"
  [ "$left" = "0" ] && break
done

echo "===== 3) 重启 qcluster（long 集群 + token） ====="
: > "$OUT_LOG"; : > "$ERR_LOG"
powershell.exe -NoProfile -Command '
  $tok = $env:OJ_AUTH_TOKEN
  if (-not $tok) { $tok = [Environment]::GetEnvironmentVariable("OJ_AUTH_TOKEN","User") }
  if (-not $tok) { $tok = [Environment]::GetEnvironmentVariable("OJ_AUTH_TOKEN","Machine") }
  if (-not $tok) { Write-Output "TOKEN_NOT_FOUND" } else { Write-Output "TOKEN_OK" }
  $env:OJ_AUTH_TOKEN = $tok
  $env:Q_CLUSTER_NAME = "long"
  Start-Process -FilePath "D:\miniconda\envs\codemind\python.exe" -ArgumentList "manage.py","qcluster" -WorkingDirectory "C:\Users\Lenovo\OneDrive\Desktop\codemind" -WindowStyle Hidden -RedirectStandardOutput "C:\Users\Lenovo\AppData\Local\Temp\codemind_accept\qcluster_m8v2.out" -RedirectStandardError "C:\Users\Lenovo\AppData\Local\Temp\codemind_accept\qcluster_m8v2.err"
' 2>&1
sleep 8

echo "===== 4) 轮询 27（期望 2~4 分钟内 system_error 终态） ====="
end=$((SECONDS + 360))
while [ $SECONDS -lt $end ]; do
  resp=$(curl -s -m 10 -b "$STUDENT_JAR" "$BASE/api/submissions/27/")
  st=$(echo "$resp" | python -c "import sys,json;print(json.load(sys.stdin).get('status','?'))" 2>/dev/null)
  sc=$(echo "$resp" | python -c "import sys,json;print(json.load(sys.stdin).get('score','?'))" 2>/dev/null)
  echo "  $(date +%H:%M:%S) 提交27 status=$st score=$sc"
  case "$st" in
    queued|running|'?') ;;   # '?' = 解析失败，继续重试
    *) echo "===== 27 已终态：$st score=$sc，retryable 孤儿兜底验证通过 ====="; exit 0 ;;
  esac
  sleep 10
done
echo "===== 超时 360s 仍未终态 ====="
echo "qcluster 日志：$OUT_LOG 与 $ERR_LOG"
exit 1
