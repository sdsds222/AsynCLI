from playwright.sync_api import sync_playwright
import sys

# 该脚本通过 Playwright 连接到已启动的调试模式 Chrome
# 并直接调用 OpenClaw UI 组件的 handleSendChat 钩子，实现用户身份注入。

def inject_message(message):
    try:
        with sync_playwright() as p:
            # 连接到正在运行的调试模式 Chrome 实例 (端口 9222)
            # 确保 Chrome 启动参数包含 --remote-debugging-port=9222
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            context = browser.contexts[0]
            
            # 自动定位打开了 Dashboard 的页面
            dashboard_page = None
            for page in context.pages:
                if "127.0.0.1:18789" in page.url:
                    dashboard_page = page
                    break
            
            if not dashboard_page:
                print("错误: 未找到已打开的 OpenClaw Dashboard 页面 (http://127.0.0.1:18789/)")
                return

            # 使用 page.evaluate 传递变量，而不是在 f-string 中拼接
            # 这样可以完美避开 f-string 不允许反斜杠的问题
            inject_js = """
            (function(msg) {
                const app = document.querySelector('openclaw-app');
                if (app) {
                    app.chatMessage = msg;
                    app.handleSendChat();
                    return "Success";
                }
                return "Error: openclaw-app not found";
            })
            """
            result = dashboard_page.evaluate(inject_js, message)
            print(f"注入结果: {result}")
            
    except Exception as e:
        print(f"注入失败，请确保 Chrome 已以 --remote-debugging-port=9222 启动: {e}")

if __name__ == "__main__":
    msg = sys.argv[1] if len(sys.argv) > 1 else "【自动注入】默认测试指令"
    inject_message(msg)
