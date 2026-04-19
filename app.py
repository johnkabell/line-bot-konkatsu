from fastapi import FastAPI, Request, HTTPException
import logging
import os

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from openai import OpenAI
from dotenv import load_dotenv

# =========================
# 初期設定
# =========================
load_dotenv()
user_counts = {}
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

OPENAI_MODEL = "gpt-5-nano"  # ←安いモデル

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# 🔥 ユーザーごとの履歴
user_histories = {}

# =========================
# Webhook
# =========================
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("x-line-signature", "")
    body = await request.body()
    body_text = body.decode("utf-8")

    logger.info("Request body: %s", body_text)

    try:
        handler.handle(body_text, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception:
        logger.exception("Unexpected error in callback")
        raise HTTPException(status_code=500, detail="Internal server error")

    return "OK"

# =========================
# メッセージ受信
# =========================
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    try:
        user_message = event.message.text
        user_id = event.source.user_id

        logger.info("受信メッセージ: %s", user_message)
        logger.info("user_id: %s", user_id)

        # 初回なら0
        if user_id not in user_counts:
            user_counts[user_id] = 0

        user_counts[user_id] += 1
        logger.info("利用回数: %s", user_counts[user_id])

        # 回数制限チェック
        if user_counts[user_id] > 5:
            reply_text = (
                "無料相談は5回までです🙏\n\n"
                "続きはこちら👇\n"
                "https://あなたのリンク"
            )
        else:
            reply_text = create_reply_text(user_message, user_id)

        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )

        logger.info("返信成功: %s", reply_text)

    except Exception:
        logger.exception("LINE返信処理でエラーが発生しました")

# =========================
# AI処理
# =========================
def create_reply_text(user_message: str, user_id: str) -> str:
    text = user_message.strip()

    if not text:
        return "メッセージを入力してください"

    # 🔥 添削モード
    if "添削" in text or "この返信どう" in text:
        prompt = f"""
以下のメッセージを婚活的に添削してください。
改善案も出してください。

メッセージ：
{text}
"""
        return ask_ai(prompt, user_id)

    # 🔥 プロフィール改善モード
    if "プロフィール" in text:
        prompt = f"""
以下のプロフィールを婚活的に改善してください。

{text}
"""
        return ask_ai(prompt, user_id)

    # 🔥 通常相談
    return ask_ai(text, user_id)

# =========================
# OpenAI呼び出し
# =========================
def ask_ai(prompt: str, user_id: str) -> str:

    if user_id not in user_histories:
        user_histories[user_id] = [
            {
                "role": "system",
                "content": """
あなたは30代男性向けの婚活アドバイザーです。

マッチングアプリ（Pairs、東カレ、ブライダルネットなど）に詳しく、
現実的で具体的な改善案を簡潔に提示してください。

以下を重視してください：
・プロフィール改善
・写真戦略
・メッセージ改善
・デート戦略
・マッチ率向上

ルール：
・抽象論は禁止（必ず具体的に）
・優しさ7：厳しさ3
・結論→理由→具体例の順で話す
"""
            }
        ]

    # ユーザー発言追加
    user_histories[user_id].append({
        "role": "user",
        "content": prompt
    })

    try:
        response = openai_client.responses.create(
            model=OPENAI_MODEL,
            input=user_histories[user_id]
        )

        reply = response.output_text.strip()

        # AIの返答も履歴に追加
        user_histories[user_id].append({
            "role": "assistant",
            "content": reply
        })

        # 履歴制限（重要）
        if len(user_histories[user_id]) > 20:
            user_histories[user_id] = user_histories[user_id][-20:]

        return reply

    except Exception:
        logger.exception("OpenAI APIエラー")
        return "AIの応答でエラーが発生しました"