import argparse
import socket
import json
import sys

def main():
    parser = argparse.ArgumentParser(description="AsynCLI 无状态命令行客户端")
    subparsers = parser.add_subparsers(dest="action", help="可用操作")

    # Action: create
    parser_create = subparsers.add_parser("create", help="创建并异步执行新任务")
    parser_create.add_argument("--cmd", required=True, help="要执行的命令行指令")
    parser_create.add_argument("--summary", required=True, help="任务摘要/意图说明")
    parser_create.add_argument("--heartbeat", type=int, default=60, help="心跳间隔（秒），默认 60")

    # Action: input
    parser_input = subparsers.add_parser("input", help="向发生交互阻塞的任务发送输入")
    parser_input.add_argument("--task_id", required=True, help="目标任务 ID")
    parser_input.add_argument("--text", required=True, help="输入内容（例如 y，代码会自动追加换行符）")

    # Action: list
    parser_list = subparsers.add_parser("list", help="查看所有正在运行或阻塞的任务")

    # Action: kill
    parser_kill = subparsers.add_parser("kill", help="强制终止任务")
    parser_kill.add_argument("--task_id", required=True, help="目标任务 ID")

    # Action: confirm_danger
    parser_danger = subparsers.add_parser("confirm_danger", help="强制放行被拦截的危险命令")
    parser_danger.add_argument("--task_id", required=True, help="目标任务 ID")

    # Action: snapshot
    parser_snap = subparsers.add_parser("snapshot", help="立即查询指定任务的当前控制台快照")
    parser_snap.add_argument("--task_id", required=True, help="目标任务 ID")

    # Action: adjust_heartbeat
    parser_adj = subparsers.add_parser("adjust_heartbeat", help="动态修改心跳推送的间隔时间")
    parser_adj.add_argument("--task_id", required=True, help="目标任务 ID")
    parser_adj.add_argument("--seconds", type=int, required=True, help="新的心跳间隔（秒）")

    args = parser.parse_args()

    if not args.action:
        parser.print_help()
        sys.exit(1)

    # 构建底层 JSON 请求
    req = {"action": args.action}
    if args.action == "create":
        req.update({"cmd": args.cmd, "summary": args.summary, "heartbeat": args.heartbeat})
    elif args.action == "input":
        # 对于终端输入，末尾通常需要换行符来模拟按下回车键
        req.update({"task_id": args.task_id, "text": args.text + "\n"})
    elif args.action in ("kill", "confirm_danger", "snapshot"):
        req.update({"task_id": args.task_id})
    elif args.action == "adjust_heartbeat":
        req.update({"task_id": args.task_id, "seconds": args.seconds})
    
    # 通过 Socket 发送给 8888 服务端
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # 设定超时时间 (服务端最长会阻塞 5 秒用于收集执行状态，所以这里给 10 秒缓冲)
            s.settimeout(10.0)
            s.connect(('127.0.0.1', 8888))
            s.sendall((json.dumps(req) + '\n').encode('utf-8'))
            
            response = ""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                try:
                    response += chunk.decode('utf-8')
                except UnicodeDecodeError:
                    response += chunk.decode('gbk', errors='ignore')
                
                # 如果遇到换行符，说明接收到了一条完整的服务端 JSON 响应
                if '\n' in response:
                    break
                    
            print(response.strip())
            
    except ConnectionRefusedError:
        print('{"type": "error", "msg": "无法连接到服务端 (127.0.0.1:8888)，请检查 AsynCLI Server 是否已启动。"}')
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"type": "error", "msg": f"通信发生异常: {str(e)}"}, ensure_ascii=False))
        sys.exit(1)

if __name__ == "__main__":
    main()
