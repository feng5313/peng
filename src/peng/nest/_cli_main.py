"""
Nest CLI 统一入口点模块

供 pyproject.toml [project.scripts] 注册为 console_scripts 入口。
bin/nest 也导入此模块，确保只有一个入口。

用法:
    nest connect --host HOST [--name NAME] [--port PORT]      连接到 peng 服务（前台）
    nest serve --host HOST [--name NAME] [--port PORT]        连接到 peng 服务（兼容旧命令）
    nest install [--name NAME] [--host HOST] [--port PORT]    安装为系统服务并启动
    nest uninstall [--name NAME]                              停止并卸载系统服务
    nest default --name NAME                                  设置默认 nest 实例
    nest start                                                启动系统服务
    nest stop                                                 停止系统服务
    nest restart                                              重启所有 config/nest.json 中的 nest 实例
    nest status                                               查看服务状态
    nest exec --nest NAME [--timeout N] [--stream] CMD [ARGS...]  在远端 nest 上执行命令

配置文件: config/nest.json（install/uninstall 等命令使用）
    nest connect/serve 的 --host 必填，--name 未指定时自动使用系统 hostname
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def _get_webui_port() -> int:
    """获取webui端口"""
    try:
        from peng.config.loader import load_config
        config = load_config()
        if hasattr(config, 'webui') and config.webui:
            return config.webui.port
    except Exception:
        pass
    return 6688


def _load_nest_config() -> dict:
    """加载 config/nest.json 配置"""
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    config_path = project_root / "config" / "nest.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"instances": {}}


def _get_api_hosts(port: int) -> list[str]:
    """获取可用的 API 地址列表（从 nest config 读取服务端地址）"""
    api_hosts: list[str] = []
    try:
        nest_config = _load_nest_config()
        for inst in nest_config.get("instances", {}).values():
            h = inst.get("host", "")
            # host 可能是 host:port 格式，需要提取 host 部分
            if ":" in h:
                h = h.split(":")[0]
            if h and h not in api_hosts:
                api_hosts.append(h)
    except Exception:
        pass
    if not api_hosts:
        api_hosts = ["localhost"]
    return api_hosts


def _do_exec(nest_name: str, command: str, timeout: int = 60, stream: bool = False) -> int:
    """在远端nest上执行命令并打印结果

    支持在远端 nest 上运行：先尝试 localhost，失败后从 nest config 获取服务端地址重试。
    
    当 stream=True 时，使用 SSE 流式模式实时显示输出。
    """
    port = _get_webui_port()
    api_hosts = _get_api_hosts(port)
    last_error = None

    if stream:
        return _do_exec_stream(nest_name, command, timeout, port, api_hosts)

    for api_host in api_hosts:
        try:
            params = urllib.parse.urlencode({"nest": nest_name, "command": command, "timeout": timeout})
            url = f"http://{api_host}:{port}/api/nest/exec?{params}"
            body = json.dumps({}).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            if not result.get("success"):
                print(f"远程执行失败: {result.get('error', 'unknown')}", file=sys.stderr)
                return 1

            exit_code = result.get("exit_code", -1)
            stdout = result.get("stdout", "")
            stderr = result.get("stderr", "")

            print(f"[{nest_name}]$ {command}")
            if stdout:
                print(stdout, end="")
            if stderr:
                print(f"\nSTDERR: {stderr}", end="")
            print(f"\n退出码: {exit_code}")

            return exit_code
        except urllib.error.URLError as e:
            last_error = e
            continue
        except Exception as e:
            last_error = e
            continue

    print(f"错误：无法连接到 peng webui 服务 (尝试了: {', '.join(f'{h}:{port}' for h in api_hosts)}): {last_error}", file=sys.stderr)
    return 1


def _do_exec_stream(nest_name: str, command: str, timeout: int, port: int, api_hosts: list[str]) -> int:
    """流式模式执行远端命令，实时显示输出
    
    通过 SSE (Server-Sent Events) 接收实时输出。
    """
    import sys
    
    last_error = None
    for api_host in api_hosts:
        try:
            params = urllib.parse.urlencode({"nest": nest_name, "command": command, "timeout": timeout})
            url = f"http://{api_host}:{port}/api/nest/exec/stream?{params}"
            body = json.dumps({}).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            })
            
            print(f"[{nest_name}]$ {command}", flush=True)
            
            exit_code = -1
            event_type = ""
            with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                    if not line:
                        # 空行分隔 SSE 事件
                        event_type = ""
                        continue
                    
                    # 解析 SSE 格式
                    if line.startswith("event: "):
                        event_type = line[7:]
                        continue
                    elif line.startswith("data: "):
                        data_str = line[6:]
                    else:
                        continue
                    
                    if event_type == "stdout":
                        try:
                            text = json.loads(data_str)
                        except (json.JSONDecodeError, ValueError):
                            text = data_str
                        sys.stdout.write(text)
                        sys.stdout.flush()
                    elif event_type == "stderr":
                        try:
                            text = json.loads(data_str)
                        except (json.JSONDecodeError, ValueError):
                            text = data_str
                        sys.stderr.write(text)
                        sys.stderr.flush()
                    elif event_type == "done":
                        try:
                            result = json.loads(data_str)
                            exit_code = result.get("exit_code", -1)
                        except (json.JSONDecodeError, ValueError):
                            pass
                    elif event_type == "error":
                        try:
                            result = json.loads(data_str)
                            print(f"\n错误: {result.get('error', '未知错误')}", file=sys.stderr)
                        except (json.JSONDecodeError, ValueError):
                            print(f"\n错误: {data_str}", file=sys.stderr)
                        return 1
            
            print(f"\n退出码: {exit_code}")
            return exit_code
            
        except urllib.error.URLError as e:
            last_error = e
            continue
        except Exception as e:
            last_error = e
            continue
    
    print(f"错误：无法连接到 peng webui 服务 (尝试了: {', '.join(f'{h}:{port}' for h in api_hosts)}): {last_error}", file=sys.stderr)
    return 1


def main() -> int:
    """Nest 主入口"""
    parser = argparse.ArgumentParser(
        prog="nest",
        description="Nest（鹏窝）- 远程控制服务（主动连接 peng）",
    )
    
    subparsers = parser.add_subparsers(dest="command", help="可用命令")
    
    # connect 子命令
    connect_parser = subparsers.add_parser("connect", help="连接到 peng 服务（前台）")
    connect_parser.add_argument("--name", default=None, help="实例名称（未指定时自动使用系统 hostname）")
    connect_parser.add_argument("--host", required=True, help="远程 peng 服务地址（host 或 host:port，必填）")
    connect_parser.add_argument("--port", type=int, default=None, help="远程 peng 服务端口")
    connect_parser.add_argument("--skip-single-instance-check", action="store_true", default=False,
                                help="跳过单实例检测")
    connect_parser.add_argument("--builtin-local", action="store_true", default=False,
                                help=argparse.SUPPRESS)  # 向后兼容，忽略此参数
    
    # serve 子命令（保留兼容，等同于 connect）
    serve_parser = subparsers.add_parser("serve", help="连接到 peng 服务（兼容旧命令）")
    serve_parser.add_argument("--name", default=None, help="实例名称")
    serve_parser.add_argument("--host", required=True, help="远程 peng 服务地址（host 或 host:port）")
    serve_parser.add_argument("--port", type=int, default=None, help="远程 peng 服务端口")
    
    # install 子命令
    install_parser = subparsers.add_parser("install", help="安装为系统服务并启动")
    install_parser.add_argument("--host", required=True, help="远程 peng 服务地址（host 或 host:port），必填")
    install_parser.add_argument("--port", type=int, default=None, help="远程 peng 服务端口")
    install_parser.add_argument("--name", default=None, help="实例名称（未指定则使用系统 hostname）")
    install_parser.add_argument("-d", "--daemon", action="store_true", default=False,
                                help="启用服务守护模式（远端服务失效时自动修复）")
    install_parser.add_argument("-s", "--ssh", default=None,
                                help="远程控制SSH命令（守护模式必须，如 root@192.168.1.100）")
    
    # uninstall 子命令
    uninstall_parser = subparsers.add_parser("uninstall", help="停止并卸载系统服务")
    uninstall_parser.add_argument("--name", default=None, help="实例名称（不指定则卸载所有）")
    
    # start 子命令
    subparsers.add_parser("start", help="启动系统服务")
    
    # stop 子命令
    subparsers.add_parser("stop", help="停止系统服务")
    
    # restart 子命令
    subparsers.add_parser("restart", help="重启所有 config/nest.json 中的 nest 实例")
    
    # status 子命令
    subparsers.add_parser("status", help="查看服务状态")
    
    # default 子命令
    default_parser = subparsers.add_parser("default", help="设置默认 nest 实例")
    default_parser.add_argument("--name", required=True, help="设置为默认的实例名称")
    
    # exec 子命令：在远端nest上执行命令
    exec_parser = subparsers.add_parser("exec", help="在远端 nest 上执行命令")
    exec_parser.add_argument("--nest", required=True, help="目标 nest 名称")
    exec_parser.add_argument("--timeout", type=int, default=60, help="超时秒数（默认60）")
    exec_parser.add_argument("--stream", action="store_true", default=False,
                              help="流式模式：实时显示输出（适用于长时间运行的命令）")
    exec_parser.add_argument("cmd", nargs="+", help="要执行的命令及参数")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    if args.command in ("connect", "serve"):
        from peng.nest.cli import do_connect
        skip_sic = getattr(args, 'skip_single_instance_check', False)
        return do_connect(name=args.name, host=args.host, port=args.port, skip_single_instance_check=skip_sic)
    
    if args.command == "exec":
        stream = getattr(args, 'stream', False)
        return _do_exec(nest_name=args.nest, command=" ".join(args.cmd), timeout=args.timeout, stream=stream)
    
    from peng.nest.cli import (
        do_install,
        do_uninstall,
        do_start,
        do_stop,
        do_restart,
        do_status,
        do_default,
    )
    
    if args.command == "install":
        daemon = getattr(args, 'daemon', False)
        ssh = getattr(args, 'ssh', None)
        return do_install(name=args.name, host=args.host, port=args.port, daemon=daemon, ssh=ssh)
    elif args.command == "uninstall":
        return do_uninstall(name=args.name)
    elif args.command == "default":
        return do_default(name=args.name)
    elif args.command == "start":
        return do_start()
    elif args.command == "stop":
        return do_stop()
    elif args.command == "restart":
        return do_restart()
    elif args.command == "status":
        return do_status()
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
