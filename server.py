import asyncio
import json
import re
import time
import uuid
import sys
import os
import threading
import datetime

from auto_inject import inject_message

def get_timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

try:
    import pexpect
except ImportError:
    print("Please install pexpect: pip install pexpect")
    sys.exit(1)

# 黑名单正则
BLACKLIST_PATTERN = re.compile(r'(rm\s+-rf|drop\s+table|>\s*/etc)', re.IGNORECASE)

# 增强版：去除了单侧尖括号 '>' 的触发，保留冒号、问号、井号、美元符，以及各类括号和 y/n
BROAD_PROMPT_PATTERN = re.compile(r'([:?\$#]\s*|\[.*?\]\s*|\(.*?\)\s*|\b(y/n|yes/no)\b\s*)$', re.IGNORECASE)

class AgentTask:
    def __init__(self, task_id, cmd, summary, heartbeat, broadcast_callback):
        self.task_id = task_id
        self.cmd = cmd
        self.summary = summary
        self.heartbeat = heartbeat
        self.broadcast_callback = broadcast_callback
        
        self.start_time = time.time()
        self.intervention_count = 0
        self.status = "pending" # pending, blocked, running, finished
        self.is_sync_waiting = False  # 是否正处于同步等待短连接结果的窗口期
        self.last_heartbeat = time.time() # 记录上一次心跳（或主动快照）的时间
        
        # 👇 新增：交互阻塞状态锁，用于防止重复触发报警
        self.is_interactive_waiting = False
        
        self.stdout_buf = []
        self.stderr_buf = [] # pexpect通常将stderr合并到stdout，这里保留逻辑
        self.current_output = ""
        
        self.process = None
        self.last_output_time = time.time()
        self.monitor_task = None
        self.read_task = None

    @property
    def uptime(self):
        return int(time.time() - self.start_time)

    def get_snapshot(self):
        return {
            "task_id": self.task_id,
            "cmd": self.cmd,
            "summary": self.summary,
            "uptime_seconds": self.uptime,
            "intervention_count": self.intervention_count,
            "status": self.status
        }

    def get_full_snapshot(self, event_type):
        if event_type == "finished":
            # 任务结束时，不进行任何省略，保留完整控制台输出
            stdout_lines = "".join(self.stdout_buf)
            full_stdout = stdout_lines + self.current_output
            stderr_lines = "".join(self.stderr_buf)
        else:
            # 心跳等其他查询，只截取最后 10 行
            stdout_lines = "".join(self.stdout_buf[-10:])
            
            # 针对进度条这种狂刷 \r 而不换行的情况，只取最后一次 \r 后的有效信息
            curr_out = self.current_output
            if '\r' in curr_out:
                curr_out = curr_out.split('\r')[-1]
                
            full_stdout = stdout_lines + curr_out
            
            # 强制字符数兜底保护，防止心跳日志过长
            if len(full_stdout) > 1000:
                full_stdout = "...\n(截断以防超过大模型上下文限制)\n...\n" + full_stdout[-1000:]
                
            stderr_lines = "".join(self.stderr_buf[-3:])
            
        # 👇 新增：将 suspicious_stagnation 加入主动推送白名单
        push_types = ("heartbeat", "finished", "suspicious_stagnation")
        return {
            "type": "event_push" if event_type in push_types else "snapshot_response",
            "timestamp": get_timestamp(),
            "task_id": self.task_id,
            "event": event_type,
            "uptime_seconds": self.uptime,
            "intervention_count": self.intervention_count,
            "tail_stdout": full_stdout,
            "tail_stderr": stderr_lines
        }

    async def send_snapshot(self, event_type):
        # 1. 广播给 Socket 监听的客户端 (如果还有活跃连接的话)
        if self.broadcast_callback:
            msg = json.dumps(self.get_full_snapshot(event_type)) + "\n"
            await self.broadcast_callback(msg)
            
        # 2. 直接主动通过 auto_inject 注入到 OpenClaw UI 浏览器中！
        msg_dict = self.get_full_snapshot(event_type)
        uptime = msg_dict.get('uptime_seconds', 0)
        
        ts = get_timestamp()
        if event_type == "heartbeat":
            content = (f"[来自AsynCLI注入，非用户输入] 常规心跳包 [{ts}]\n"
                       f"任务 ID: {self.task_id}\n"
                       f"执行耗时: {uptime} 秒\n"
                       f"当前状态: 运行中\n"
                       f"最近终端输出:\n{msg_dict['tail_stdout']}\n"
                       f"【操作规范】请分析上述日志并向用户简要汇报进度。如遇需要交互（如 [y/N]）或疑似死循环，请务必先询问用户，在获得明确授权后方可调用 input 或 kill 指令，切勿擅自做主！")
        # 👇 新增：针对停滞触发事件，构造紧急注入格式
        elif event_type == "suspicious_stagnation":
            content = (f"[来自AsynCLI注入，非用户输入] 疑似交互阻塞警告 [{ts}]\n"
                       f"任务 ID: {self.task_id}\n"
                       f"执行耗时: {uptime} 秒\n"
                       f"当前状态: 运行中 (终端已停滞3秒且疑似等待输入)\n"
                       f"末尾终端输出:\n```text\n{msg_dict['tail_stdout']}\n```\n"
                       f"【操作要求】任务似乎卡在提示符。请分析上述日志，如果它确实在等待输入（如 [y/N]、密码、选项等），请调用 `send_input` 提供正确的输入项（记得加换行符 \\n）。如果不确定，请向用户询问。如是正常卡顿请忽略。")
        elif event_type == "finished":
            content = (f"任务执行完毕 [{ts}]\n"
                       f"任务 ID: {self.task_id}\n"
                       f"总耗时: {uptime} 秒\n"
                       f"最终终端输出:\n{msg_dict['tail_stdout']}")
        else:
            content = json.dumps(msg_dict, ensure_ascii=False)
            
        print(f"\n[{ts}] [INJECT] 触发事件 '{event_type}'，正在向 OpenClaw UI 注入消息...")
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, inject_message, content)
            print(f"\n[{get_timestamp()}] [INJECT] 注入成功！")
        except Exception as e:
            print(f"\n[{get_timestamp()}] [!] 注入失败: {e}")

    def start(self):
        # 强制设置 Python 脚本输出无缓冲模式，解决管道块缓冲导致的日志延迟
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        
        # 兼容Windows测试
        if sys.platform == 'win32':
            import pexpect.popen_spawn
            # 去掉 encoding 参数，使用原始字节流读取
            self.process = pexpect.popen_spawn.PopenSpawn(self.cmd, env=env)
        else:
            self.process = pexpect.spawn(self.cmd, env=env)
        self.status = "running"
        self.start_time = time.time()
        self.last_output_time = time.time()

    async def run(self):
        self.start()
        # 启动后台读取协程
        loop = asyncio.get_running_loop()
        self.read_task = asyncio.create_task(self._read_output())
        self.monitor_task = asyncio.create_task(self._monitor())

    async def _read_output(self):
        loop = asyncio.get_running_loop()
        while self.status == "running" and self.process is not None:
            try:
                # 异步读取原始字节流
                chunk_bytes = await loop.run_in_executor(None, self.process.read_nonblocking, 1024, 0.5)
                if chunk_bytes:
                    # 尝试 UTF-8 解码，如果失败则回退到 GBK（兼容 Windows 终端默认输出）
                    try:
                        chunk_str = chunk_bytes.decode('utf-8')
                    except UnicodeDecodeError:
                        chunk_str = chunk_bytes.decode('gbk', errors='replace')
                    
                    self.current_output += chunk_str
                    self.last_output_time = time.time()
                    
                    # 按行保存
                    if '\n' in self.current_output:
                        
                        self.is_interactive_waiting = False
                        
                        lines = self.current_output.split('\n')
                        for line in lines[:-1]:
                            self.stdout_buf.append(line + '\n')
                            # 移除原先的 100 行截断保护，以保证 finished 时能取到全部控制台输出
                        self.current_output = lines[-1]
            except pexpect.EOF:
                self.status = "finished"
                # 如果此时不是由于创建/输入正在同步等待5秒返回的窗口期，就异步注入完结快照
                if not self.is_sync_waiting:
                    await self.send_snapshot("finished")
                break
            except pexpect.TIMEOUT:
                pass
            except Exception as e:
                print(f"Read error: {e}")
                break

    async def _monitor(self):
        self.last_heartbeat = time.time()
        while self.status == "running":
            await asyncio.sleep(1)
            now = time.time()
            
            is_prompt = bool(BROAD_PROMPT_PATTERN.search(self.current_output))
            
            if not is_prompt:
                self.is_interactive_waiting = False
            
            if now - self.last_output_time >= 3.0 and self.current_output:
                if is_prompt and not self.is_interactive_waiting:
                    self.is_interactive_waiting = True  # 咔嗒！上锁
                    await self.send_snapshot("suspicious_stagnation")
                    
                    # 重置正常心跳周期，防止报警和心跳连发
                    self.last_heartbeat = now
                    continue
            
            # 纯粹且统一的心跳检查，彻底杜绝重复触发
            if now - self.last_heartbeat >= self.heartbeat:
                await self.send_snapshot("heartbeat")
                self.last_heartbeat = now

    def write_input(self, text):
        if self.process and self.status == "running":
            self.intervention_count += 1
            self.process.send(text)
            self.last_output_time = time.time()
            
            # 手动在控制台缓冲区中回显用户的输入，模拟真实终端的回显行为
            self.current_output += text
            
            # 处理因为人工注入而可能产生的换行
            if '\n' in self.current_output:
                # 👇 新增：人为注入导致换行，也必须砸开锁！
                self.is_interactive_waiting = False
                
                lines = self.current_output.split('\n')
                for line in lines[:-1]:
                    self.stdout_buf.append(line + '\n')
                self.current_output = lines[-1]

    def kill(self):
        self.status = "finished"
        if self.process:
            try:
                # 兼容跨平台的进程终止机制
                if hasattr(self.process, 'terminate'):
                    self.process.terminate(force=True)  # Unix pexpect.spawn
                elif hasattr(self.process, 'kill'):
                    self.process.kill(9) # Windows pexpect.popen_spawn
            except Exception as e:
                print(f"[{get_timestamp()}] [!] 终止进程异常: {e}")
                
        if self.monitor_task:
            self.monitor_task.cancel()
        if self.read_task:
            self.read_task.cancel()


class AgentBrokerServer:
    def __init__(self, host='127.0.0.1', port=8888):
        self.host = host
        self.port = port
        self.tasks = {}
        self.active_writers = set()

    async def broadcast(self, msg: str):
        print(f"\n[{get_timestamp()}] 推送事件:\n  {msg.strip()}")
        dead_writers = set()
        for writer in self.active_writers:
            try:
                writer.write(msg.encode('utf-8'))
                await writer.drain()
            except Exception as e:
                dead_writers.add(writer)
                
        for w in dead_writers:
            self.active_writers.discard(w)

    async def handle_client(self, reader, writer):
        addr = writer.get_extra_info('peername')
        print(f"\n[{get_timestamp()}] [+] 客户端已连接: {addr}")
        self.active_writers.add(writer)
        
        buffer = ""
        try:
            while True:
                # 改用块读取，支持多行格式化的 JSON
                data = await reader.read(4096)
                if not data:
                    break
                
                # 兼容不同编码下发的 JSON 串
                try:
                    text = data.decode('utf-8')
                except UnicodeDecodeError:
                    text = data.decode('gbk', errors='ignore')
                    
                buffer += text
                
                # 尝试从缓冲区中不断解析出完整的 JSON 对象
                while buffer:
                    buffer = buffer.lstrip()
                    if not buffer:
                        break
                        
                    try:
                        decoder = json.JSONDecoder()
                        obj, idx = decoder.raw_decode(buffer)
                        print(f"\n[{get_timestamp()}] [接收] 从 {addr} 收到有效请求:\n  {json.dumps(obj, ensure_ascii=False)}")
                        await self.process_request(obj, writer)
                        
                        # 按要求：处理完一条有效指令后，服务端强制主动断开短连接
                        print(f"\n[{get_timestamp()}] [*] 请求已处理，服务端主动断开连接 {addr}")
                        return
                    except json.JSONDecodeError:
                        # 如果发生解析错误，说明当前数据还不够组成完整的 JSON 对象，跳出等下一波
                        break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"\n[{get_timestamp()}] [!] 连接异常 {addr}: {e}")
        finally:
            print(f"\n[{get_timestamp()}] [-] 客户端已断开: {addr}")
            self.active_writers.discard(writer)
            try:
                writer.close()
            except:
                pass

    async def process_request(self, req, writer):
        action = req.get("action")
        
        if action == "create":
            cmd = req.get("cmd", "")
            summary = req.get("summary", "No summary")
            heartbeat = req.get("heartbeat", 60)
            
            task_id = str(uuid.uuid4())[:8]
            
            # 物理熔断检测
            if BLACKLIST_PATTERN.search(cmd):
                task = AgentTask(task_id, cmd, summary, heartbeat, self.broadcast)
                task.status = "blocked"
                self.tasks[task_id] = task
                
                resp = {
                    "type": "alert",
                    "timestamp": get_timestamp(),
                    "task_id": task_id,
                    "msg": "Command blocked by security rules. Use confirm_danger to execute.",
                    "status": "blocked"
                }
                print(f"\n[{get_timestamp()}] [发送] 回复请求:\n  {json.dumps(resp, ensure_ascii=False)}")
                writer.write((json.dumps(resp) + "\n").encode('utf-8'))
                await writer.drain()
            else:
                task = AgentTask(task_id, cmd, summary, heartbeat, self.broadcast)
                self.tasks[task_id] = task
                asyncio.create_task(task.run())
                
                task.is_sync_waiting = True
                # 发起新任务仍按要求等待最多 5 秒，以捕获瞬间完成或报错的短任务
                for _ in range(50):
                    if task.status == "finished":
                        break
                    await asyncio.sleep(0.1)
                task.is_sync_waiting = False
                
                if task.status == "finished":
                    # 5 秒内执行完毕，直接通过短连接返回所有结果，不再异步注入
                    resp = task.get_full_snapshot("finished")
                    resp["type"] = "task_finished_early"
                    # 执行完毕后终止并清理任务
                    del self.tasks[task_id]
                else:
                    # 还在执行中，返回当前快照并立刻断开，在后台继续
                    resp = task.get_full_snapshot("running_snapshot")
                    resp["type"] = "task_started"
                    
                print(f"\n[{get_timestamp()}] [SEND] 回复请求:\n  {json.dumps(resp, ensure_ascii=False)}")
                writer.write((json.dumps(resp) + "\n").encode('utf-8'))
                await writer.drain()

        elif action == "confirm_danger":
            task_id = req.get("task_id")
            if task_id in self.tasks:
                task = self.tasks[task_id]
                if task.status == "blocked":
                    asyncio.create_task(task.run())
                    resp = {"type": "confirmed", "timestamp": get_timestamp(), "task_id": task_id, "status": "running"}
                    print(f"\n[{get_timestamp()}] [SEND] 回复请求:\n  {json.dumps(resp, ensure_ascii=False)}")
                    writer.write((json.dumps(resp) + "\n").encode('utf-8'))
                    await writer.drain()

        elif action == "input":
            task_id = req.get("task_id")
            text = req.get("text", "")
            if task_id not in self.tasks or self.tasks[task_id].status == "finished":
                resp = {"type": "error", "timestamp": get_timestamp(), "task_id": task_id, "msg": "该任务不存在或已经执行完毕"}
                print(f"\n[{get_timestamp()}] [SEND] 回复请求:\n  {json.dumps(resp, ensure_ascii=False)}")
                writer.write((json.dumps(resp) + "\n").encode('utf-8'))
                await writer.drain()
            else:
                task = self.tasks[task_id]
                task.write_input(text)
                print(f"\n[{get_timestamp()}] [*] 已将输入内容 '{text.strip()}' 注入至任务 {task_id}。")
                
                # 瞬间沿着 Socket 返回当前的最新快照
                resp = task.get_full_snapshot("running_snapshot")
                resp["type"] = "task_running_after_input"
                print(f"\n[{get_timestamp()}] [SEND] 回复请求:\n  {json.dumps(resp, ensure_ascii=False)}")
                writer.write((json.dumps(resp) + "\n").encode('utf-8'))
                await writer.drain()

        elif action == "kill":
            task_id = req.get("task_id")
            if task_id not in self.tasks or self.tasks[task_id].status == "finished":
                resp = {"type": "error", "timestamp": get_timestamp(), "task_id": task_id, "msg": "该任务不存在或已经执行完毕"}
                print(f"\n[{get_timestamp()}] [SEND] 回复请求:\n  {json.dumps(resp, ensure_ascii=False)}")
                writer.write((json.dumps(resp) + "\n").encode('utf-8'))
                await writer.drain()
            else:
                self.tasks[task_id].kill()
                resp = {"type": "killed", "timestamp": get_timestamp(), "task_id": task_id}
                del self.tasks[task_id]
                print(f"\n[{get_timestamp()}] [SEND] 回复请求:\n  {json.dumps(resp, ensure_ascii=False)}")
                writer.write((json.dumps(resp) + "\n").encode('utf-8'))
                await writer.drain()

        elif action == "adjust_heartbeat":
            task_id = req.get("task_id")
            seconds = req.get("seconds", 60)
            if task_id not in self.tasks or self.tasks[task_id].status == "finished":
                resp = {"type": "error", "timestamp": get_timestamp(), "task_id": task_id, "msg": "该任务不存在或已经执行完毕"}
                print(f"\n[{get_timestamp()}] [SEND] 回复请求:\n  {json.dumps(resp, ensure_ascii=False)}")
                writer.write((json.dumps(resp) + "\n").encode('utf-8'))
                await writer.drain()
            else:
                self.tasks[task_id].heartbeat = seconds
                print(f"\n[{get_timestamp()}] [*] 任务 {task_id} 的心跳时间已被修改为 {seconds} 秒")
                resp = {"type": "adjusted", "timestamp": get_timestamp(), "task_id": task_id}
                writer.write((json.dumps(resp) + "\n").encode('utf-8'))
                await writer.drain()

        elif action == "list":
            active_tasks = [t.get_snapshot() for t in self.tasks.values() if t.status in ("running", "blocked")]
            resp = {
                "type": "task_list",
                "timestamp": get_timestamp(),
                "tasks": active_tasks
            }
            print(f"\n[{get_timestamp()}] [SEND] 回复请求:\n  {json.dumps(resp, ensure_ascii=False)}")
            writer.write((json.dumps(resp) + "\n").encode('utf-8'))
            await writer.drain()

        elif action == "snapshot":
            task_id = req.get("task_id")
            if task_id not in self.tasks:
                resp = {"type": "error", "timestamp": get_timestamp(), "task_id": task_id, "msg": "该任务不存在"}
                print(f"\n[{get_timestamp()}] [SEND] 回复请求:\n  {json.dumps(resp, ensure_ascii=False)}")
                writer.write((json.dumps(resp) + "\n").encode('utf-8'))
                await writer.drain()
            else:
                task = self.tasks[task_id]
                
                # 重置该任务的内部心跳时钟，延后下一次心跳的自动触发，避免重复灌输信息
                task.last_heartbeat = time.time()
                print(f"\n[{get_timestamp()}] [*] 任务 {task_id} 已手动拉取快照，其自动心跳倒计时被重置。")
                
                resp = task.get_full_snapshot("manual_snapshot")
                resp["type"] = "snapshot_response"
                print(f"\n[{get_timestamp()}] [SEND] 回复请求:\n  {json.dumps(resp, ensure_ascii=False)}")
                writer.write((json.dumps(resp) + "\n").encode('utf-8'))
                await writer.drain()

    async def start(self):
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        print(f"\n[{get_timestamp()}] AgentBrokerServer running on {self.host}:{self.port}")
        print("="*80)
        print("【重要提示: Playwright 注入前置准备】")
        print("为了让心跳与结果成功注入 OpenClaw，请务必先通过以下命令启动 Chrome 的 Debug 模式：")
        print(r'& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\temp\chrome_debug"')
        print("并在该浏览器中保持打开 OpenClaw 的 Dashboard 页面！")
        print("="*80)
        async with server:
            await server.serve_forever()

if __name__ == '__main__':
    server = AgentBrokerServer()
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("Server stopped.")