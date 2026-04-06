import asyncio
import json
import sys
from auto_inject import inject_message

async def send_via_telegram(msg):
    # 构造一条自然语言消息以供注入
    if msg.get("type") == "event_push":
        if msg.get("event") == "interactive_blocked":
            content = f"任务(ID:{msg.get('task_id')})发生阻塞，等待交互输入。\n终端输出末尾：\n{msg.get('tail_stdout')}\n请提供下一步操作指令。"
        elif msg.get("event") == "heartbeat":
            content = f"任务(ID:{msg.get('task_id')})心跳状态：运行中。\n最近输出：\n{msg.get('tail_stdout')}"
        else:
            content = json.dumps(msg, ensure_ascii=False)
    else:
        content = json.dumps(msg, ensure_ascii=False)
        
    print(f"\n[OpenClaw Inject 🚀] 正在注入消息至 OpenClaw UI...")
    loop = asyncio.get_running_loop()
    # inject_message 是同步函数且可能阻塞，所以放在 run_in_executor 中执行
    await loop.run_in_executor(None, inject_message, content)

async def read_from_server(reader):
    try:
        while True:
            data = await reader.readline()
            if not data:
                break
            msg = json.loads(data.decode('utf-8').strip())
            
            # 直接投递，由 OpenClaw 的自带队列去处理状态
            await send_via_telegram(msg)
                
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Read error: {e}")

async def main():
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', 8888)
        print("Connected to AgentBrokerServer")
    except Exception as e:
        print(f"Failed to connect: {e}")
        return

    # Start background reader
    read_task = asyncio.create_task(read_from_server(reader))

    def send_cmd(req_dict):
        msg = json.dumps(req_dict) + "\n"
        writer.write(msg.encode('utf-8'))

    print("\n--- Agent-CLI-Broker Client ---")
    print("Commands:")
    print("  1: Create normal task (ping/sleep simulation)")
    print("  2: Create blocked task (rm -rf)")
    print("  3: Confirm danger task (needs task_id)")
    print("  4: Send input to task (needs task_id, text)")
    print("  5: List tasks")
    print("  6: Kill task")
    print("  0: Exit")

    loop = asyncio.get_running_loop()

    while True:
        cmd = await loop.run_in_executor(None, input, "\nSelect option: ")
        
        if cmd == "1":
            # 模拟一个会卡住等待输入的命令
            # Windows/Linux兼容测试可用 python -c "x=input('Do you want to continue? [y/N] ')"
            test_cmd = 'python -c "import time; print(\'Starting...\'); time.sleep(1); x=input(\'Do you want to continue? [y/N] \'); print(f\'You entered: {x}\')"'
            send_cmd({
                "action": "create",
                "cmd": test_cmd,
                "summary": "运行交互式Python测试脚本",
                "heartbeat": 5
            })
        elif cmd == "2":
            send_cmd({
                "action": "create",
                "cmd": "rm -rf /test_dir",
                "summary": "恶意删除目录",
                "heartbeat": 30
            })
        elif cmd == "3":
            tid = await loop.run_in_executor(None, input, "Enter task_id: ")
            send_cmd({
                "action": "confirm_danger",
                "task_id": tid
            })
        elif cmd == "4":
            tid = await loop.run_in_executor(None, input, "Enter task_id: ")
            txt = await loop.run_in_executor(None, input, "Enter text (e.g. y): ")
            send_cmd({
                "action": "input",
                "task_id": tid,
                "text": txt + "\n"
            })
        elif cmd == "5":
            send_cmd({
                "action": "list"
            })
        elif cmd == "6":
            tid = await loop.run_in_executor(None, input, "Enter task_id: ")
            send_cmd({
                "action": "kill",
                "task_id": tid
            })
        elif cmd == "0":
            break

    read_task.cancel()
    writer.close()
    await writer.wait_closed()

if __name__ == '__main__':
    asyncio.run(main())
