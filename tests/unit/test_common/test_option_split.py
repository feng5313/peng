"""
测试 option_split.py 模块（事务#1998）

背景：LLM 通过 task/msg 工具、WebUI 的 /task、/msg 命令以单个字符串传入子命令
参数（"内容 + 尾部选项"）。历史缺陷：整串被当作内容写入 --file 临时文件，
导致 --workdir/--prompt/--creator/--small-model 等选项被静默忽略。
本模块测试 split_trailing_options / find_unparsed_option_flags 的分离与保护逻辑。
"""

import pytest

from peng.common.option_split import (
    find_unparsed_option_flags,
    split_trailing_options,
)

# bin/task subtask 的选项定义
SUB_VALUE = ("--prompt", "-p", "--workdir", "-w", "--creator", "-c", "--file")
SUB_BOOL = ("--small-model",)
# bin/task 字段类子命令（info/content/...）的选项定义
FIELD_VALUE = ("--file",)
FIELD_BOOL = ("-d", "--delete")


class TestSplitTrailingOptions:
    """尾部选项分离与内容保护"""

    @pytest.mark.parametrize(
        "desc,text,value_flags,bool_flags,exp_content,exp_options",
        [
            (
                "文档标准用法：全部选项位于尾部",
                "1 需求描述 --prompt coder --workdir /mnt/p/web --creator 鹏 --small-model",
                SUB_VALUE,
                SUB_BOOL,
                "1 需求描述",
                ["--prompt", "coder", "--workdir", "/mnt/p/web", "--creator", "鹏", "--small-model"],
            ),
            (
                "内容中间提及 --workdir（不得误判）",
                "1 参数尾部加的 --workdir xxx参数，没有起到 设置子事务workdir的作用",
                SUB_VALUE,
                SUB_BOOL,
                "1 参数尾部加的 --workdir xxx参数，没有起到 设置子事务workdir的作用",
                [],
            ),
            (
                "正文尾部形似选项（取值形态校验拦截）",
                "1 用 --workdir 指定子事务的工作目录",
                SUB_VALUE,
                SUB_BOOL,
                "1 用 --workdir 指定子事务的工作目录",
                [],
            ),
            (
                "正文尾形似选项2（值的形态不合规）",
                "1 说明 --workdir 参数的用法",
                SUB_VALUE,
                SUB_BOOL,
                "1 说明 --workdir 参数的用法",
                [],
            ),
            (
                "内容含普通 '-' 开头词（不误判）",
                "1 修复-auth模块的越权问题",
                SUB_VALUE,
                SUB_BOOL,
                "1 修复-auth模块的越权问题",
                [],
            ),
            (
                "Windows 路径反斜杠保留",
                r"1 需求 --workdir D:\work\temp",
                SUB_VALUE,
                SUB_BOOL,
                "1 需求",
                ["--workdir", r"D:\work\temp"],
            ),
            (
                "引号包裹路径去引号",
                '1 需求 --workdir "D:\\my dir\\sub"',
                SUB_VALUE,
                SUB_BOOL,
                "1 需求",
                ["--workdir", "D:\\my dir\\sub"],
            ),
            (
                "多行内容保留换行",
                "1 第一行\n第二行\n--workdir /x",
                SUB_VALUE,
                SUB_BOOL,
                "1 第一行\n第二行",
                ["--workdir", "/x"],
            ),
            ("短选项 -w", "1 需求 -w /x", SUB_VALUE, SUB_BOOL, "1 需求", ["-w", "/x"]),
            (
                "等号形式 --workdir=/x",
                "1 需求 --workdir=/x",
                SUB_VALUE,
                SUB_BOOL,
                "1 需求",
                ["--workdir=/x"],
            ),
            (
                "等号形式取值不合规（形态校验拦截）",
                "1 需求 --workdir=说明文本",
                SUB_VALUE,
                SUB_BOOL,
                "1 需求 --workdir=说明文本",
                [],
            ),
            (
                "prompt 取值非slug形态（拦截）",
                "1 需求 --prompt 指定提示词",
                SUB_VALUE,
                SUB_BOOL,
                "1 需求 --prompt 指定提示词",
                [],
            ),
            (
                "creator 取值 CJK 合法（无形态约束）",
                "1 需求 --creator 鹏 --small-model",
                SUB_VALUE,
                SUB_BOOL,
                "1 需求",
                ["--creator", "鹏", "--small-model"],
            ),
            ("仅内容无选项", "1 只改css样式", SUB_VALUE, SUB_BOOL, "1 只改css样式", []),
            ("field 子命令 -d 清空", "123 -d", FIELD_VALUE, FIELD_BOOL, "123", ["-d"]),
            (
                "field 子命令 内容 + -d",
                "123 修改说明 -d",
                FIELD_VALUE,
                FIELD_BOOL,
                "123 修改说明",
                ["-d"],
            ),
            (
                "field 普通内容",
                "123 修改后的描述内容",
                FIELD_VALUE,
                FIELD_BOOL,
                "123 修改后的描述内容",
                [],
            ),
            (
                "内容含 -d 样式的词（不误判）",
                "123 把进度-d改成完成",
                FIELD_VALUE,
                FIELD_BOOL,
                "123 把进度-d改成完成",
                [],
            ),
            (
                "选项在中间（非尾部，保守不解析）",
                "1 --small-model 需求描述",
                SUB_VALUE,
                SUB_BOOL,
                "1 --small-model 需求描述",
                [],
            ),
            ("空串", "", SUB_VALUE, SUB_BOOL, "", []),
            ("仅选项", "--small-model", SUB_VALUE, SUB_BOOL, "", ["--small-model"]),
        ],
    )
    def test_split(self, desc, text, value_flags, bool_flags, exp_content, exp_options):
        content, options = split_trailing_options(text, value_flags, bool_flags)
        assert content == exp_content, f"{desc}: content 不符"
        assert options == exp_options, f"{desc}: options 不符"


class TestFindUnparsedOptionFlags:
    """未解析选项（写错位置）的检测提示"""

    def test_detect_workdir_in_content(self):
        found = find_unparsed_option_flags(
            "1 用 --workdir 指定子事务的工作目录", SUB_VALUE + SUB_BOOL
        )
        assert found == ["--workdir"]

    def test_detect_small_model(self):
        found = find_unparsed_option_flags("1 需求 --small-model 内容在后", SUB_VALUE + SUB_BOOL)
        assert found == ["--small-model"]

    def test_plain_content_no_flags(self):
        assert find_unparsed_option_flags("1 普通需求描述", SUB_VALUE + SUB_BOOL) == []

    def test_dash_prefixed_word_not_flag(self):
        assert find_unparsed_option_flags("1 修复-auth模块", SUB_VALUE + SUB_BOOL) == []
