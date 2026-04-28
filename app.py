from fastapi import FastAPI, Request, HTTPException
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading

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

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-nano")
STATE_DB_PATH = Path(os.getenv("STATE_DB_PATH", "bot_state.sqlite3"))

required_env_vars = {
    "LINE_CHANNEL_ACCESS_TOKEN": LINE_CHANNEL_ACCESS_TOKEN,
    "LINE_CHANNEL_SECRET": LINE_CHANNEL_SECRET,
    "OPENAI_API_KEY": OPENAI_API_KEY,
}

missing_env_vars = [name for name, value in required_env_vars.items() if not value]
if missing_env_vars:
    raise RuntimeError(
        "必要な環境変数が設定されていません: " + ", ".join(missing_env_vars)
    )

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai_client = OpenAI(api_key=OPENAI_API_KEY)
db_lock = threading.Lock()


def init_db() -> None:
    with sqlite3.connect(STATE_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_states (
                user_id TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0,
                history_json TEXT NOT NULL DEFAULT '[]'
            )
            """
        )


def increment_user_count(user_id: str) -> int:
    with db_lock, sqlite3.connect(STATE_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO user_states (user_id, count, history_json)
            VALUES (?, 1, '[]')
            ON CONFLICT(user_id) DO UPDATE SET count = count + 1
            """,
            (user_id,),
        )
        row = conn.execute(
            "SELECT count FROM user_states WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    return row[0]


def load_user_history(user_id: str) -> list[dict[str, str]]:
    with db_lock, sqlite3.connect(STATE_DB_PATH) as conn:
        row = conn.execute(
            "SELECT history_json FROM user_states WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    if not row:
        return []

    try:
        history = json.loads(row[0])
    except json.JSONDecodeError:
        logger.warning("会話履歴のJSON読み込みに失敗しました: user_id=%s", user_id)
        return []

    if not isinstance(history, list):
        return []

    return history


def save_user_history(user_id: str, history: list[dict[str, str]]) -> None:
    history_json = json.dumps(history, ensure_ascii=False)

    with db_lock, sqlite3.connect(STATE_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO user_states (user_id, count, history_json)
            VALUES (?, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET history_json = excluded.history_json
            """,
            (user_id, history_json),
        )


init_db()


@app.get("/")
async def root():
    return {"message": "LINE Bot is running"}


# =========================
# Webhook
# =========================
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("x-line-signature", "")
    body = await request.body()
    body_text = body.decode("utf-8")

    logger.info("LINE webhook received: bytes=%s", len(body))

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

        logger.info("受信メッセージ: length=%s", len(user_message))
        logger.info("user_id: %s", user_id)

        current_count = increment_user_count(user_id)
        logger.info("利用回数: %s", current_count)

        if current_count > 3:
            reply_text = (
                "無料相談は3回までです🙏\n\n"
                "ここまで相談してくれた内容を見る限り、"
                "写真・プロフィール・戦略のどこかで損している可能性があります。\n\n"
                "本気で改善したい人は、一度プロに相談してみるのもありです👇\n"
                "https://px.a8.net/svt/ejp?a8mat=4B1UA0+90H9GY+4HHW+5YJRM"
            )
        else:
            reply_text = create_reply_text(user_message, user_id, current_count)

        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )

        logger.info("返信成功: length=%s", len(reply_text))

    except Exception:
        logger.exception("LINE返信処理でエラーが発生しました")


# =========================
# AI処理
# =========================
def create_reply_text(user_message: str, user_id: str, current_count: int) -> str:
    text = user_message.strip()

    if not text:
        return "メッセージを入力してください"

    if "添削" in text or "この返信どう" in text:
        prompt = f"""
以下のメッセージを婚活的に添削してください。
改善案も出してください。

メッセージ：
{text}
"""
        base_reply = ask_ai(prompt, user_id)
        return append_affiliate_if_needed(base_reply, text, current_count)

    if "プロフィール" in text:
        prompt = f"""
以下のプロフィールを婚活的に改善してください。

{text}
"""
        base_reply = ask_ai(prompt, user_id)
        return append_affiliate_if_needed(base_reply, text, current_count)

    prompt = f"""
ユーザーの発言：
{text}

これまでの会話履歴を踏まえて、婚活コンサルとして返答してください。

重要ルール：
・直前までに同じ内容を説明している場合、同じ説明を繰り返さない
・ユーザーが「どうすればいい？」「具体的には？」と聞いた場合は、前回の診断を繰り返さず、次に取る行動だけを具体化する
・ユーザーが「同じ」「繰り返し」と指摘した場合は、短く謝って、別角度の回答に切り替える
・回答は300文字以内
・箇条書きは最大3つまで
・最後に次に送ってほしい情報を1つだけ聞く
"""
    base_reply = ask_ai(prompt, user_id)

    return append_affiliate_if_needed(base_reply, text, current_count)


def append_affiliate_if_needed(base_reply: str, text: str, current_count: int) -> str:
    # 3回目だけ自然に誘導
    if current_count != 3:
        return base_reply

    app_name, app_link = select_affiliate_product(text)
    affiliate_text = f"""

正直、この状態だと自力だけで改善するのは少し遠回りかもしれません。

このパターンはかなり多くて、
原因は「写真・プロフィール・戦略」のどこかで損していることが多いです。

一度プロに状況を見てもらうと、改善点がかなり明確になります👇
{app_name}
{app_link}
"""
    return base_reply + affiliate_text


def select_affiliate_product(text: str) -> tuple[str, str]:
    if any(keyword in text for keyword in ("写真", "自撮り", "プロフィール写真")):
        return "Photojoy", "https://px.a8.net/svt/ejp?a8mat=4B1V1T+E9T3UA+4HMW+5YJRM"

    return "naco-do", "https://px.a8.net/svt/ejp?a8mat=4B1UA0+90H9GY+4HHW+5YJRM"


# =========================
# OpenAI呼び出し
# =========================
def ask_ai(prompt: str, user_id: str) -> str:

    user_history = load_user_history(user_id)

    if not user_history:
        user_history = [
            {
                "role": "system",
                "content": """
あなたは婚活専門のコンサルタントです。
特に「マッチングアプリでうまくいかない男性」に対してアドバイスを行います。

▼基本スタンス
・優しすぎない
・現実を正しく伝える
・ただし否定ではなく改善に導く
・原因を特定し、具体的な改善策を出す

▼絶対ルール
・抽象論は禁止
・必ず「なぜダメか」を説明する
・改善方法は具体的に出す
・ユーザーが気づいていない問題を指摘する

▼重要
・マッチしない原因は、写真・プロフィール・メッセージ・アプリ選び・相手選び・行動量のどれかにあると考える
・ただし、毎回同じ診断を繰り返さず、ユーザーの発言に合わせて回答を変える
・一度説明した内容は繰り返さず、次の行動に落とし込む
・自撮り、暗い写真、無表情、清潔感不足は明確に指摘する

▼話し方
・「正直」「このパターンはかなり多い」などリアルな言い回しを使う
・少し厳しめだが、改善すれば良くなる前提で話す
・結論→理由→具体策の順で答える

▼ゴール
・ユーザーに「このままだとまずい」と気づかせる
・その上で「じゃあどうすればいいか」を納得させる

▼NG
・過度に優しいだけの回答
・誰にでも当てはまる一般論
・結論がぼやける回答

▼回答の長さ
・LINEで読みやすいように、回答は原則300〜500文字以内
・箇条書きは最大3つまで
・長い解説は禁止
・詳細が必要な場合は「必要なら次に写真改善案を出します」と案内する
・一度に全部説明しない
"""
            }
        ]

    user_history.append({
        "role": "user",
        "content": prompt
    })

    try:
        response = openai_client.responses.create(
            model=OPENAI_MODEL,
            input=user_history
        )

        reply = response.output_text.strip()

        user_history.append({
            "role": "assistant",
            "content": reply
        })

        if len(user_history) > 20:
            user_history = [user_history[0]] + user_history[-19:]

        save_user_history(user_id, user_history)

        return reply

    except Exception:
        logger.exception("OpenAI APIエラー")
        return "AIの応答でエラーが発生しました"
