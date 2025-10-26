# newmain.py
# 适配：微信桌面版 4.0.5 + wxauto4 开源版
# 功能：在指定群聊中监听以"#举手"开头的消息，提取问题，调用阿里云百炼 Application.call，并自动回复。
# 更新：新增问题级去重 processed_questions，防止同一问题被多次回答。

import os
import time
import re
from http import HTTPStatus
from typing import Set, Tuple, Optional

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

def try_quote_message(msg: Message, text: str) -> None:
    """如果支持 msg.quote，则引用原消息"""
    try:
        if hasattr(msg, "quote") and callable(msg.quote):
            msg.quote(text)
    except Exception as e:
        print(f"quote 异常：{e}")

def safe_send_in_subwindow(chat, content: str) -> None:
    """
    在子窗口模式下发送消息：
    - 文档说明"当子窗口时，who 参数无效"，所以使用 chat.SendMsg 或不传 who。
    """
    try:
        if hasattr(chat, "SendMsg") and callable(chat.SendMsg):
            chat.SendMsg(content)
            return
    except Exception as e:
        print(f"chat.SendMsg 异常：{e}")
    try:
        wx.SendMsg(content)
    except Exception as e:
        print(f"wx.SendMsg 异常：{e}")

def on_message(msg: Message, chat):
    try:
        if not is_target_group(chat):
            return
        if not is_group_chat(chat):
            return

        text = str(getattr(msg, "content", "") or getattr(msg, "text", "") or "")
        if not text.strip():
            return

        question = extract_question(text)
        if not question:
            return

        # 先做"问题级去重"
        if not remember_question_once(question):
            return

        # 再做"消息级去重"
        signature = make_message_signature(msg)
        if signature in processed_messages:
            return
        processed_messages.add(signature)

        sender = str(getattr(msg, "sender", "") or getattr(msg, "author", ""))
        print(f"[{get_chat_name(chat)}] {sender} 举手提问（去重后处理）：{question}")

        answer = call_dashscope(question)
        reply_text = REPLY_TEMPLATE.format(answer=answer)

        try_quote_message(msg, reply_text)
        time.sleep(0.2)
        safe_send_in_subwindow(chat, reply_text)

    except Exception as e:
        print(f"on_message 异常：{e}")

def main():
    try:
        sub = wx.AddListenChat(nickname=TARGET_GROUP_NAME, callback=on_message)
        if hasattr(sub, "ChatInfo"):
            info = sub.ChatInfo()
            print(f"监听子窗口信息: {info}")
        else:
            print(f"监听返回: {sub}")
    except Exception as e:
        print(f"AddListenChat 失败：{e}")
        return
    try:
        if hasattr(wx, "StartListening"):
            wx.StartListening()
            print("已调用 StartListening()")
    except Exception as e:
        print(f"StartListening 异常：{e}")

    print(f"已添加群聊监听：目标群聊={TARGET_GROUP_NAME}，触发关键词={TRIGGER_PREFIX}")
    wx.KeepRunning()

if __name__ == "__main__":
    main()
