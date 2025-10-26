# newmain.py
# 适配：微信桌面版 4.0.5 + wxauto4 开源版
# 功能：在指定群聊中监听以"#举手"开头的消息，提取问题，调用阿里云百炼 Application.call，并自动回复。
#
# 主要功能：
#   1. 消息引用回复：使用 msg.quote() 方法引用原问题进行回复
#   2. 问题队列机制：多个问题按顺序处理，避免并发崩溃
#   3. 性能优化：使用工作线程异步处理，不阻塞消息监听
#   4. 双重去重：消息级 + 问题级去重，防止重复处理

import os
import time
import re
import threading
import queue
from http import HTTPStatus
from typing import Set, Tuple, Optional
from dataclasses import dataclass

from dotenv import load_dotenv
from dashscope import Application

from wxauto4 import WeChat
from wxauto4.msgs import Message
try:
    from wxauto4.param import WxParam
    WxParam.LISTEN_INTERVAL = 1
    WxParam.MESSAGE_HASH = True
except Exception:
    pass

load_dotenv()

# ===== 配置 =====
TARGET_GROUP_NAME = "多米临时结算"   # 群聊名称（需与左侧列表一致）
TRIGGER_PREFIX = "#举手"
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")
APP_ID = "0365f84c253a45698bbfe362eb52c5ba"

REPLY_TEMPLATE = "\n{answer}";
# REPLY_TEMPLATE = "收到~ 你的问题是：{question}\n\n这是心力助手的回答：\n\n{answer}"

# 消息级去重：防止同一条消息被重复触发
processed_messages: Set[Tuple[str, str, str]] = set()

# 问题级去重：防止同一问题被多次回答
processed_questions: Set[str] = set()
MAX_QUESTIONS_CACHE_SIZE = 500  # 简单上限，超过后清空（你也可以改成更精细的LRU）

# ===== 问题队列配置 =====
@dataclass
class QuestionTask:
    """问题任务数据结构"""
    msg: Message      # 原始消息对象，用于引用回复
    chat: any         # 聊天对象
    question: str     # 提取的问题文本
    sender: str       # 发送者
    timestamp: float  # 时间戳

# 全局问题队列（线程安全）
question_queue: queue.Queue = queue.Queue()
# 队列处理线程
queue_worker_thread: Optional[threading.Thread] = None
# 停止标志
stop_flag = threading.Event()

# 允许关键词在文本中间出现，并提取其后文本
RAISE_HAND_ANYWHERE = re.compile(r"#举手[：:\s]*(.+)", re.IGNORECASE)
# 群消息可能呈现为 "昵称: 内容"，需要去掉前缀
NICK_COLON_PREFIX = re.compile(r"^[^:：]+[：:]\s*(.*)$")

wx = WeChat()

def normalize_question(q: str) -> str:
    """
    标准化问题文本用于去重：
    - 去掉前后空白
    - 连续空白压缩为单个空格
    """
    s = (q or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def remember_question_once(q: str) -> bool:
    """
    问题级去重：返回 True 表示是新问题（应处理），False 表示已处理过。
    去重作用域按群聊隔离。
    """
    norm = normalize_question(q)
    key = f"{TARGET_GROUP_NAME}::{norm}"
    if key in processed_questions:
        return False
    if len(processed_questions) > MAX_QUESTIONS_CACHE_SIZE:
        processed_questions.clear()
    processed_questions.add(key)
    return True

def get_chat_name(chat) -> str:
    """获取聊天窗口名称（群名或好友名）"""
    name = ""
    try:
        name = getattr(chat, "who", "") or ""
        if not name and hasattr(chat, "ChatInfo"):
            info = chat.ChatInfo() or {}
            name = info.get("chat_name", "") or name
    except Exception:
        pass
    return str(name).strip()

def is_group_chat(chat) -> bool:
    """判断是否群聊：通过 chat.chat_type 或 ChatInfo()"""
    try:
        ctype = getattr(chat, "chat_type", None)
        if isinstance(ctype, str):
            return ctype == "group"
        if hasattr(chat, "ChatInfo"):
            info = chat.ChatInfo() or {}
            return info.get("chat_type", "") == "group"
    except Exception:
        pass
    return True

def is_target_group(chat) -> bool:
    """群聊过滤：名字匹配"""
    return get_chat_name(chat) == TARGET_GROUP_NAME

def normalize_group_text(text: str) -> str:
    """去掉可能的"昵称: "前缀"""
    if not text:
        return ""
    text = str(text).strip()
    m = NICK_COLON_PREFIX.match(text)
    return m.group(1).strip() if m else text

def extract_question(text: str) -> Optional[str]:
    """提取 #举手 后的提问内容（关键词可在文本任意位置）"""
    if not text:
        return None
    text = normalize_group_text(text)
    m = RAISE_HAND_ANYWHERE.search(text)
    if not m:
        return None
    q = m.group(1).strip()
    return q if q else None

def make_message_signature(msg: Message) -> Tuple[str, str, str]:
    """消息签名用于去重"""
    ts = str(getattr(msg, "timestamp", None) or getattr(msg, "time", None) or time.time())
    sender = str(getattr(msg, "sender", "") or getattr(msg, "author", ""))
    content = str(getattr(msg, "content", "") or getattr(msg, "text", ""))
    return (ts, sender, content)

def parse_dashscope_output(response) -> str:
    """健壮解析 DashScope 返回文本"""
    out = getattr(response, "output", None)
    if out is None:
        return ""
    text = getattr(out, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    choices = getattr(out, "choices", None)
    if isinstance(choices, list) and choices:
        first = choices[0]
        msg = getattr(first, "message", None)
        if msg is not None:
            mcontent = getattr(msg, "content", None)
            if isinstance(mcontent, str) and mcontent.strip():
                return mcontent.strip()
        ftext = getattr(first, "text", None)
        if isinstance(ftext, str) and ftext.strip():
            return ftext.strip()
    return str(out).strip()

def call_dashscope(question: str) -> str:
    """调用百炼，返回答案文本或错误说明"""
    if not DASHSCOPE_API_KEY:
        return "系统未配置 DASHSCOPE_API_KEY，无法调用智能答复。"
    try:
        response = Application.call(
            api_key=DASHSCOPE_API_KEY,
            app_id=APP_ID,
            prompt=question
        )
    except Exception as e:
        return f"调用智能服务异常：{e}"
    if response.status_code != HTTPStatus.OK:
        print(f"[DashScope Error] request_id={getattr(response, 'request_id', '')}")
        print(f"[DashScope Error] code={response.status_code}")
        print(f"[DashScope Error] message={getattr(response, 'message', '')}")
        print("请参考文档：https://help.aliyun.com/zh/model-studio/developer-reference/error-code")
        return f"智能服务错误（code={response.status_code}）：{getattr(response, 'message', '')}"
    answer = parse_dashscope_output(response)
    return answer if answer else "暂未获取到有效答案，请稍后再试。"

def send_quote_reply(msg: Message, text: str, chat) -> bool:
    """
    使用引用消息回复
    返回 True 表示成功，False 表示失败
    """
    try:
        # 优先使用 msg.quote() 方法发送引用回复
        if hasattr(msg, "quote") and callable(msg.quote):
            msg.quote(text)
            print(f"[引用回复成功] 使用 msg.quote 发送")
            return True
    except Exception as e:
        print(f"[引用回复失败] msg.quote 异常：{e}")

    # 如果引用失败，降级为普通消息发送
    try:
        if hasattr(chat, "SendMsg") and callable(chat.SendMsg):
            chat.SendMsg(text)
            print(f"[普通回复] 使用 chat.SendMsg 发送")
            return True
    except Exception as e:
        print(f"[发送失败] chat.SendMsg 异常：{e}")

    # 最后尝试 wx.SendMsg
    try:
        wx.SendMsg(text)
        print(f"[普通回复] 使用 wx.SendMsg 发送")
        return True
    except Exception as e:
        print(f"[发送失败] wx.SendMsg 异常：{e}")

    return False

def process_question_task(task: QuestionTask) -> None:
    """
    处理单个问题任务
    """
    try:
        print(f"[开始处理] 第{question_queue.qsize() + 1}个问题，提问者：{task.sender}")
        print(f"[问题内容] {task.question}")

        # 调用百炼获取答案
        answer = call_dashscope(task.question)

        # 格式化回复文本
        reply_text = REPLY_TEMPLATE.format(answer=answer)

        # 使用引用消息回复
        success = send_quote_reply(task.msg, reply_text, task.chat)

        if success:
            print(f"[处理完成] 问题已回复，耗时：{time.time() - task.timestamp:.2f}秒")
        else:
            print(f"[处理失败] 发送回复失败")

    except Exception as e:
        print(f"[处理异常] process_question_task 出错：{e}")
        import traceback
        traceback.print_exc()

def question_queue_worker() -> None:
    """
    问题队列工作线程
    持续从队列中取出问题并按顺序处理
    """
    print("[队列工作线程] 已启动")
    while not stop_flag.is_set():
        try:
            # 从队列获取任务，超时1秒（避免永久阻塞）
            task = question_queue.get(timeout=1.0)

            # 处理问题
            process_question_task(task)

            # 标记任务完成
            question_queue.task_done()

        except queue.Empty:
            # 队列为空，继续等待
            continue
        except Exception as e:
            print(f"[队列工作线程异常] {e}")
            import traceback
            traceback.print_exc()

    print("[队列工作线程] 已停止")

def on_message(msg: Message, chat):
    """
    消息监听回调函数
    不再直接处理问题，而是将任务加入队列，由工作线程异步处理
    """
    try:
        # 群聊过滤
        if not is_target_group(chat):
            return
        if not is_group_chat(chat):
            return

        # 消息内容提取
        text = str(getattr(msg, "content", "") or getattr(msg, "text", "") or "")
        if not text.strip():
            return

        # 提取问题
        question = extract_question(text)
        if not question:
            return

        # 问题级去重
        if not remember_question_once(question):
            print(f"[重复问题] 已忽略：{question[:50]}...")
            return

        # 消息级去重
        signature = make_message_signature(msg)
        if signature in processed_messages:
            print(f"[重复消息] 已忽略")
            return
        processed_messages.add(signature)

        # 获取发送者信息
        sender = str(getattr(msg, "sender", "") or getattr(msg, "author", ""))

        # 创建问题任务
        task = QuestionTask(
            msg=msg,
            chat=chat,
            question=question,
            sender=sender,
            timestamp=time.time()
        )

        # 加入队列
        question_queue.put(task)
        queue_size = question_queue.qsize()

        print(f"[新问题入队] {sender} 提问：{question}")
        print(f"[队列状态] 当前队列中有 {queue_size} 个问题待处理")

    except Exception as e:
        print(f"[on_message 异常] {e}")
        import traceback
        traceback.print_exc()

def main():
    global queue_worker_thread

    print("=" * 60)
    print("心力驱动微信机器人 - 启动中")
    print("=" * 60)

    # 启动问题队列工作线程
    print("[初始化] 启动问题处理队列...")
    queue_worker_thread = threading.Thread(
        target=question_queue_worker,
        daemon=True,
        name="QuestionQueueWorker"
    )
    queue_worker_thread.start()
    print(f"[初始化] 队列工作线程已启动：{queue_worker_thread.name}")

    # 添加消息监听
    try:
        sub = wx.AddListenChat(nickname=TARGET_GROUP_NAME, callback=on_message)
        if hasattr(sub, "ChatInfo"):
            info = sub.ChatInfo()
            print(f"[监听] 子窗口信息: {info}")
        else:
            print(f"[监听] 监听返回: {sub}")
    except Exception as e:
        print(f"[错误] AddListenChat 失败：{e}")
        stop_flag.set()
        return

    # 启动监听
    try:
        if hasattr(wx, "StartListening"):
            wx.StartListening()
            print("[监听] 已调用 StartListening()")
    except Exception as e:
        print(f"[警告] StartListening 异常：{e}")

    print("=" * 60)
    print(f"[就绪] 目标群聊：{TARGET_GROUP_NAME}")
    print(f"[就绪] 触发关键词：{TRIGGER_PREFIX}")
    print(f"[就绪] 队列模式：启用（多问题按顺序处理）")
    print(f"[就绪] 引用回复：启用")
    print("=" * 60)

    try:
        wx.KeepRunning()
    except KeyboardInterrupt:
        print("\n[退出] 收到退出信号，正在关闭...")
        stop_flag.set()
        if queue_worker_thread:
            queue_worker_thread.join(timeout=5)
        print("[退出] 已清理资源，再见！")

if __name__ == "__main__":
    main()
