"""pytest 全局夹具

🔧 事务#2402：全局 cwd 隔离——测试一律在独立临时目录运行。

背景：测试用 Windows 风格路径（"D:" + 绝对路径）在 Linux 上构成相对路径
（如 test_external_backup_drives 的 "D:/tmp/pytest-..."），任何相对解析/
写入都落到 pytest 启动目录（项目根），在项目里攒出 D:/ 等垃圾目录树。
隔离 cwd 后，未显式使用 tmp_path 的相对路径写入全部落在临时目录，随测试
销毁，从机制上斩断"测试往项目目录放垃圾"。
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_cwd(request, tmp_path_factory, monkeypatch):
    """每个测试自动 chdir 到独立临时目录（斩断相对路径污染项目根）

    依赖项目根 cwd 的测试（读 config/peng.json 等相对资源）用
    ``@pytest.mark.keep_cwd`` 显式声明保留原 cwd。
    """
    if request.node.get_closest_marker("keep_cwd"):
        yield
        return
    d = tmp_path_factory.mktemp("cwd")
    monkeypatch.chdir(d)
    yield
