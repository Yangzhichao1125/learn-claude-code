#!/usr/bin/env python3
"""
s05_todo_write.py - TodoWrite

The model tracks its progress through a TodoManager. After three rounds
without an update, the harness adds a reminder alongside the tool results.

    +----------+      +-------+      +--------------+
    |   User   | ---> |  LLM  | ---> | Tools        |
    |  prompt  |      |       |      | + todo_write |
    +----------+      +---^---+      +------+-------+
                          |                 | update
                          |          +------v----------+
                          |          | TodoManager     |
                          |          | [ ] pending     |
                          |          | [>] in progress |
                          |          | [x] completed   |
                          |          +------+----------+
                          | tool_result     |
                          +-----------------+

              rounds_since_todo >= 3 -> add <reminder>
"""

# =============================================================================
# 1. 导入与运行环境初始化
# =============================================================================
# ast        : 安全解析 Python 字面量字符串，作为 JSON 解析失败时的兜底
# json       : 解析模型传来的 JSON 字符串（todos 有时不是 list，而是字符串）
# os         : 读取环境变量、判断操作系统
# re         : 编译危险命令匹配规则
# subprocess : 执行 bash 命令
# Path       : 跨平台路径拼接与文件读写
import ast
import json
import os
import re
import subprocess
from pathlib import Path

# macOS/Linux 的终端输入默认没有历史记录和行编辑，readline 可以补上。
# Windows 上可能没有这个模块，所以 ImportError 时直接跳过。
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

# Anthropic 官方 SDK 和 dotenv（从 .env 读取 API Key、模型 ID 等配置）
from anthropic import Anthropic
from dotenv import load_dotenv

# override=True：让 .env 中的值覆盖 shell 里已有的同名环境变量
load_dotenv(override=True)
# 如果使用第三方兼容网关（配置了 ANTHROPIC_BASE_URL），
# 就移除 AUTH_TOKEN，避免 SDK 同时带两种鉴权信息。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# WORKDIR 就是 agent 的工作区：所有相对路径都以它为基准。
WORKDIR = Path.cwd()
# client 负责和模型 API 通信；base_url 为空时用 Anthropic 官方地址。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 模型 ID 必须由环境变量 MODEL_ID 提供，缺了会直接报 KeyError。
MODEL = os.environ["MODEL_ID"]

# =============================================================================
# 2. System Prompt：告诉模型它是谁、在哪工作、先做计划
# =============================================================================
# s05 change: SYSTEM prompt adds planning guidance
# Windows 和 Unix 的 shell 语法不同，先根据操作系统给模型一段环境说明。
ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax, and prefer dedicated file tools for file operations"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)
# 这里相比 s04 新增了最后两句：要求模型在多步任务开始前先用 todo_write 做计划，
# 并在执行过程中持续更新状态。这是 s05 的核心改动之一。
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
    "Before starting any multi-step task, use todo_write to plan your steps. "
    "Update status as you go."
)


# =============================================================================
# 3. 基础工具实现（来自 s02-s04：bash / 读 / 写 / 编辑 / 搜索）
# =============================================================================
# -- Tool implementations from s02-s04 --

def run_bash(command: str) -> str:
    """执行一条 shell 命令，返回 stdout + stderr 的合并结果。"""
    try:
        # shell=True：把整条字符串交给系统 shell 解释（支持管道、重定向等）。
        # cwd=WORKDIR：命令在项目根目录下执行。
        # text=True：以字符串而非 bytes 返回；errors="replace" 避免编码错误崩溃。
        # timeout=120：防止命令卡死，超过 120 秒抛 TimeoutExpired。
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, errors="replace", timeout=120)
        # 合并标准输出和标准错误，并去掉首尾空白。
        out = (r.stdout + r.stderr).strip()
        # 截断超长输出，避免一次性塞爆模型上下文；没有输出时给个明确提示。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    """读取文本文件；limit 指定最多读取多少行，用于大文件。"""
    try:
        # 相对路径拼到 WORKDIR 下，resolve() 规范化 ..、软链接等。
        # 注意：这里不检查路径是否越界，越界检查在 permission_hook 里做。
        lines = (WORKDIR / path).resolve().read_text(encoding="utf-8").splitlines()
        # 超出 limit 时只保留前 limit 行，并追加一行提示告诉模型还有多少行。
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    """把内容写入文件；父目录不存在时自动创建。"""
    try:
        file_path = (WORKDIR / path).resolve()
        # parents=True 创建多层父目录；exist_ok=True 目录已存在时不报错。
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """在文件中把 old_text 精确替换成 new_text，只替换第一次出现的位置。"""
    try:
        file_path = (WORKDIR / path).resolve()
        text = file_path.read_text(encoding="utf-8")
        # 必须先确认旧文本存在，否则直接报错，避免模型以为修改成功了。
        if old_text not in text:
            return f"Error: text not found in {path}"
        # replace(..., 1) 中的 1 表示只替换第一处，防止误改多处相同内容。
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

def run_glob(pattern: str) -> str:
    """按 glob 模式查找文件，例如 **/*.py；结果限制在工作区内，最多返回 200 条。"""
    import glob as g
    try:
        # set 去重；sorted 让输出稳定，便于模型阅读。
        matches = sorted({
            match for match in g.glob(
                pattern, root_dir=WORKDIR, recursive=True)
            # 过滤掉解析后跳出 WORKDIR 的匹配项（例如通过软链接逃逸）。
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
        })
        # 结果太多时只展示前 200 条，并提示模型缩小搜索范围。
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# =============================================================================
# 4. s05 核心：TodoManager
# =============================================================================
# 模型每轮都能看到这份任务清单，相当于给 agent 加了一份"外部记忆"。
# -- New in s05: structured state the model updates --

class TodoManager:
    """维护当前会话的 todo 列表，并负责校验、存储和渲染。"""

    def __init__(self):
        # 每个元素形如 {"content": "写测试", "status": "pending"}
        self.items: list[dict] = []

    def update(self, todos: list | str) -> str:
        """用新列表整体替换旧列表，成功返回渲染后的文本，失败抛 ValueError。

        todos 既可能是真正的 list，也可能是模型生成的 JSON 字符串
        （有些模型会把数组序列化成字符串再传进来）。
        """
        if isinstance(todos, str):
            # 先按标准 JSON 解析，例如 '[{"content": "a", "status": "pending"}]'
            try:
                todos = json.loads(todos)
            except json.JSONDecodeError:
                # 有些模型会输出 Python 风格的单引号字符串，
                # ast.literal_eval 能安全解析字面量，不会执行任意代码。
                try:
                    todos = ast.literal_eval(todos)
                except (SyntaxError, ValueError) as e:
                    raise ValueError("todos must be a list or JSON array string") from e

        # 解析后必须是数组，不能是单个对象或普通字符串。
        if not isinstance(todos, list):
            raise ValueError("todos must be a list")
        # 限制数量，避免模型一次塞入无限多任务，撑爆上下文。
        if len(todos) > 20:
            raise ValueError("Max 20 todos allowed")

        # validated 保存"清洗过"的任务，只有通过全部校验才会替换旧状态。
        validated = []
        in_progress_count = 0
        for index, todo in enumerate(todos):
            if not isinstance(todo, dict):
                raise ValueError(f"todos[{index}] must be an object")

            # content 去掉首尾空白；status 统一转小写，默认 pending。
            content = str(todo.get("content", "")).strip()
            status = str(todo.get("status", "pending")).lower()
            # 任务内容不能为空，否则清单没有意义。
            if not content:
                raise ValueError(f"todos[{index}] requires content")
            # 状态只能是三种枚举值之一，防止模型写出奇怪的状态。
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"todos[{index}] has invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            # 只保留需要的字段，丢掉模型多传的无用字段。
            validated.append({"content": content, "status": status})

        # 业务约束：同一时刻只允许一个任务处于进行中，避免 agent 并行跑偏。
        if in_progress_count > 1:
            raise ValueError("Only one todo can be in_progress at a time")

        # 校验全部通过后，才一次性替换状态（原子更新，不会留下半成品）。
        self.items = validated
        return self.render()

    def render(self) -> str:
        """把内部列表渲染成人类和模型都容易读的多行文本。"""
        if not self.items:
            return "No todos."

        lines = []
        for todo in self.items:
            # 不同状态用不同符号表示：待办、进行中、已完成。
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
            }[todo["status"]]
            lines.append(f"{marker} {todo['content']}")

        # bool 在 Python 里可以当 0/1 相加，这里统计已完成数量。
        done = sum(todo["status"] == "completed" for todo in self.items)
        # 末尾追加进度摘要，例如 (2/5 completed)。
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)


# 全局单例：整个进程共用一份清单，所以同一轮对话中状态会持续保留。
TODO = TodoManager()


def run_todo_write(todos: list | str) -> str:
    """todo_write 工具的处理函数：更新清单并返回渲染结果。"""
    try:
        output = TODO.update(todos)
    except ValueError as e:
        # 校验失败不抛给主循环，而是把错误作为 tool_result 返回给模型，
        # 模型看到错误后可以修正参数重试。
        return f"Error: {e}"
    # 在终端打印当前任务（黄色），方便用户观察 agent 的计划。
    print(f"\n\033[33m## Current Tasks\033[0m\n{output}")
    return output

# =============================================================================
# 5. 工具定义：TOOLS 是给模型看的"说明书"
# =============================================================================
# 模型不会直接读 Python 函数，它只看到这里 JSON Schema 描述的工具名和参数。
# input_schema 规定参数类型、必填项、枚举值、最大长度等。
TOOLS = [
    # 1) 执行 shell 命令
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    # 2) 读取文件，可选的 limit 控制最多读多少行
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    # 3) 写入文件（覆盖写），path 和 content 都必填
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    # 4) 精确文本替换，只替换第一处
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    # 5) 按 glob 模式查找文件
    {"name": "glob", "description": "Find files matching a glob pattern; ** matches recursively.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    # 6) s05 新增：todo_write。todos 是数组，最多 20 项；
    #    每项必须有 content，status 只能是 pending / in_progress / completed。
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "maxItems": 20, "items": {"type": "object", "properties": {"content": {"type": "string", "minLength": 1}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
]

# 工具名 → Python 处理函数的映射，主循环靠它做分发。
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
}


# =============================================================================
# 6. Hook 系统（来自 s04）
# =============================================================================
# HOOKS 是一个"事件 → 回调函数列表"的注册表。
# 主循环在关键节点调用 trigger_hooks(event, ...)，就可以在不改主循环的前提下
# 插入权限检查、日志、审计等逻辑。
# -- Hook system from s04 --

# 四个生命周期事件：用户提交输入、工具执行前、工具执行后、循环结束前。
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    """把回调函数追加到某个事件的列表里；注册顺序就是执行顺序。"""
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    """依次执行某个事件下的所有 hook；任一 hook 返回非 None 就立即短路。"""
    for callback in HOOKS[event]:
        # *args 把触发时收到的参数原样转交给每个 hook。
        result = callback(*args)
        # 返回非 None 通常表示"拦截"：例如权限拒绝的提示文本。
        if result is not None:
            return result
    return None

# 绝对禁止的命令片段：一旦包含就直接拒绝，不给用户确认机会。
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
# 更精细的危险命令识别：匹配处于命令位置的 rm / del，而不是文件名里的字母。
DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)
# 其他可疑关键字：命中后询问用户是否允许，而不是直接拒绝。
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


def contains_destructive_command(command: str) -> bool:
    """命令中是否出现了位于命令位置的 rm / del。"""
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))


def permission_hook(block):
    """PreToolUse: s03 permission logic, registered as an s04 hook."""
    # 只对 bash 工具做命令级检查；其他工具检查的是路径。
    if block.name == "bash":
        command = block.input.get("command", "")
        # 第一层：黑名单直接拒绝，连确认都不给。
        for pattern in DENY_LIST:
            if pattern in command:
                print(f"\n\033[31m[blocked] '{pattern}'\033[0m")
                return "Permission denied by deny list"
        # 第二层：可疑但可能合法的命令，交给用户判断。
        if contains_destructive_command(command) or any(
            keyword in command for keyword in DESTRUCTIVE
        ):
            print(f"\n\033[33m[permission] Potentially destructive command\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    # 文件工具：如果 resolve 后跑到 WORKDIR 外面，询问用户是否允许。
    if block.name in ("read_file", "write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m[permission] Access outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    # 返回 None 表示放行，trigger_hooks 会继续执行下一个 hook。
    return None

def log_hook(block):
    """PreToolUse: log every tool call."""
    # 只打印前两个参数值，并截断到 60 个字符，避免日志刷屏。
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None

def large_output_hook(block, output):
    """PostToolUse: warn on large output."""
    # PostToolUse 在工具执行完之后触发，所以能拿到 output 并检查大小。
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None

def context_inject_hook(query: str):
    """UserPromptSubmit: log working directory."""
    # 用户每次提交输入时触发；这里只是打印工作目录，也可以在这里注入额外上下文。
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

def summary_hook(messages: list):
    """Stop: print tool call count."""
    # 统计整段对话里产生了多少个 tool_result，用来观察 agent 的工作量。
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

# 注册 hook：同一事件可以注册多个，按下面的顺序执行。
# PreToolUse 先权限检查，再打日志；如果权限检查拦截，日志 hook 不会执行。
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


# =============================================================================
# 7. Agent 主循环 + 未更新 todo 的提醒计数器
# =============================================================================
# -- Agent loop with the reminder counter --

def agent_loop(messages: list):
    """反复调用模型 → 执行工具，直到模型不再请求工具为止。"""
    # 记录"连续多少轮没有调用 todo_write"；达到 3 轮就注入提醒。
    rounds_since_todo = 0
    while True:
        # 把完整对话历史 + 工具定义发给模型。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 助手这一轮的回复必须原样追加到历史里，模型下一轮才能看到自己的工具调用。
        messages.append({"role": "assistant", "content": response.content})

        # 从回复内容里挑出所有 tool_use 块。
        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        # 没有工具调用 = 模型认为任务结束。
        if not tool_calls:
            # 退出前触发 Stop hook；如果 hook 返回内容，就把它当作新消息继续循环。
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        # results 收集本轮所有工具的执行结果，最后作为一条 user 消息发回模型。
        results = []
        # 本轮是否调用过 todo_write，用来决定计数器是清零还是加一。
        used_todo = False
        for block in tool_calls:
            # 工具执行前先过 hook 链（权限检查 + 日志）。
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                # 被拦截时也要返回 tool_result，否则模型会一直等结果。
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            # 根据工具名找到对应的 Python 函数。
            handler = TOOL_HANDLERS.get(block.name)
            try:
                # **block.input 把模型给的参数字典拆成关键字参数。
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
            except Exception as e:
                # 工具异常不终止 agent，而是作为错误结果返回给模型自行修正。
                output = f"Error: {e}"

            # 工具执行后触发 PostToolUse（例如检查输出是否过大）。
            trigger_hooks("PostToolUse", block, output)

            # 只要本轮用过 todo_write，就认为模型在维护计划。
            if block.name == "todo_write":
                used_todo = True

            # 把结果和对应的 tool_use_id 配对，模型才能知道是哪个调用的结果。
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": str(output)})

        # 本轮更新过 todo → 计数清零；否则计数 +1。
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        # 连续 3 轮没更新 todo，就在工具结果后面追加一条提醒文本。
        if rounds_since_todo >= 3:
            results.append({"type": "text",
                            "text": "<reminder>Update your todos.</reminder>"})
            # 提醒发出后重新计数，避免每轮都重复提醒。
            rounds_since_todo = 0

        # 把本轮全部结果（以及可能的提醒）作为 user 消息发回模型。
        messages.append({"role": "user", "content": results})


# =============================================================================
# 8. 交互式入口：读取用户输入并驱动 agent_loop
# =============================================================================
if __name__ == "__main__":
    print("s05: TodoWrite - plan before execution")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    # history 保存整段会话（用户消息、助手消息、tool_result），跨多轮输入持续存在。
    history = []
    while True:
        try:
            # \001/\002 tell Readline the ANSI escapes have zero display width.
            # 这两个控制字符告诉 readline 颜色转义不占显示宽度，避免光标错位。
            query = input("\001\033[36m\002s05 >> \001\033[0m\002")
        except (EOFError, KeyboardInterrupt):
            # Ctrl+C 或 Ctrl+D 正常退出。
            break
        if not query.strip():
            # 空输入直接忽略。
            continue
        if query.strip().lower() in ("q", "exit"):
            # 输入 q 或 exit 退出程序。
            break
        # 用户提交输入时触发 UserPromptSubmit hook（目前用于打印工作目录）。
        trigger_hooks("UserPromptSubmit", query)
        # 把用户问题加入历史，然后进入 agent 循环。
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # agent 结束时，history 最后一条是助手的最终回复；打印其中的文本块。
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
