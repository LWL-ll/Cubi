#!/bin/bash
# M8 验收 4.3-4.5：作业锁定三连测
# 流程：教师建作业 → 学生提交 → 改题/删除/提前截止 均预期 400
# 用法：bash m8_accept_43.sh （项目根目录）
set -u

BASE=http://127.0.0.1:8000
TEACHER_JAR=/tmp/jar_teacher
STUDENT_JAR=/tmp/jar_student
# 教师 CSRF 从 cookie jar 动态提取（token 可能随服务重启/会话刷新变化，不硬编码）
T_CSRF=$(awk '$1=="127.0.0.1" && $6=="csrftoken"{print $7; exit}' "$TEACHER_JAR")
[ -n "$T_CSRF" ] || T_CSRF=$(awk '$6=="csrftoken"{print $7; exit}' "$TEACHER_JAR")
echo "教师 CSRF = $T_CSRF"

echo "===== 0) 教师会话检查（期望 200） ====="
code=$(curl -s -b "$TEACHER_JAR" -o /dev/null -w '%{http_code}' "$BASE/api/teacher/teaching-classes/")
echo "HTTP $code"
[ "$code" = "200" ] || { echo "教师会话失效，中止"; exit 1; }

echo "===== 1) 创建测试作业 M8-lock（期望返回含 id 的 JSON） ====="
resp=$(curl -s -b "$TEACHER_JAR" \
  -H "X-CSRFToken: $T_CSRF" -H "Content-Type: application/json" \
  -X POST "$BASE/api/teacher/teaching-classes/5/homeworks/" \
  --json '{"title":"M8-lock","startAt":"2026-10-18T08:00:00+08:00","deadline":"2026-10-25T23:59:59+08:00","problemIds":[3]}')
echo "$resp"
# 从返回 JSON 提取作业 id（动态，不硬编码）
hid=$(echo "$resp" | python -c "import sys,json;print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
[ -n "$hid" ] || { echo "未拿到作业 id，中止"; exit 1; }
echo "作业 id = $hid"

# 学生 CSRF 从 cookie jar 提取（Netscape 格式：第 6 列 name=csrftoken，第 7 列 value）
S_CSRF=$(awk '$6=="csrftoken"{print $7; exit}' "$STUDENT_JAR")
echo "学生 CSRF = $S_CSRF"

echo "===== 2) 学生提交一次（期望返回 submissionId） ====="
# code 为单行 A+B 的 C 程序；\n 转义在 JSON 层生效
sresp=$(curl -s -b "$STUDENT_JAR" \
  -H "X-CSRFToken: $S_CSRF" -H "Content-Type: application/json" \
  -X POST "$BASE/api/submissions/" \
  --json "{\"problemId\":3,\"submitType\":\"homework\",\"language\":\"c\",\"homeworkId\":$hid,\"code\":\"#include <stdio.h>\\nint main(){int a,b;scanf(\\\"%d %d\\\",&a,&b);printf(\\\"%d\\\",a+b);return 0;}\"}")
echo "$sresp"

echo "===== 3) 4.3 教师改题目列表（期望 400 已有提交，题目不可变更） ====="
curl -s -b "$TEACHER_JAR" \
  -H "X-CSRFToken: $T_CSRF" -H "Content-Type: application/json" \
  -X PUT "$BASE/api/teacher/homeworks/$hid/" \
  --json '{"title":"M8-lock","startAt":"2026-10-18T08:00:00+08:00","deadline":"2026-10-25T23:59:59+08:00","problemIds":[2,3]}'
echo

echo "===== 4) 4.4 教师删除该作业（期望 400 已有提交的作业不可删除） ====="
curl -s -b "$TEACHER_JAR" \
  -H "X-CSRFToken: $T_CSRF" \
  -X DELETE "$BASE/api/teacher/homeworks/$hid/"
echo

echo "===== 5) 4.5 教师提前截止（期望 400 截止时间只允许向后延长） ====="
curl -s -b "$TEACHER_JAR" \
  -H "X-CSRFToken: $T_CSRF" -H "Content-Type: application/json" \
  -X PUT "$BASE/api/teacher/homeworks/$hid/" \
  --json '{"title":"M8-lock","startAt":"2026-10-18T08:00:00+08:00","deadline":"2026-10-24T00:00:00+08:00","problemIds":[3]}'
echo
