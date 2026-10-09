"""
路径规范化测试

测试目标：
验证 peng.llm.vision._normalize_path 函数处理各种路径格式
（反斜杠/正斜杠/相对路径统一规范化）
"""

import sys
from peng.llm.vision import _normalize_path


def test_normalize_path():
    """测试路径规范化函数"""
    tests = [
        # (输入, 期望输出)
        ('d:\\\\downloads\\\\test.jpg', 'd:/downloads/test.jpg'),
        ('d:/downloads/test.jpg', 'd:/downloads/test.jpg'),
        ('/home/user/test.jpg', '/home/user/test.jpg'),
        ('d:/downloads\\\\test.jpg', 'd:/downloads/test.jpg'),
        ('./test.jpg', 'test.jpg'),
    ]

    print("=" * 60)
    print("测试 _normalize_path 函数")
    print("=" * 60)

    all_passed = True
    for inp, expected in tests:
        result = _normalize_path(inp)
        passed = result == expected
        if not passed:
            all_passed = False
        status = 'PASS' if passed else 'FAIL'
        print(f'  {status}: {inp!r} -> {result!r} (expected: {expected!r})')

    print(f'\\n结果: {"全部通过" if all_passed else "有失败"}')
    return all_passed


def main():
    # 测试: 路径规范化
    path_ok = test_normalize_path()

    return 0 if path_ok else 1


if __name__ == '__main__':
    sys.exit(main())
