"""
VisionTool 路径规范化测试

测试目标：
1. 验证 _normalize_path 函数处理各种路径格式
2. 验证 VisionTool 使用反斜杠和正斜杠路径都能正常工作
"""

import asyncio
import json
import sys
from peng.llm.vision import _normalize_path
from peng.tools.vision import VisionTool


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


async def test_vision_tool():
    """测试 VisionTool 使用不同路径格式"""
    tool = VisionTool()
    print("\\n" + "=" * 60)
    print("测试 VisionTool 路径兼容性")
    print("=" * 60)
    
    # 测试1: 反斜杠路径
    print("\\n测试1: 反斜杠路径 d:\\\\downloads\\\\test.jpg")
    result1 = await tool.execute(
        image_path='d:\\\\downloads\\\\test.jpg',
        prompt='请识别图片中的人物名字'
    )
    data1 = json.loads(result1)
    if 'error' in data1:
        print(f'  FAIL: {data1["error"]}')
        return False
    print(f'  PASS: actual_scale={data1["actual_scale"]}')
    print(f'  识别结果: {data1["result"][:100]}...')
    
    # 测试2: 正斜杠路径
    print("\\n测试2: 正斜杠路径 d:/downloads/test.jpg")
    result2 = await tool.execute(
        image_path='d:/downloads/test.jpg',
        prompt='请识别图片中的人物名字'
    )
    data2 = json.loads(result2)
    if 'error' in data2:
        print(f'  FAIL: {data2["error"]}')
        return False
    print(f'  PASS: actual_scale={data2["actual_scale"]}')
    print(f'  识别结果: {data2["result"][:100]}...')
    
    # 测试3: 不存在的文件
    print("\\n测试3: 不存在的文件 d:/nonexistent.jpg")
    result3 = await tool.execute(
        image_path='d:/nonexistent.jpg',
        prompt='测试'
    )
    data3 = json.loads(result3)
    if 'error' in data3:
        print(f'  PASS: 正确返回错误 - {data3["error"][:50]}...')
    else:
        print(f'  FAIL: 应该返回错误')
        return False
    
    # 测试4: scale参数为字符串
    print("\\n测试4: scale参数为字符串 '0.5'")
    result4 = await tool.execute(
        image_path='d:/downloads/test.jpg',
        prompt='请识别图片中的人物名字',
        scale='0.5'
    )
    data4 = json.loads(result4)
    if 'error' in data4:
        print(f'  FAIL: {data4["error"]}')
        return False
    print(f'  PASS: actual_scale={data4["actual_scale"]}')
    
    print("\\n" + "=" * 60)
    print("所有测试通过！")
    print("=" * 60)
    return True


async def main():
    # 测试1: 路径规范化
    path_ok = test_normalize_path()
    
    # 测试2: VisionTool 功能
    tool_ok = await test_vision_tool()
    
    return 0 if (path_ok and tool_ok) else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
