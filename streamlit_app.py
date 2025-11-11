# -*- coding: utf-8 -*-
# 3分で分かる 資金繰り改善診断｜Victor Consulting
# - 会社名/メール必須、UTM取得、AIコメント自動生成、PDF 1ページ、JST
# - Google Sheets 自動保存（なければ CSV）
# - サイレント保存（利用者に保存メッセージを出さない）
# - 管理者モード（?admin=1 または Secrets: ADMIN_MODE="1"）でイベント確認
# - responsesシートのヘッダー順に完全同期（HEADER_ORDER）

import os
import io
import re
import json
import time
import base64
import tempfile
from datetime import datetime, timedelta, timezone

import streamlit as st
import pandas as pd
import altair as alt
import matplotlib.pyplot as plt

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import registerFontFamily

# Fonts/Images
from matplotlib import font_manager
from PIL import Image as PILImage
import qrcode
import requests

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# ========= ブランド & 定数 =========
BRAND_BG   = "#f0f7f7"
LOGO_LOCAL = "assets/CImark.png"
LOGO_URL   = "https://victorconsulting.jp/wp-content/uploads/2025/10/CImark.png"
CTA_URL    = "https://victorconsulting.jp/spot-diagnosis/"
OPENAI_MODEL = "gpt-4o-mini"
APP_VERSION  = "cf-v1.0.0"  # cashflow

# responses シートの1行目（ヘッダー）に合わせる（製造業版と完全同一）
HEADER_ORDER = [
    "timestamp",
    "company",
    "email",
    "category_scores",
    "total_score",
    "type_label",
    "ai_comment",
    "utm_source",
    "utm_campaign",
    "pdf_url",
    "app_version",
    "status",
    "ai_comment_len",
    "risk_level",
    "entry_check",
    "report_date"
]

# 日本時間
JST = timezone(timedelta(hours=9))

# 画面設定
st.set_page_config(
    page_title="3分で分かる 資金繰り改善診断｜Victor Consulting",
    page_icon="💴",
    layout="centered",
    initial_sidebar_state="expanded"
)

# ========= Secrets/環境変数 =========
def read_secret(key: str, default=None):
    try:
        return st.secrets[key]
    except Exception:
        return os.environ.get(key, default)

# ========= 管理者モード =========
try:
    qp = st.query_params
except Exception:
    qp = st.experimental_get_query_params()
ADMIN_MODE = (str(qp.get("admin", ["0"])[0]) == "1") or (str(read_secret("ADMIN_MODE", "0")) == "1")

# ========= 日本語TTF 登録 =========
def setup_japanese_font():
    candidates = [
        "NotoSansJP-Regular.ttf",
        "/mnt/data/NotoSansJP-Regular.ttf",
        "/content/NotoSansJP-Regular.ttf",
    ]
    font_path = next((p for p in candidates if os.path.exists(p)), None)
    if not font_path:
        return None
    try:
        pdfmetrics.registerFont(TTFont("JP", font_path))
        registerFontFamily("JP", normal="JP", bold="JP", italic="JP", boldItalic="JP")
    except Exception as e:
        print("ReportLab font register error:", e)
    try:
        font_manager.fontManager.addfont(font_path)
        fp = font_manager.FontProperties(fname=font_path)
        import matplotlib as mpl
        mpl.rcParams["font.family"] = fp.get_name()
        mpl.rcParams["axes.unicode_minus"] = False
    except Exception as e:
        print("Matplotlib font register error:", e)
    return font_path
FONT_PATH_IN_USE = setup_japanese_font()

# ========= スタイル =========
st.markdown(
    f"""
<style>
.stApp {{ background: {BRAND_BG}; }}
.block-container {{ padding-top: 2.8rem; }}
h1 {{ margin-top: .6rem; }}
.result-card {{
  background: white; border-radius: 14px; padding: 1.0rem 1.0rem;
  box-shadow: 0 6px 20px rgba(0,0,0,.06); border: 1px solid rgba(0,0,0,.06);
}}
.badge {{ display:inline-block; padding:.25rem .6rem; border-radius:999px; font-size:.9rem;
  font-weight:700; letter-spacing:.02em; margin-left:.5rem; }}
.badge-blue  {{ background:#e6f0ff; color:#0b5fff; border:1px solid #cfe3ff; }}
.badge-yellow{{ background:#fff6d8; color:#8a6d00; border:1px solid #ffecb3; }}
.badge-red   {{ background:#ffe6e6; color:#a80000; border:1px solid #ffc7c7; }}
.small-note {{ color:#666; font-size:.9rem; }}
hr {{ border:none; border-top:1px dotted #c9d7d7; margin:1.0rem 0; }}
</style>
""",
    unsafe_allow_html=True
)

# ========= ロゴ取得 =========
def path_or_download_logo() -> str | None:
    if os.path.exists(LOGO_LOCAL):
        return LOGO_LOCAL
    try:
        r = requests.get(LOGO_URL, timeout=8)
        if r.ok:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
            tmp.write(r.content); tmp.flush()
            return tmp.name
    except Exception:
        pass
    return None

# ========= イベント記録（管理者用） =========
def _report_event(level: str, message: str, payload: dict | None = None):
    evt = {
        "timestamp": datetime.now(JST).isoformat(timespec="seconds"),
        "level": level,
        "message": message,
        "payload": json.dumps(payload, ensure_ascii=False) if payload else ""
    }
    secret_json     = read_secret("GOOGLE_SERVICE_JSON", None)
    secret_sheet_id = read_secret("SPREADSHEET_ID", None)
    wrote = False
    try:
        if secret_json and secret_sheet_id:
            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            info = json.loads(secret_json)
            creds = Credentials.from_service_account_info(info, scopes=scopes)
            gc = gspread.authorize(creds)
            sh = gc.open_by_key(secret_sheet_id)
            try:
                ws = sh.worksheet("events")
            except gspread.WorksheetNotFound:
                ws = sh.add_worksheet(title="events", rows=1000, cols=6)
                ws.append_row(list(evt.keys()))
            ws.append_row([evt[k] for k in evt.keys()])
            wrote = True
    except Exception:
        wrote = False
    if not wrote:
        try:
            df = pd.DataFrame([evt])
            csv_path = "events.csv"
            if os.path.exists(csv_path):
                df.to_csv(csv_path, mode="a", header=False, index=False, encoding="utf-8")
            else:
                df.to_csv(csv_path, index=False, encoding="utf-8")
        except Exception:
            pass
    if ADMIN_MODE:
        st.caption(f"［ADMIN］{level}: {message}")

# ========= 保存系（Sheets/CSV） =========
def try_append_to_google_sheets(row_dict: dict, spreadsheet_id: str, service_json_str: str):
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    info = json.loads(service_json_str)
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.sheet1  # responses

    values = ws.get_all_values()
    if not values:
        ws.append_row(HEADER_ORDER)

    record = [row_dict.get(k, "") for k in HEADER_ORDER]
    ws.append_row(record, value_input_option="USER_ENTERED")

def fallback_append_to_csv(row_dict: dict, csv_path="responses.csv"):
    df = pd.DataFrame([row_dict])
    if os.path.exists(csv_path):
        df.to_csv(csv_path, mode="a", header=False, index=False, encoding="utf-8")
    else:
        df.to_csv(csv_path, index=False, encoding="utf-8")

def auto_save_row(row: dict):
    secret_json     = read_secret("GOOGLE_SERVICE_JSON", None)
    if not secret_json:
        b64 = read_secret("GOOGLE_SERVICE_JSON_BASE64", None)
        if b64:
            try:
                secret_json = base64.b64decode(b64).decode("utf-8")
            except Exception as e:
                _report_event("ERROR", f"Base64デコード失敗: {e}", {})
    secret_sheet_id = read_secret("SPREADSHEET_ID", None)

    def _append_csv():
        try:
            fallback_append_to_csv(row)
        except Exception as e2:
            _report_event("ERROR", f"CSV保存に失敗: {e2}", {"row_head": {k: row.get(k) for k in list(row)[:6]}})

    try:
        if secret_json and secret_sheet_id:
            try_append_to_google_sheets(row, secret_sheet_id, secret_json)
        else:
            _append_csv()
    except Exception as e:
        _append_csv()
        _report_event("WARN", f"Sheets保存に失敗しCSVへフォールバック: {e}", {"reason": str(e)})

# ========= サイドバー =========
with st.sidebar:
    logo_path = path_or_download_logo()
    if logo_path:
        st.image(logo_path, width=150)
    st.markdown("### 3分無料診断")
    st.markdown("- 入力は 2〜3段階＋Yes/部分的/No のみ\n- 機密数値は不要\n- 結果は 6タイプ＋赤/黄/青")
    st.caption("© Victor Consulting")

st.title("3分で分かる 資金繰り改善診断")
st.write("**10問**に答えるだけで、資金繰りの“詰まりどころ”を可視化します。")

# ========= セッション初期化 =========
defaults = {
    "result_ready": False, "df": None, "overall_avg": None, "signal": None,
    "main_type": None, "company": "", "email": "",
    "ai_comment": None, "ai_tried": False,
    "utm_source": "", "utm_medium": "", "utm_campaign": ""
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ========= UTM取得 =========
try:
    q = st.query_params
except Exception:
    q = st.experimental_get_query_params()
st.session_state["utm_source"]   = q.get("utm_source",   [""])[0] if isinstance(q.get("utm_source"), list) else q.get("utm_source", "")
st.session_state["utm_medium"]   = q.get("utm_medium",   [""])[0] if isinstance(q.get("utm_medium"), list) else q.get("utm_medium", "")
st.session_state["utm_campaign"] = q.get("utm_campaign", [""])[0] if isinstance(q.get("utm_campaign"), list) else q.get("utm_campaign", "")

# ========= バリデーション =========
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
def validate_inputs(company: str, email: str) -> tuple[bool, str]:
    if not company.strip():
        return False, "会社名は必須です。"
    if not email.strip():
        return False, "メールアドレスは必須です。"
    if not EMAIL_RE.match(email.strip()):
        return False, "メールアドレスの形式が正しくありません。"
    return True, ""

# ========= スケール変換 =========
YN3  = ["Yes", "部分的に", "No"]
THREE_USUAL = ["いつも", "ときどき", "ほとんどない"]             # 高頻度がリスク高 → 反転
THREE_BANK  = ["ほとんどない", "たまに", "頻繁に"]               # 頻繁が良い → 通常
THREE_STOCK = ["多くある", "少しある", "ほとんどない"]           # 多いがリスク高 → 反転

def to_score_yn3(ans: str, invert=False) -> int:
    base = {"Yes": 5, "部分的に": 3, "No": 1}
    v = base.get(ans, 3)
    return {5:1,3:3,1:5}[v] if invert else v

def to_score_map(ans: str, mapping: dict, invert=False) -> int:
    v = mapping.get(ans, 3)
    return {5:1,3:3,1:5}[v] if invert else v

MAP_USUAL = {"いつも":1, "ときどき":3, "ほとんどない":5}
MAP_BANK  = {"ほとんどない":1, "たまに":3, "頻繁に":5}
MAP_STOCK = {"多くある":1, "少しある":3, "ほとんどない":5}

# ========= 設問 =========
with st.form("diagnose_form"):
    st.subheader("① 売上・入金管理")
    q1 = st.radio("Q1. 得意先からの入金が「少し遅い」と感じることがありますか？", THREE_USUAL, index=1)
    q2 = st.radio("Q2. 請求書発行から入金までの流れを定期的に点検・改善していますか？", YN3, index=1)

    st.subheader("② 支払・仕入管理")
    q3 = st.radio("Q3. 支払条件（サイト）は自社の資金繰りを考慮して設計できていますか？", YN3, index=1)
    q4 = st.radio("Q4. 外注費や仕入先への支払予定を月次で見通せていますか？", YN3, index=1)

    st.subheader("③ 在庫・固定費管理")
    q5 = st.radio("Q5. 倉庫や事業所に「売れ残り在庫」がありますか？", THREE_STOCK, index=1)
    q6 = st.radio("Q6. 固定費（家賃・人件費など）を季節変動を加味して予実管理できていますか？", YN3, index=1)

    st.subheader("④ 借入・金融機関連携")
    q7 = st.radio("Q7. 銀行とは、どの程度の頻度で連絡を取り合いますか？", THREE_BANK, index=1)
    q8 = st.radio("Q8. 借入金の返済計画や金利条件を把握し、必要に応じて見直していますか？", YN3, index=1)

    st.subheader("⑤ 資金繰り管理体制")
    q9  = st.radio("Q9. 短期の資金繰り表（資金予測）を運用していますか？", YN3, index=2)
    q10 = st.radio("Q10. 資金不足が見込まれる場合の社内手順（対応ルール）は定めていますか？", YN3, index=1)

    st.markdown("---")
    company = st.text_input("会社名（必須）", value=st.session_state["company"])
    email   = st.text_input("メールアドレス（必須）", value=st.session_state["email"])
    st.caption("※ 入力いただいた会社名・メールは診断ログとして保存されます（営業目的以外には利用しません）。")

    submitted = st.form_submit_button("診断する")

# ========= 型テキスト =========
TYPE_TEXT = {
    "売上依存型": "売上・入金管理に弱点。請求〜入金のズレや回収管理の甘さが資金を細らせます。入金管理の定点観測と遅延アラート、与信ルールの整備を優先しましょう。",
    "支払圧迫型": "支払・仕入管理に弱点。期日や条件が自社のキャッシュサイクルに合っていない可能性。仕入先との条件見直しと支払予定の可視化が効果的です。",
    "在庫・固定費過多型": "在庫・固定費管理に弱点。売れ残りや固定費の重さが現金を圧迫。滞留在庫の処分・棚卸頻度の適正化、固定費の弾力化を検討しましょう。",
    "金融連携不足型": "金融機関連携に弱点。日頃の関係構築が薄いと、有事の資金調達が後手に回ります。担当者との定期対話と借入条件の棚卸しが必要です。",
    "体制未整備型": "資金繰りの運用体制に弱点。資金繰り表の未整備や手順不在は“気づいたら足りない”の温床。3ヶ月ローリングの予測運用を開始しましょう。",
    "バランス良好型": "全体バランスは良好。次は資金効率の最大化へ。余剰資金の運用設計、回収・支払条件の最適化でキャッシュ創出力を高めましょう。"
}

# ========= OpenAI: AIコメント =========
def _openai_client(api_key: str):
    try:
        from openai import OpenAI
        return "new", OpenAI(api_key=api_key)
    except Exception:
        import openai
        openai.api_key = api_key
        return "old", openai

def generate_ai_comment(company: str, main_type: str, df_scores: pd.DataFrame, overall_avg: float):
    api_key = read_secret("OPENAI_API_KEY", None)
    if not api_key:
        return None, "OpenAIのAPIキーが未設定です。"

    worst2 = df_scores.sort_values("平均スコア", ascending=True).head(2)["カテゴリ"].tolist()
    signal = "青" if overall_avg>=4.0 else ("黄" if overall_avg>=2.6 else "赤")

    user_prompt = f"""
あなたは資金繰りに強いコンサルタントです。以下の診断結果を受け、経営者向けに約300字（260〜340）で日本語コメントを1段落で作成。
・前置きや免責は不要、箇条書き禁止、具体策重視。
・最後の1文は信号色に応じた強度で「90分スポット診断」への自然な誘導で締める（赤=強く推奨、黄=推奨、青=任意の精緻化）。

[会社名] {company or "（未入力）"}
[全体平均] {overall_avg:.2f} / 5
[信号] {signal}
[タイプ] {main_type}
[弱点カテゴリTOP2] {", ".join(worst2)}
[5カテゴリ] {", ".join(df_scores["カテゴリ"].tolist())}
""".strip()

    mode, client = _openai_client(api_key)

    for attempt in range(2):
        try:
            if mode == "new":
                resp = client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": "専門的かつ簡潔。日本語。実務に直結する助言を。"},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.4,
                    max_tokens=420,
                )
                return resp.choices[0].message.content.strip(), None
            else:
                resp = client.ChatCompletion.create(
                    model=OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": "専門的かつ簡潔。日本語。実務に直結する助言を。"},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.4,
                    max_tokens=420,
                )
                return resp.choices[0].message["content"].strip(), None
        except Exception as e:
            if attempt == 0:
                time.sleep(4)
                continue
            _report_event("ERROR", f"AIコメント生成エラー: {e}", {})
            return None, f"AIコメント生成でエラー: {e}"

def clamp_comment(text: str, max_chars: int = 520) -> str:
    if not text:
        return ""
    t = " ".join(text.strip().split())
    return t if len(t) <= max_chars else (t[:max_chars - 1] + "…")

# ========= 図・QR =========
def build_bar_png(df: pd.DataFrame) -> bytes:
    fig, ax = plt.subplots(figsize=(5.0, 2.4), dpi=220)
    df_sorted = df.sort_values("平均スコア", ascending=True)
    ax.barh(df_sorted["カテゴリ"], df_sorted["平均スコア"])
    ax.set_xlim(0, 5)
    ax.set_xlabel("平均スコア（0-5）")
    ax.grid(axis="x", linestyle="--", alpha=0.3)
    if FONT_PATH_IN_USE:
        from matplotlib import font_manager as fm
        fp = fm.FontProperties(fname=FONT_PATH_IN_USE)
        ax.set_xlabel("平均スコア（0-5）", fontproperties=fp)
        for label in ax.get_yticklabels(): label.set_fontproperties(fp)
        for label in ax.get_xticklabels(): label.set_fontproperties(fp)
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig); buf.seek(0)
    return buf.read()

def image_with_max_width(path: str, max_w: int):
    with PILImage.open(path) as im:
        w, h = im.size
    if w <= max_w:
        return Image(path, width=w, height=h)
    new_h = h * (max_w / w)
    return Image(path, width=max_w, height=new_h)

def build_qr_png(data_url: str) -> bytes:
    img = qrcode.make(data_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

# ========= PDF生成 =========
def make_pdf_bytes(result: dict, df_scores: pd.DataFrame, brand_hex=BRAND_BG) -> bytes:
    logo_path = path_or_download_logo()
    bar_png = build_bar_png(df_scores)
    qr_png  = build_qr_png(CTA_URL)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        rightMargin=32, leftMargin=32, topMargin=28, bottomMargin=28
    )

    styles = getSampleStyleSheet()
    title = styles["Title"]; normal = styles["BodyText"]; h3 = styles["Heading3"]
    if FONT_PATH_IN_USE:
        title.fontName = normal.fontName = h3.fontName = "JP"
    normal.fontSize = 10
    normal.leading = 14
    h3.spaceBefore = 6
    h3.spaceAfter = 4

    elems = []
    if logo_path:
        elems.append(image_with_max_width(logo_path, max_w=120))
        elems.append(Spacer(1, 6))

    elems.append(Paragraph("3分無料診断レポート", title))
    elems.append(Spacer(1, 4))
    meta = (
        f"会社名：{result['company'] or '（未入力）'}　/　"
        f"実施日時：{result['dt']}　/　"
        f"信号：{result['signal']}　/　"
        f"タイプ：{result['main_type']}"
    )
    elems.append(Paragraph(meta, normal))
    elems.append(Spacer(1, 6))

    elems.append(Paragraph("診断コメント", h3))
    elems.append(Paragraph(clamp_comment(result["comment"], 520), normal))
    elems.append(Spacer(1, 6))

    table_data = [["カテゴリ", "平均スコア（0-5）"]] + [
        [r["カテゴリ"], f"{r['平均スコア']:.2f}"] for _, r in df_scores.iterrows()
    ]
    tbl = Table(table_data, colWidths=[220, 140])
    style_list = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(brand_hex)),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.black),
        ("GRID",       (0, 0), (-1, -1), 0.3, colors.grey),
        ("ALIGN",      (1, 1), (-1, -1), "CENTER"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
    ]
    if FONT_PATH_IN_USE:
        style_list.append(("FONTNAME", (0, 0), (-1, -1), "JP"))
    tbl.setStyle(TableStyle(style_list))
    elems.append(tbl)
    elems.append(Spacer(1, 6))

    bar_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    bar_tmp.write(bar_png); bar_tmp.flush()
    elems.append(Paragraph("カテゴリ別スコア（棒グラフ）", h3))
    elems.append(Image(bar_tmp.name, width=390, height=180))
    elems.append(Spacer(1, 6))

    # 次の一手（QR右寄せ）
    elems.append(Paragraph("次の一手（90分スポット診断のご案内）", h3))
    url_par = Paragraph(f"詳細・お申込み：<u>{CTA_URL}</u>", normal)
    qr_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    qr_tmp.write(qr_png); qr_tmp.flush()
    qr_img = Image(qr_tmp.name, width=52, height=52)
    next_table = Table([[url_par, qr_img]], colWidths=[430, 70])
    nt_style = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]
    if FONT_PATH_IN_USE:
        nt_style.append(("FONTNAME", (0, 0), (-1, -1), "JP"))
    next_table.setStyle(TableStyle(nt_style))
    elems.append(next_table)

    doc.build(elems)
    buf.seek(0)
    return buf.read()

# ========= 計算＆表示 =========
if submitted:
    ok, msg = validate_inputs(company, email)
    if not ok:
        st.error(msg)
        st.stop()

    sales_scores  = [to_score_map(q1, MAP_USUAL, invert=False), to_score_yn3(q2)]
    pay_scores    = [to_score_yn3(q3), to_score_yn3(q4)]
    stock_scores  = [to_score_map(q5, MAP_STOCK, invert=False), to_score_yn3(q6)]
    bank_scores   = [to_score_map(q7, MAP_BANK, invert=False), to_score_yn3(q8)]
    sys_scores    = [to_score_yn3(q9), to_score_yn3(q10)]

    df = pd.DataFrame({
        "カテゴリ": ["売上・入金管理","支払・仕入管理","在庫・固定費管理","借入・金融機関連携","資金繰り管理体制"],
        "平均スコア": [
            sum(sales_scores)/2,
            sum(pay_scores)/2,
            sum(stock_scores)/2,
            sum(bank_scores)/2,
            sum(sys_scores)/2
        ]
    })
    overall_avg = df["平均スコア"].mean()

    if overall_avg >= 4.0:
        signal = ("青信号", "badge-blue")
    elif overall_avg >= 2.6:
        signal = ("黄信号", "badge-yellow")
    else:
        signal = ("赤信号", "badge-red")

    if (df["平均スコア"] >= 4.0).all():
        main_type = "バランス良好型"
    else:
        worst_row = df.sort_values("平均スコア").iloc[0]
        cat = worst_row["カテゴリ"]
        main_type = {
            "売上・入金管理": "売上依存型",
            "支払・仕入管理": "支払圧迫型",
            "在庫・固定費管理": "在庫・固定費過多型",
            "借入・金融機関連携": "金融連携不足型",
            "資金繰り管理体制": "体制未整備型"
        }[cat]

    st.session_state.update({
        "df": df, "overall_avg": overall_avg, "signal": signal,
        "main_type": main_type, "company": company, "email": email,
        "result_ready": True, "ai_comment": None, "ai_tried": False
    })

# 結果画面
if st.session_state.get("result_ready"):
    df = st.session_state["df"]
    overall_avg = st.session_state["overall_avg"]
    signal = st.session_state["signal"]
    main_type = st.session_state["main_type"]
    company = st.session_state["company"]
    email = st.session_state["email"]
    current_time = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    # AIコメント自動生成（初回のみ）
    if not st.session_state["ai_tried"]:
        st.session_state["ai_tried"] = True
        text, err = generate_ai_comment(company, main_type, df, overall_avg)
        if text:
            st.session_state["ai_comment"] = text
        elif err:
            st.session_state["ai_comment"] = None
            _report_event("WARN", f"AIコメント未生成: {err}", {})

    st.markdown("### 診断結果")
    st.markdown(
        f"""
        <div class="result-card">
            <h3 style="margin:0 0 .3rem 0;">
              タイプ判定：{main_type} <span class="badge {signal[1]}">{signal[0]}</span>
            </h3>
            <div class="small-note">
              会社名：{company or "（未入力）"} ／ 実施日時：{current_time}
            </div>
            <hr/>
            <p style="margin:.2rem 0 0 0;">{TYPE_TEXT[main_type]}</p>
        </div>
        """,
        unsafe_allow_html=True
    )

    chart = (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X("平均スコア:Q", scale=alt.Scale(domain=[0, 5])),
            y=alt.Y("カテゴリ:N", sort="-x"),
            tooltip=["カテゴリ", "平均スコア"]
        ).properties(height=210)
    )
    st.altair_chart(chart, use_container_width=True)
    st.dataframe(df.style.format({"平均スコア": "{:.2f}"}), use_container_width=True)

    # 画面にもAIコメント自動表示
    st.subheader("AIコメント（自動生成）")
    if st.session_state["ai_comment"]:
        st.write(st.session_state["ai_comment"])
    else:
        st.caption("（OpenAI APIキー未設定等のため、PDFには静的コメントを挿入します）")

    # PDF
    comment_for_pdf = st.session_state["ai_comment"] or TYPE_TEXT[main_type]
    result_payload = {
        "company": company,
        "email": email,
        "dt": current_time,  # JST
        "signal": signal[0],
        "main_type": main_type,
        "comment": comment_for_pdf
    }
    pdf_bytes = make_pdf_bytes(result_payload, df, brand_hex=BRAND_BG)
    fname = f"VC_診断_{company or '匿名'}_{datetime.now(JST).strftime('%Y%m%d_%H%M')}.pdf"
    st.download_button("📄 PDFをダウンロード", data=pdf_bytes, file_name=fname, mime="application/pdf")

    # ======== シート書き込み用データ（ヘッダー順に整形） ========
    category_scores = {
        "売上・入金管理": float(df.loc[df["カテゴリ"]=="売上・入金管理","平均スコア"].values[0]),
        "支払・仕入管理": float(df.loc[df["カテゴリ"]=="支払・仕入管理","平均スコア"].values[0]),
        "在庫・固定費管理": float(df.loc[df["カテゴリ"]=="在庫・固定費管理","平均スコア"].values[0]),
        "借入・金融機関連携": float(df.loc[df["カテゴリ"]=="借入・金融機関連携","平均スコア"].values[0]),
        "資金繰り管理体制": float(df.loc[df["カテゴリ"]=="資金繰り管理体制","平均スコア"].values[0]),
    }
    category_scores_str = json.dumps(category_scores, ensure_ascii=False)

    def to_risk_level(total: float) -> str:
        if total < 2.0:
            return "高リスク"
        elif total < 3.5:
            return "中リスク"
        else:
            return "低リスク"

    pdf_persist_url = ""
    comment_text = st.session_state["ai_comment"] or ""
    comment_len = len(comment_text)
    entry_check = "OK"
    report_date = datetime.now(JST).strftime("%Y-%m-%d")

    row = {
        "timestamp":   datetime.now(JST).isoformat(timespec="seconds"),
        "company":     company,
        "email":       email,
        "category_scores": category_scores_str,
        "total_score": f"{overall_avg:.2f}",
        "type_label":  main_type,
        "ai_comment":  comment_text,
        "utm_source":  st.session_state.get("utm_source",""),
        "utm_campaign":st.session_state.get("utm_campaign",""),
        "pdf_url":     pdf_persist_url,
        "app_version": APP_VERSION,
        "status":      "ok",
        "ai_comment_len": str(comment_len),
        "risk_level":  to_risk_level(overall_avg),
        "entry_check": entry_check,
        "report_date": report_date,
    }
    auto_save_row(row)

else:
    st.caption("フォームに回答し、「診断する」を押してください。")

# ========= 管理者UI（任意） =========
if ADMIN_MODE:
    with st.expander("ADMIN：イベントログの確認（最新50件）"):
        secret_json     = read_secret("GOOGLE_SERVICE_JSON", None)
        secret_sheet_id = read_secret("SPREADSHEET_ID", None)
        shown = False
        try:
            if secret_json and secret_sheet_id:
                scopes = ["https://www.googleapis.com/auth/spreadsheets"]
                info = json.loads(secret_json)
                creds = Credentials.from_service_account_info(info, scopes=scopes)
                gc = gspread.authorize(creds)
                sh = gc.open_by_key(secret_sheet_id)
                ws = sh.worksheet("events")
                values = ws.get_all_records()
                if values:
                    df_evt = pd.DataFrame(values).sort_values("timestamp", ascending=False).head(50)
                    st.dataframe(df_evt, use_container_width=True)
                    shown = True
        except Exception:
            pass
        if not shown:
            if os.path.exists("events.csv"):
                df_evt = pd.read_csv("events.csv").sort_values("timestamp", ascending=False).head(50)
                st.dataframe(df_evt, use_container_width=True)
            else:
                st.info("イベントログはまだありません。")












