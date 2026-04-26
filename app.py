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
user_histories = {}

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-nano")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai_client = OpenAI(api_key=OPENAI_API_KEY)


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

        if user_id not in user_counts:
            user_counts[user_id] = 0

        user_counts[user_id] += 1
        logger.info("利用回数: %s", user_counts[user_id])

        if user_counts[user_id] > 5:
            reply_text = (
                "無料相談は3回までです🙏\n\n"
                "ここまで相談してくれた内容を見る限り、"
                "写真・プロフィール・戦略のどこかで損している可能性があります。\n\n"
                "本気で改善したい人は、一度プロに相談してみるのもありです👇\n"
                "https://www.wealsma.com/"
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

    current_count = user_counts.get(user_id, 0)

    if "添削" in text or "この返信どう" in text:
        prompt = f"""
以下のメッセージを婚活的に添削してください。
改善案も出してください。

メッセージ：
{text}
"""
        return ask_ai(prompt, user_id)

    if "プロフィール" in text:
        prompt = f"""
以下のプロフィールを婚活的に改善してください。

{text}
"""
        return ask_ai(prompt, user_id)

    app_name = "ウェルスマ"
    app_link = "https://www.wealsma.com/"

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

    # 3回目だけ自然に誘導
    if current_count == 3:
        affiliate_text = f"""

正直、この状態だと自力だけで改善するのは少し遠回りかもしれません。

このパターンはかなり多くて、
原因は「写真・プロフィール・戦略」のどこかで損していることが多いです。

一度プロに状況を見てもらうと、改善点がかなり明確になります👇
{app_name}
{app_link}
"""
        return base_reply + affiliate_text

    return base_reply


# =========================
# OpenAI呼び出し
# =========================
def ask_ai(prompt: str, user_id: str) -> str:

    if user_id not in user_histories:
        user_histories[user_id] = [
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

        user_histories[user_id].append({
            "role": "assistant",
            "content": reply
        })

        if len(user_histories[user_id]) > 20:
            user_histories[user_id] = user_histories[user_id][-20:]

        return reply

    except Exception:
        logger.exception("OpenAI APIエラー")
        return "AIの応答でエラーが発生しました"