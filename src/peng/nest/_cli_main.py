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
    nest list                                                 查看服务状态（status 别名）
    nest exec --nest NAME [--timeout N] [--stream] CMD [ARGS...]  在远端 nest 上执行命令
    nest push --nest NAME LOCAL_PATH [REMOTE_PATH]            推送文件/目录到远端 nest（递归）
    nest pull --nest NAME REMOTE_PATH [LOCAL_PATH]            从远端 nest 拉取文件/目录（递归）
    nest sync --nest NAME [--direction push|pull] [--delete] [--dry-run] LOCAL_DIR REMOTE_DIR
                                                              目录递归增量同步（mtime 比对）

配置文件: config/nest.json（install/uninstall 等命令使用）
    nest connect/serve 的 --host 必填，--name 未指定时自动使用系统 hostname
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
import uuid
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


def _make_ws_transport(nest_name: str):
    """构建通过 peng 服务端 ws (bridge 8899) 转发的传输 transport

    供 push/pull/sync 使用：CLI 可运行在 peng 主机或远端 nest 机器上，
    统一连接 nest server 的 forward_request 通道。
    """
    import asyncio

    try:
        import websockets
    except ImportError as e:
        raise RuntimeError(f"缺少 websockets 库: {e}")

    nest_server_port = 8899
    try:
        from peng.config.loader import load_config

        config = load_config()
        nest_cfg = getattr(config, "nest", None)
        if nest_cfg:
            nest_server_port = getattr(nest_cfg, "port", 8899)
    except Exception:
        pass

    from peng.nest.protocol import NestResponse

    class _WsTransport:
        async def request(self, request_type: str, params: dict, timeout: float = 120.0):
            uri = f"ws://localhost:{nest_server_port}"
            async with websockets.connect(uri, max_size=50 * 1024 * 1024) as ws:
                forward_msg = json.dumps(
                    {
                        "type": "forward_request",
                        "request": {"id": str(uuid.uuid4()), "type": request_type, "params": params},
                        "timeout": timeout,
                        "nest_name": nest_name,
                    },
                    ensure_ascii=True,
                )
                await ws.send(forward_msg)
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                resp_data = json.loads(raw)
                if resp_data.get("type") == "error":
                    from peng.nest.protocol import NestError

                    raise NestError(resp_data.get("error", "转发请求失败"))
                return NestResponse.from_dict(resp_data)

    return _WsTransport()


def _fmt_size(n: float) -> str:
    """人类可读字节数"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _cli_push(nest_name: str, local_path: str, remote_path: str | None) -> int:
    """推送文件/目录到远端 nest"""
    import asyncio
    from pathlib import Path

    from peng.nest.transfer import TransferError, push_file, sync_tree

    src = Path(local_path).expanduser()
    if not src.exists():
        print(f"错误: 本地路径不存在: {local_path}", file=sys.stderr)
        return 1

    # 默认远端路径：远端工作目录下同名文件/目录（由 workdir 决定，CLI 端无法预知，
    # 使用相对路径让 nest 端按其 cwd 解析）
    rpath = remote_path

    async def _run():
        transport = _make_ws_transport(nest_name)
        if src.is_dir():
            if rpath is None:
                rpath_ = src.name
            else:
                rpath_ = rpath
            stats = await sync_tree(transport, local_path, rpath_, direction="push")
            return [
                f"✅ 目录已推送到 {nest_name}:{rpath_}",
                f"   检查 {stats['checked']} 个文件: 复制 {stats['copied']}, 跳过 {stats['skipped']}, "
                f"删除 {stats['deleted']}, 失败 {stats['failed']}",
                f"   传输量: {_fmt_size(stats['total_bytes'])}",
            ] + ([f"⚠️ 有 {stats['failed']} 个文件失败"] if stats["failed"] else [])
        rpath_f = rpath or src.name
        result = await push_file(transport, local_path, rpath_f)
        speed = result.get("speed", 0)
        return [
            f"✅ 文件已推送到 {nest_name}:{rpath_f} ({_fmt_size(result['size'])}, {_fmt_size(speed)}/s)"
        ]

    try:
        lines = asyncio.run(_run())
        for line in lines:
            print(line)
        return 0
    except TransferError as e:
        print(f"错误: 推送失败: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n传输已中断", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


def _cli_pull(nest_name: str, remote_path: str, local_path: str | None) -> int:
    """从远端 nest 拉取文件/目录"""
    import asyncio
    from pathlib import Path

    from peng.nest.transfer import TransferError, pull_file, sync_tree

    async def _run():
        transport = _make_ws_transport(nest_name)
        # 先探测远端类型
        resp = await transport.request("stat", {"path": remote_path}, timeout=30.0)
        if resp.error:
            raise TransferError(f"远端路径不可访问: {resp.error}")
        is_dir = bool((resp.data or {}).get("is_dir"))
        if is_dir:
            lpath = local_path or Path(remote_path.rstrip("/\\")).name or "pull_dir"
            stats = await sync_tree(transport, lpath, remote_path, direction="pull")
            return [
                f"✅ 目录已从 {nest_name}:{remote_path} 同步到 {lpath}",
                f"   检查 {stats['checked']} 个文件: 复制 {stats['copied']}, 跳过 {stats['skipped']}, "
                f"删除 {stats['deleted']}, 失败 {stats['failed']}",
                f"   传输量: {_fmt_size(stats['total_bytes'])}",
            ] + ([f"⚠️ 有 {stats['failed']} 个文件失败"] if stats["failed"] else [])
        lpath = local_path or Path(remote_path).name
        result = await pull_file(transport, remote_path, lpath)
        speed = result.get("speed", 0)
        return [
            f"✅ 文件已拉取到 {lpath} ({_fmt_size(result['size'])}, {_fmt_size(speed)}/s)"
        ]

    try:
        lines = asyncio.run(_run())
        for line in lines:
            print(line)
        return 0
    except TransferError as e:
        print(f"错误: 拉取失败: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n传输已中断", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


def _cli_sync(nest_name: str, direction: str, delete: bool, dry_run: bool,
              local_dir: str, remote_dir: str) -> int:
    """目录递归增量同步"""
    import asyncio

    from peng.nest.transfer import TransferError, sync_tree

    async def _run():
        transport = _make_ws_transport(nest_name)
        stats = await sync_tree(
            transport, local_dir, remote_dir, direction=direction,
            delete=delete, dry_run=dry_run,
        )
        verb = "预演同步" if dry_run else "同步"
        arrow = "→" if direction == "push" else "←"
        src_desc = local_dir if direction == "push" else f"{nest_name}:{remote_dir}"
        dst_desc = f"{nest_name}:{remote_dir}" if direction == "push" else local_dir
        return [
            f"✅ {verb}完成: {src_desc} {arrow} {dst_desc}",
            f"   检查 {stats['checked']} 个文件: 复制 {stats['copied']}, 跳过 {stats['skipped']}, "
            f"删除 {stats['deleted']}, 失败 {stats['failed']}",
            f"   传输量: {_fmt_size(stats['total_bytes'])}",
        ] + ([f"⚠️ 有 {stats['failed']} 个文件失败"] if stats["failed"] else [])

    try:
        lines = asyncio.run(_run())
        for line in lines:
            print(line)
        return 0
    except TransferError as e:
        print(f"错误: 同步失败: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n传输已中断", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
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

    # list 子命令（status 别名）
    subparsers.add_parser("list", help="查看服务状态（status 别名）")

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

    # push 子命令：推送文件/目录到远端 nest
    push_parser = subparsers.add_parser("push", help="推送文件/目录到远端 nest（目录递归）")
    push_parser.add_argument("--nest", required=True, help="目标 nest 名称")
    push_parser.add_argument("local_path", help="本地文件或目录路径")
    push_parser.add_argument("remote_path", nargs="?", default=None,
                              help="远端目标路径（可选，默认为远端工作目录下同名文件/目录）")

    # pull 子命令：从远端 nest 拉取文件/目录
    pull_parser = subparsers.add_parser("pull", help="从远端 nest 拉取文件/目录（目录递归）")
    pull_parser.add_argument("--nest", required=True, help="目标 nest 名称")
    pull_parser.add_argument("remote_path", help="远端文件或目录路径")
    pull_parser.add_argument("local_path", nargs="?", default=None,
                              help="本地保存路径（可选，默认当前目录下同名文件/目录）")

    # sync 子命令：目录递归增量同步
    sync_parser = subparsers.add_parser("sync", help="目录递归增量同步（mtime 比对，push/pull 方向）")
    sync_parser.add_argument("--nest", required=True, help="目标 nest 名称")
    sync_parser.add_argument("--direction", choices=["push", "pull"], default="push",
                              help="同步方向：push=本地→远端（默认），pull=远端→本地")
    sync_parser.add_argument("--delete", action="store_true", default=False,
                              help="镜像模式：删除目标端源端没有的文件")
    sync_parser.add_argument("--dry-run", action="store_true", default=False,
                              help="预演模式：只报告将执行的变更，不实际传输")
    sync_parser.add_argument("local_dir", help="本地目录路径")
    sync_parser.add_argument("remote_dir", help="远端目录路径")

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

    if args.command == "push":
        return _cli_push(args.nest, args.local_path, args.remote_path)

    if args.command == "pull":
        return _cli_pull(args.nest, args.remote_path, args.local_path)

    if args.command == "sync":
        return _cli_sync(args.nest, args.direction, args.delete, args.dry_run,
                         args.local_dir, args.remote_dir)

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
    elif args.command == "list":
        return do_status()
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
