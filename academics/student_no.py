"""学号格式规则模块（占位规则，待学校格式定稿）

全站唯一的学号解析入口：导入学生、自动分班、学院/专业生成都调用本模块。
学校学号格式定稿后，只需修改本文件的规则，全站生效。

占位规则（2026-10-18 起解析升级为 V2 专业层，详见 docs/数据库表重构设计.md）：
- 学号为 8 位数字，如 20231001
- 第 1-4 位：入学年级（2023）
- 第 5-6 位：专业代码（10）
- 第 7-8 位：班级序号（01）
- 班级名 = 专业名 + 年级后两位 + 班级序号（如 计科2301）
- 班级编码 = 专业代码 + 班级序号（如 1001）
"""

# 专业代码 -> (学院名, 专业名)
MAJOR_CODES = {
    '10': ('计算机学院', '计科'),
    '11': ('外国语学院', '英语'),
    '12': ('数学学院', '数学'),
}


def parseStudentNo(no):
    """按学号格式解析出 (学院名, 专业代码, 专业名, 班级名)

    格式不合法或专业代码未知时抛出 ValueError，调用方按导入行报错。
    """
    # 校验：8 位纯数字
    if not no or len(no) != 8 or not no.isdigit():
        raise ValueError(f'学号 {no} 不符合 8 位数字格式')
    year = no[0:4]
    majorCode = no[4:6]
    classSeq = no[6:8]
    if majorCode not in MAJOR_CODES:
        raise ValueError(f'学号 {no} 的专业代码 {majorCode} 未登记')
    departmentName, majorName = MAJOR_CODES[majorCode]
    clazzName = f'{majorName}{year[2:]}{classSeq}'
    return departmentName, majorCode, majorName, clazzName
