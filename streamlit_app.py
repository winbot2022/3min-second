# -*- coding: utf-8 -*-
# 3分セカンドキャリア診断 v0.11（進捗バーUI版）
# - 10問（5段階） → 3軸＋行動意欲スコア
# - 4タイプ（S/R/P/I）
# - 完全匿名（会社名・メール・年齢・属性 一切なし）
# - ChatGPT APIで約400字コメント生成
# - Google Sheets or CSV へログ保存（ai_comment全文も含む）
# - 相談員カード（診断件数付き）＋クリックログ
# - 3軸診断結果を【数値非表示】の進捗バー（ミントグリーン）へ変更

import os
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple, List

import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

# ========= 時刻・定数 =========
JST = timezone(timedelta(hours=9))
APP_VERSION = "second-career-v0.11"
OPENAI_MODEL = "gpt-4o-mini"

ANSWER_HEADER = [
    "timestamp",
    "session_id",
    "result_type",
    "challenge_score",
    "autonomy_score",
    "portfolio_score",
    "action_score",
    "ai_comment",
    "app_version",
]
CLICK_HEADER = [
    "timestamp",
    "session_id",
    "result_type",
    "consultant_id",
]

# ========= Secrets/環境変数 =========
def read_secret(key: str, default=None):
    try:
        return st.secrets[key]
    except Exception:
        return os.environ.get(key, default)

# ========= イベント記録 =========
def report_event(level: str, message: str, payload: dict | None = None):
    if not payload:
        payload = {}
    ts = datetime.now(JST).isoformat(timespec="seconds")
    print(f"[{ts}] [{level}] {message} {payload}")

# ========= Google Sheets / CSV 保存 =========
def _get_gspread_client(service_json_str: str):
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    info = json.loads(service_json_str)
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc

def _append_to_sheet(
    row_dict: dict,
    spreadsheet_id: str,
    service_json_str: str,
    sheet_title: str,
    header: List[str],
):
    gc = _get_gspread_client(service_json_str)
    sh = gc.open_by_key(spreadsheet_id)
    try:
        ws = sh.worksheet(sheet_title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=sheet_title, rows=2000, cols=20)
        ws.append_row(header)

    values = ws.get_all_values()
    if not values:
        ws.append_row(header)

    record = [row_dict.get(k, "") for k in header]
    ws.append_row(record, value_input_option="USER_ENTERED")

def _append_to_csv(row_dict: dict, csv_path: str, header: List[str]):
    df = pd.DataFrame([row_dict])
    if os.path.exists(csv_path):
        df.to_csv(csv_path, mode="a", header=False, index=False, encoding="utf-8")
    else:
        df.to_csv(csv_path, index=False, encoding="utf-8")

def save_answer_row(row: dict):
    secret_json = read_secret("GOOGLE_SERVICE_JSON", None)
    if not secret_json:
        b64 = read_secret("GOOGLE_SERVICE_JSON_BASE64", None)
        if b64:
            try:
                import base64
                secret_json = base64.b64decode(b64).decode("utf-8")
            except Exception as e:
                report_event("ERROR", "Base64 decode error", {"e": str(e)})

    secret_sheet_id = read_secret("SPREADSHEET_ID", None)

    try:
        if secret_json and secret_sheet_id:
            _append_to_sheet(
                row,
                spreadsheet_id=secret_sheet_id,
                service_json_str=secret_json,
                sheet_title="answers_second_career",
                header=ANSWER_HEADER,
            )
        else:
            _append_to_csv(row, "answers_second_career.csv", ANSWER_HEADER)
    except Exception as e:
        report_event("WARN", "save_answer_row error, fallback CSV", {"e": str(e)})
        _append_to_csv(row, "answers_second_career.csv", ANSWER_HEADER)

def save_click_row(row: dict):
    secret_json = read_secret("GOOGLE_SERVICE_JSON", None)
    if not secret_json:
        b64 = read_secret("GOOGLE_SERVICE_JSON_BASE64", None)
        if b64:
            try:
                import base64
                secret_json = base64.b64decode(b64).decode("utf-8")
            except Exception as e:
                report_event("ERROR", "Base64 decode error", {"e": str(e)})

    secret_sheet_id = read_secret("SPREADSHEET_ID", None)

    try:
        if secret_json and secret_sheet_id:
            _append_to_sheet(
                row,
                spreadsheet_id=secret_sheet_id,
                service_json_str=secret_json,
                sheet_title="clicks_second_career",
                header=CLICK_HEADER,
            )
        else:
            _append_to_csv(row, "clicks_second_career.csv", CLICK_HEADER)
    except Exception as e:
        report_event("WARN", "save_click_row error, fallback CSV", {"e": str(e)})
        _append_to_csv(row, "clicks_second_career.csv", CLICK_HEADER)

# ========= OpenAI =========
def _openai_client(api_key: str):
    try:
        from openai import OpenAI
        return "new", OpenAI(api_key=api_key)
    except Exception:
        import openai
        openai.api_key = api_key
        return "old", openai

def generate_ai_comment(result_type: str, scores: Dict[str, float], session_id: str) -> str | None:
    api_key = read_secret("OPENAI_API_KEY", None)
    if not api_key:
        report_event("WARN", "OPENAI_API_KEY not set", {})
        return None

    system_prompt = (
        "あなたは40〜50代の会社員・管理職向けに、"
        "セカンドキャリアを一緒に考えるキャリアアドバイザーです。"
        "診断結果をもとに、相手を評価・断定せず、"
        "ねぎらいと安心感のあるトーンでコメントを書いてください。"
        "医療・投資・法律などの具体アドバイスには踏み込まず、"
        "自己理解を深めるための示唆にとどめてください。"
        "400字前後の日本語で書いてください。"
    )

    user_prompt = (
        f"診断結果はタイプ: {result_type} です。\n"
        f"スコアは以下の通りです。\n"
        f"- 挑戦志向: {scores['challenge']:.1f}\n"
        f"- 自律・独立志向: {scores['autonomy']:.1f}\n"
        f"- ポートフォリオ志向: {scores['portfolio']:.1f}\n"
        f"- 行動意欲: {scores['action']:.1f}\n\n"
        "この結果を踏まえて、本人が自分のこれまでを肯定しつつ、"
        "今後の選択肢を前向きに考えられるようなコメントを書いてください。"
        "『あなたは〜です』と決めつけすぎない表現でお願いします。"
        f"\nセッションID: {session_id}（ログ用、文中に不要）"
    )

    mode, client = _openai_client(api_key)

    try:
        if mode == "new":
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=800,
                temperature=0.7,
            )
            return resp.choices[0].message.content.strip()
        else:
            resp = client.ChatCompletion.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=800,
                temperature=0.7,
            )
            return resp.choices[0].message["content"].strip()
    except Exception as e:
        report_event("ERROR", "AI comment error", {"e": str(e)})
        return None

# ========= 診断ロジック =========

TYPE_TEXT = {
    "S": "いまの延長線上で役割や働き方を少しずつ調整しながら、安定的にキャリアを深めていくスタイルがフィットしやすいタイプです。",
    "R": "すぐに大きく動くよりも、学び直しや副業など、小さな実験を積み重ねながら数年かけてキャリアをシフトしていくタイプです。",
    "P": "ひとつの軸にしばられず、複数の仕事や活動を組み合わせて、自分らしいポートフォリオをつくっていくスタイルが向きやすいタイプです。",
    "I": "自分の看板で仕事をつくることへの関心が強く、中長期的に独立や起業、個人プロとしての活動も選択肢になりやすいタイプです。",
}

def calc_scores(answers: Dict[str, int]) -> Dict[str, float]:
    def mean(vals: List[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    def rev(v: int) -> int:
        return 6 - v

    challenge = mean([answers["Q1"], answers["Q2"], answers["Q3"]])
    autonomy = mean([rev(answers["Q4"]), answers["Q5"], answers["Q6"]])
    portfolio = mean([rev(answers["Q7"]), answers["Q8"], answers["Q9"]])
    action = float(answers["Q10"])

    return {
        "challenge": round(challenge, 2),
        "autonomy": round(autonomy, 2),
        "portfolio": round(portfolio, 2),
        "action": round(action, 2),
    }

def decide_type(scores: Dict[str, float]) -> str:
    ch = scores["challenge"]
    au = scores["autonomy"]
    pf = scores["portfolio"]

    if ch >= 3.5 and au >= 3.5:
        return "I"
    if pf >= 3.5 and au >= 3.0:
        return "P"
    if ch <= 2.5 and au <= 3.0:
        return "S"
    return "R"

# ========= スコア → ラベル変換 =========
def score_to_label(score: float) -> str:
    if score >= 4.5:
        return "とても整っている"
    elif score >= 3.5:
        return "おおむね整っている"
    elif score >= 2.5:
        return "見直しポイントがある"
    elif score >= 1.5:
        return "変化のきっかけをつかみたい"
    else:
        return "立て直しのヒントが必要"

# ========= 相談員データ =========
class Consultant:
    def __init__(
        self,
        id: str,
        name: str,
        title: str,
        bio: str,
        specialties: List[str],
        diagnosis_cases: int,
        contact_url: str,
        photo: str = None,
    ):
        self.id = id
        self.name = name
        self.title = title
        self.bio = bio
        self.specialties = specialties
        self.diagnosis_cases = diagnosis_cases
        self.contact_url = contact_url
        self.photo = photo

def load_consultants() -> List[Consultant]:
    data = [
        {
            "id": "A",
            "name": "山田 太郎",
            "title": "50代管理職の“ゆるやか転身”支援",
            "bio": "大手メーカーで30年勤務後、独立。",
            "specialties": ["50代管理職", "セミリタイア", "副業からの独立"],
            "diagnosis_cases": 34,
            "contact_url": "https://example.com/consultant/yamada",
            "photo": None,
        },
        {
            "id": "B",
            "name": "佐藤 花子",
            "title": "40代女性の“キャリアと暮らし”両立支援",
            "bio": "人事・キャリア支援歴15年。",
            "specialties": ["40代女性", "地方移住", "パラレルワーク"],
            "diagnosis_cases": 21,
            "contact_url": "https://example.com/consultant/sato",
            "photo": None,
        },
        {
            "id": "C",
            "name": "鈴木 一郎",
            "title": "専門職の“独立・プロ化”支援",
            "bio": "専門商社・コンサルを経て独立。",
            "specialties": ["専門職", "フリーランス", "法人化"],
            "diagnosis_cases": 18,
            "contact_url": "https://example.com/consultant/suzuki",
            "photo": None,
        },
    ]
    return [Consultant(**d) for d in data]

# ========= Streamlit アプリ本体 =========
st.set_page_config(
    page_title="3分セカンドキャリア診断",
    page_icon="🧭",
    layout="centered",
)

# ===== カラーテーマ（ミントグリーン） =====
st.markdown(
    """
    <style>
    .stApp { background-color: #d9f5e6; }

    h1, h2, h3 { color: #004d40; }

    div.stButton > button {
        background-color: #00796b;
        color: white;
        border-radius: 999px;
        border: none;
        padding: 0.4rem 1.3rem;
        font-weight: 600;
    }
    div.stButton > button:hover {
        background-color: #00695c;
    }

    .streamlit-expanderHeader {
        font-weight: 600;
        color: #004d40;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ===== 進捗バーCSS（ミントグリーン） =====
progress_css = """
<style>
.progress-bar {
    height: 12px;
    background-color: #d9f5e6;
    border-radius: 6px;
    overflow: hidden;
    margin-top: 6px;
    border: 1px solid #b5e6d4;
}
.progress-bar-inner {
    height: 100%;
    background-color: #4db6ac;
}
</style>
"""

# セッションID（匿名）
if "session_id" not in st.session_state:
    import uuid
    st.session_state["session_id"] = str(uuid.uuid4())
session_id = st.session_state["session_id"]

st.title("3分セカンドキャリア診断")
st.caption("氏名・メール不要。完全匿名で、これからの働き方のヒントを整理します。")

with st.expander("この診断について（必ずお読みください）", expanded=True):
    st.markdown(
        "- 回答はすべて匿名で記録され、氏名・メールアドレスなどの個人情報は取得しません。\n"
        "- 診断結果は、将来のキャリアを保証するものではありません。\n"
        "- 必要に応じて、専門家との個別相談をご検討ください。"
    )

st.header("1. 質問にお答えください")

options = ["まったく当てはまらない", "あまり当てはまらない", "どちらともいえない", "やや当てはまる", "とても当てはまる"]
score_map = {label: i for i, label in enumerate(options, start=1)}

answers: Dict[str, int] = {}

# === 設問 ===（そのまま）

# A: 挑戦志向
st.subheader("A. 変化への向き合い方（挑戦志向）")
answers["Q1"] = score_map[st.radio("Q1. 現在の仕事に“大きな変化”...", options, index=2)]
answers["Q2"] = score_map[st.radio("Q2. 不確実性があっても...", options, index=2)]
answers["Q3"] = score_map[st.radio("Q3. あまり変わらない未来...", options, index=2)]

# B: 自律
st.subheader("B. 組織との距離感（自律・独立志向）")
answers["Q4"] = score_map[st.radio("Q4. 組織で働く安心感...", options, index=2)]
answers["Q5"] = score_map[st.radio("Q5. 裁量をどれだけ重視するか？", options, index=2)]
answers["Q6"] = score_map[st.radio("Q6. 個人の名前で仕事を受ける...", options, index=2)]

# C: ポートフォリオ
st.subheader("C. 働き方の組み合わせ方（ポートフォリオ志向）")
answers["Q7"] = score_map[st.radio("Q7. 専門を深める志向", options, index=2)]
answers["Q8"] = score_map[st.radio("Q8. 異分野活動の楽しさ", options, index=2)]
answers["Q9"] = score_map[st.radio("Q9. 本業＋複数活動の魅力", options, index=2)]

# D: 行動意欲
st.subheader("D. 行動に踏み出す準備度")
answers["Q10"] = score_map[st.radio("Q10. この1〜2年で行動したいか？", options, index=2)]

submitted = st.button("診断する")

if submitted:
    scores = calc_scores(answers)
    result_type = decide_type(scores)
    ai_comment = generate_ai_comment(result_type, scores, session_id) or ""

    row = {
        "timestamp": datetime.now(JST).isoformat(timespec="seconds"),
        "session_id": session_id,
        "result_type": result_type,
        "challenge_score": scores["challenge"],
        "autonomy_score": scores["autonomy"],
        "portfolio_score": scores["portfolio"],
        "action_score": scores["action"],
        "ai_comment": ai_comment,
        "app_version": APP_VERSION,
    }
    save_answer_row(row)

    st.session_state["result_type"] = result_type
    st.session_state["scores"] = scores
    st.session_state["ai_comment"] = ai_comment

# ========= 結果表示 =========
if "result_type" in st.session_state:
    result_type = st.session_state["result_type"]
    scores = st.session_state["scores"]
    ai_comment = st.session_state["ai_comment"]

    st.header("2. あなたの診断結果")
    st.subheader(f"タイプ：{result_type}（{TYPE_TEXT[result_type][:10]}…）")
    st.write(TYPE_TEXT[result_type])

    # ======= ★ 進捗バー（状態ラベル）UI 完全差し替え =======
    st.markdown(progress_css, unsafe_allow_html=True)
    st.markdown("### 3つの側面から見た現在地")

    axis_names = {
        "challenge": "挑戦志向（変化への向き合い方）",
        "autonomy": "自律・独立志向（組織との距離感）",
        "portfolio": "ポートフォリオ志向（働き方の組み合わせ）",
    }

    for key in ["challenge", "autonomy", "portfolio"]:
        score = scores[key]
        label = score_to_label(score)
        rate = score / 5

        st.markdown(f"#### {axis_names[key]}")
        st.markdown(f"**{label}**")

        st.markdown(
            f"""
            <div class="progress-bar">
                <div class="progress-bar-inner" style="width:{rate * 100}%"></div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.write("")

    # 行動意欲は別途（数値を見せず）
    st.subheader("行動に踏み出す準備度")
    st.write("ご自身のペースで進めていく準備が少しずつ整いつつある状態です。")

    # ======= AI コメント =======
    st.markdown("### AIからのコメント（約400字）")
    if ai_comment:
        st.write(ai_comment)
    else:
        st.caption("AIコメントの生成に失敗しました。")

    # ========= 相談員カード =========
    st.header("3. キャリア相談員のご紹介（外部サイト）")
    st.caption(
        "※ 以下の相談員は独立したキャリア相談専門家です。"
        "ご相談は、各相談員と直接やり取りいただきます。"
    )

    consultants = load_consultants()

    for c in consultants:
        st.markdown("---")
        cols = st.columns([1, 2])

        # 左：写真
        with cols[0]:
            if c.photo and os.path.exists(c.photo):
                st.image(c.photo, use_container_width=True)
            else:
                st.caption("（写真準備中）")

        # 右：プロフィール
        with cols[1]:
            st.markdown(f"**{c.name}**")
            st.caption(c.title)
            st.write(c.bio)
            st.write("得意分野：" + "｜".join(c.specialties))
            st.write(f"対応実績：{c.diagnosis_cases}件")

        # 相談ボタン（ログ付き）
        if st.button(f"この相談員に相談する（ID: {c.id}）", key=f"btn_{c.id}"):
            click_row = {
                "timestamp": datetime.now(JST).isoformat(timespec="seconds"),
                "session_id": session_id,
                "result_type": result_type,
                "consultant_id": c.id,
            }
            save_click_row(click_row)
            url = f"{c.contact_url}?src=3min_second_career&c={c.id}"
            st.markdown(f"[相談ページを開く]({url})")

else:
    st.caption("全ての質問に回答したあと、「診断する」ボタンを押してください。")















