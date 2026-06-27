# -*- coding: utf-8 -*-
# 3分セカンドキャリア診断 v0.4
# - 10問（5段階） → 5軸スコア（心理・強み・働き方・条件・行動）
# - 4タイプ（S/R/P/I）
# - 完全匿名（会社名・メール・年齢・属性 一切なし）
# - ChatGPT APIで約400字コメント生成（※AIコメント内にスコア・点数は出さない）
# - Google Sheets or CSV へログ保存（ai_comment全文も含む）
# - 相談員カード（診断件数付き）＋クリックログ
# - 診断結果は3軸（心理・強み・働き方）を「線分＋現在地の一点」で表示（数値はユーザーに見せない）

import os
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

# ========= 時刻・定数 =========
JST = timezone(timedelta(hours=9))
APP_VERSION = "second-career-v0.4"
OPENAI_MODEL = "gpt-4o-mini"

ANSWER_HEADER = [
    "timestamp",
    "session_id",
    "result_type",
    "psychological_score",  # 変化への向き合い方（心理的準備度）
    "identity_score",       # 自分の強み・経験の自覚度
    "workstyle_score",      # 働き方の志向
    "constraint_score",     # 働き方を変えやすい条件（制約の小ささ）
    "action_score",         # 行動に踏み出す準備度
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

# ========= イベント記録（ログ用） =========
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

# ========= OpenAI クライアント =========
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

    # ★ ここで「スコア・点数・数値は出さない」ように明示
    system_prompt = (
        "あなたは40〜50代の会社員・管理職向けに、"
        "セカンドキャリアを一緒に考えるキャリアアドバイザーです。"
        "診断結果をもとに、相手を評価・断定せず、"
        "ねぎらいと安心感のあるトーンでコメントを書いてください。"
        "医療・投資・法律などの具体アドバイスには踏み込まず、"
        "自己理解を深めるための示唆にとどめてください。"
        "400字前後の日本語で書いてください。"
        "文章の中では、数値スコアや「◯点」「スコア」「レベル」「評価」など、"
        "点数や評価を連想させる言葉は一切使わないでください。"
    )

    # 数値はあくまで「裏側の情報」として渡しつつ、
    # 出力に出さないように強く指示
    user_prompt = (
        "以下は診断の内部情報です。これらの数値や『スコア』『点数』という言葉は、"
        "出力する文章の中には一切書かないでください。"
        "あくまで、傾向をあなたが理解するためだけの材料です。\n\n"
        f"診断タイプ: {result_type}\n"
        f"- 心理的準備度（変化への向き合い方）: {scores['psychological']:.1f}\n"
        f"- 強み・経験の自覚度: {scores['identity']:.1f}\n"
        f"- 働き方の志向（自律・独立／組織・複業など）: {scores['workstyle']:.1f}\n"
        f"- 働き方を変えやすい条件（家族・健康・時間などの制約の小ささ）: {scores['constraint']:.1f}\n"
        f"- 行動に踏み出す準備度: {scores['action']:.1f}\n\n"
        "この情報をもとに、本人が自分のこれまでのキャリアを肯定しつつ、"
        "今後の選択肢を前向きに考えられるようなコメントを書いてください。"
        "『あなたは〜です』と決めつけすぎない表現でお願いします。"
        "また、「高い・低い」などの優劣を強く感じさせる表現は避け、"
        "その人なりのペースやタイミングを尊重する書き方にしてください。"
        f"\nセッションID: {session_id}（ログ用、文中に書く必要はありません）"
    )

    mode, client = _openai_client(api_key)

    try:
        if mode == "new":
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",    "content": user_prompt},
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
                    {"role": "user",    "content": user_prompt},
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
    """
    answers: Q1〜Q10 → 1〜5
    軸：
      - psychological: Q1, Q2（変化への向き合い方）
      - identity:      Q3, Q4（自分の強み・経験の自覚）
      - workstyle:     Q5, Q6, Q7（働き方の志向）
      - constraint:    Q8（制約の小ささ）
      - action:        Q9, Q10（行動に踏み出す準備度）
    """
    def mean(vals: List[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    psychological = mean([answers["Q1"], answers["Q2"]])
    identity      = mean([answers["Q3"], answers["Q4"]])
    workstyle     = mean([answers["Q5"], answers["Q6"], answers["Q7"]])
    constraint    = float(answers["Q8"])
    action        = mean([answers["Q9"], answers["Q10"]])

    return {
        "psychological": round(psychological, 2),
        "identity":      round(identity, 2),
        "workstyle":     round(workstyle, 2),
        "constraint":    round(constraint, 2),
        "action":        round(action, 2),
    }

def decide_type(scores: Dict[str, float]) -> str:
    """
    I > P > S > R の優先順位で判定
    """
    ps   = scores["psychological"]
    ident = scores["identity"]
    ws   = scores["workstyle"]
    cons = scores["constraint"]   # 値が大きいほど「制約は小さい（動きやすい）」と解釈
    act  = scores["action"]

    # 1. Independent（独立・プロ型）
    if ws >= 4.0 and ps >= 3.6 and cons >= 3.0 and act >= 3.6:
        return "I"

    # 2. Portfolio（複業・ハイブリッド型）
    if ps >= 3.2 and cons >= 3.0 and 3.0 <= ws <= 4.2:
        return "P"

    # 3. Specialist（専門深化・強み型）
    if ident >= 4.0 and ws >= 3.5:
        return "S"

    # 4. Reframe（ゆるやか転身・模索型）
    return "R"

# ========= スコア → とても柔らかいラベル =========
def soft_label(score: float) -> str:
    # 評価・命令・「見直し」という言葉を完全に避ける
    if score >= 4.5:
        return "いま大切にしたい姿が、かなりはっきり見えている状態です。"
    elif score >= 3.5:
        return "どのように働きたいか、その方向性が少しずつ形になってきているようです。"
    elif score >= 2.5:
        return "これから考えを整理していくことで、新しいヒントがいくつか見えてきそうな段階です。"
    elif score >= 1.5:
        return "いまは日々の役割をこなしながら、価値観を少しずつ確かめていくタイミングかもしれません。"
    else:
        return "無理に動く時期ではなく、少し立ち止まってこれまでを振り返る余白がある状態と言えそうです。"

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
    # ここは後で実データに差し替えればOK
    data = [
        {
            "id": "A",
            "name": "山田 太郎",
            "title": "50代管理職の“ゆるやか転身”支援",
            "bio": "大手メーカーで30年勤務後、独立。管理職から専門職・フリーランスへの移行を中心に、延べ300名以上のキャリア相談を実施。",
            "specialties": ["50代管理職", "セミリタイア", "副業からの独立"],
            "diagnosis_cases": 34,
            "contact_url": "https://example.com/consultant/yamada",
            "photo": None,
        },
        {
            "id": "B",
            "name": "佐藤 花子",
            "title": "40代女性の“キャリアと暮らし”両立支援",
            "bio": "人事・キャリア支援歴15年。子育てと仕事の両立、地方移住、副業など、ライフイベントとキャリアの両立をサポート。",
            "specialties": ["40代女性", "地方移住", "パラレルワーク"],
            "diagnosis_cases": 21,
            "contact_url": "https://example.com/consultant/sato",
            "photo": None,
        },
        {
            "id": "C",
            "name": "鈴木 一郎",
            "title": "専門職の“独立・プロ化”支援",
            "bio": "専門商社・コンサルティング会社を経て独立。技術系・専門職のフリーランス化や法人化の相談を多く担当。",
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

# ===== カラーテーマ＋フォント調整・線分スタイル =====
st.markdown(
    """
    <style>
    /* 全体背景 */
    .stApp {
        background-color: #d9f5e6;  /* やさしいミントグリーン */
    }

    /* 見出しカラー */
    h1, h2, h3 {
        color: #004d40;  /* 深めのティール */
    }

    /* 説明文・キャプション・設問ラベルを少し濃く・太めに */
    p, .stMarkdown, .stCaption, label {
        color: #00332f !important;
        font-weight: 700 !important;
    }

    /* --- caption（肩書）の色・太さを強制的に統一 --- */
    p.caption, .caption, .stCaption {
        color: #001a15 !important;   /* bio と同じ深いティール */
        font-weight: 500 !important;
        opacity: 1 !important;
    }

   /* ========================== */
    /*     ボタン（白背景）       */
    /* ========================== */

    /* すべてのボタンをこのスタイルに */
    button, div.stButton > button {
        background-color: #ffffff !important;
        color: #00695c !important;
        border: 2px solid #00695c !important;
        border-radius: 999px !important;
        padding: 0.45rem 1.4rem !important;
        font-weight: 600 !important;
    }

    /* ホバー時：少しミント色に */
    button:hover, div.stButton > button:hover {
        background-color: #e0f4ee !important;
        border-color: #005247 !important;
        color: #004d40 !important;
    }

    /* ========================== */
    /*    線分＋ポイント（前回）    */
    /* ========================== */

    .line-container {
        width: 100%;
        height: 22px;
        position: relative;
        margin: 8px 0 20px 0;
    }
    .line-base {
        position: absolute;
        top: 50%;
        left: 0;
        width: 100%;
        height: 4px;
        background-color: #d5e8e2;
        transform: translateY(-50%);
        border-radius: 2px;
    }
    .line-point {
        position: absolute;
        top: 50%;
        width: 16px;
        height: 16px;
        background-color: #00695c;
        border-radius: 50%;
        transform: translate(-50%, -50%);
    }

    </style>
    """,
    unsafe_allow_html=True,
)

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
        "- 診断結果は、将来のキャリアや収入を保証・推奨するものではありません。\n"
        "- 必要に応じて、専門家との個別相談や会社の制度もあわせてご検討ください。"
    )

st.header("1. 質問にお答えください")

options = [
    "まったく当てはまらない",
    "あまり当てはまらない",
    "どちらともいえない",
    "やや当てはまる",
    "とても当てはまる",
]
score_map = {label: i for i, label in enumerate(options, start=1)}

answers: Dict[str, int] = {}

# A. 心理的準備度
st.subheader("A. 変化への向き合い方（心理的な準備度）")
answers["Q1"] = score_map[st.radio(
    "Q1. 今の仕事や働き方に“変化を加えること”を、どの程度ポジティブに感じますか？",
    options,
    index=2,
)]
answers["Q2"] = score_map[st.radio(
    "Q2. 収入や環境にある程度の変動があっても、「やってみたい仕事」を試してみたいと思いますか？",
    options,
    index=2,
)]

# B. 職業的アイデンティティ（強みの自覚）
st.subheader("B. 自分の強み・経験の自覚")
answers["Q3"] = score_map[st.radio(
    "Q3. 自分の強みや得意分野を、第三者に言葉で説明できる自信がありますか？",
    options,
    index=2,
)]
answers["Q4"] = score_map[st.radio(
    "Q4. これまでの経験の中で、他者から「それはあなたの強みだ」と評価されたスキルや役割が、はっきり思い浮かびますか？",
    options,
    index=2,
)]

# C. 働き方の志向
st.subheader("C. 働き方の志向（組織との距離感・自律性）")
answers["Q5"] = score_map[st.radio(
    "Q5. 組織の方針に沿って働くスタイルより、自分の判断で仕事の進め方を決められる働き方に魅力を感じますか？",
    options,
    index=2,
)]
answers["Q6"] = score_map[st.radio(
    "Q6. 会社の看板ではなく、「あなた個人の名前」で仕事を受けるようなフリーランス的な働き方に対して、抵抗は少ないほうですか？",
    options,
    index=2,
)]
answers["Q7"] = score_map[st.radio(
    "Q7. 一つの専門を深める働き方よりも、複数の仕事や活動を組み合わせる働き方のほうが、自分には合っていると感じますか？",
    options,
    index=2,
)]

# D & E. 条件と行動の準備度
st.subheader("D. ライフステージ・条件と行動の準備度")
answers["Q8"] = score_map[st.radio(
    "Q8. 今後3〜5年のあいだに、働き方を変えるうえで大きな支障となる家族・健康・時間などの制約は「それほど大きくない」と感じますか？",
    options,
    index=2,
)]
answers["Q9"] = score_map[st.radio(
    "Q9. この1年で、キャリアのための学びや情報収集など、小さな一歩を実際に始められそうだと感じますか？",
    options,
    index=2,
)]
answers["Q10"] = score_map[st.radio(
    "Q10. セカンドキャリアに向けて、副業・社外活動・社内での新しい役割など、何らかの具体的な行動を起こすイメージを現実的に持てていますか？",
    options,
    index=2,
)]

submitted = st.button("診断する")

if submitted:
    scores = calc_scores(answers)
    result_type = decide_type(scores)

    ai_comment = generate_ai_comment(result_type, scores, session_id) or ""

    # ログ保存
    answer_row = {
        "timestamp": datetime.now(JST).isoformat(timespec="seconds"),
        "session_id": session_id,
        "result_type": result_type,
        "psychological_score": scores["psychological"],
        "identity_score":      scores["identity"],
        "workstyle_score":     scores["workstyle"],
        "constraint_score":    scores["constraint"],
        "action_score":        scores["action"],
        "ai_comment": ai_comment,
        "app_version": APP_VERSION,
    }
    save_answer_row(answer_row)

    st.session_state["result_type"] = result_type
    st.session_state["scores"] = scores
    st.session_state["ai_comment"] = ai_comment

# ========= 結果表示 =========
if "result_type" in st.session_state:
    result_type = st.session_state["result_type"]
    scores = st.session_state["scores"]
    ai_comment = st.session_state["ai_comment"]

    TYPE_LABEL = {
    "R": "Reframe（ゆるやか転身・模索型）",
    "S": "Specialist（専門深化・強み型）",
    "P": "Portfolio（複業・ハイブリッド型）",
    "I": "Independent（独立・プロ型）",
    }
    
    st.header("2. あなたの診断結果")
    st.subheader(f"タイプ：{TYPE_LABEL[result_type]}")
    st.write(TYPE_TEXT[result_type])

    # ===== 3つの側面＋線分＋現在地だけ（数値は見せない） =====
    st.markdown("### 3つの側面から見た現在地（いまの感触）")

    axis_names = {
        "psychological": "変化への向き合い方（心理的準備度）",
        "identity":      "自分の強み・経験の自覚度",
        "workstyle":     "働き方の志向（組織との距離感・自律性）",
    }

    for key in ["psychological", "identity", "workstyle"]:
        score = scores[key]
        label = soft_label(score)
        # 1〜5 を 0〜1 に変換（左右に「良い・悪い」の意味は持たせない）
        pos = (score - 1.0) / 4.0

        st.markdown(f"#### {axis_names[key]}")
        st.markdown(f"{label}")

        st.markdown(
            f"""
            <div class="line-container">
                <div class="line-base"></div>
                <div class="line-point" style="left:{pos * 100}%"></div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.write("")

    # 条件と行動についてのコメント（テキストのみ）
    st.subheader("行動に踏み出すペースについて")
    st.write(
        "行動の速さにも、その人なりのタイミングがあります。"
        "いまのご自身の状況や体調、家族との関係などを大切にしながら、"
        "「少し気になることから試してみる」くらいのペースで考えてみてください。"
    )

    # ===== AI コメント =====
    st.markdown("### AIからのコメント（自動生成・約400字）")
    if ai_comment:
        st.write(ai_comment)
    else:
        st.caption("AIコメントの生成に失敗しました。時間をおいて再度お試しください。")
    # ========= ウェイティングリスト案内 =========
    st.header("3. 専門家による個別キャリア相談（ウェイティングリスト受付中）")
    
    st.markdown("### 大切なお知らせ")
    
    st.write(
        "現在、本サービスでは、専門家による個別キャリア相談を開始できるよう準備を進めています。"
        "診断結果をもとに、「これからの具体的なアクションを専門家と整理したい」"
        "「個別の相談に乗ってほしい」という方は、無料のウェイティングリストへご登録ください。"
    )
    
    st.write(
        "ご登録いただいた方には、相談体制の準備が整い次第、"
        "優先的にご案内いたします。"
    )
    
    st.markdown("### ウェイティングリストに登録するメリット")
    
    st.markdown(
        """
    - **優先案内**：相談枠が準備できた際、一般案内の前に優先的にお知らせします。
    - **完全無料**：ウェイティングリストへの登録、および初期案内に費用はかかりません。
    - **診断結果をもとに相談**：診断結果を参考にしながら、あなたの状況に合わせて今後の働き方を整理できる相談機会をご案内します。無理な勧誘等は行いません。
    """
    )
    
    from urllib.parse import urlencode

    TYPE_FORM_VALUE = {
        "R": "Reframe（ゆるやか転身・模索型）",
        "S": "Specialist（専門深化・強み型）",
        "P": "Portfolio（複業・ハイブリッド型）",
        "I": "Independent（独立・プロ型）",
    }
    
    GOOGLE_FORM_BASE_URL = "https://docs.google.com/forms/d/e/1FAIpQLSeFS-IAkVAGw2P8DpInu37mo4GNq-clxDYxi-fkbgScqXY3Uw/viewform"
    TYPE_ENTRY_ID = "entry.1399344778"
    
    waiting_url = GOOGLE_FORM_BASE_URL + "?" + urlencode({
        "usp": "pp_url",
        TYPE_ENTRY_ID: TYPE_FORM_VALUE[result_type],
    })
    
    st.link_button(
        "相談開始のお知らせを受け取る（無料）",
        waiting_url
    )

else:
    st.caption("全ての質問に回答したあと、「診断する」ボタンを押してください。")


















