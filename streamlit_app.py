# -*- coding: utf-8 -*-
# 3分無料診断｜Victor Consulting
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
APP_VERSION  = "v1.0.0"

# responses シートの1行目（ヘッダー）に合わせる
HEADER_ORDER = [
    "timestamp",        # A
    "company",          # B
    "email",            # C
    "category_scores",  # D  ← 5カテゴリ平均をJSON文字列で
    "total_score",      # E  ← overall_avg
    "type_label",       # F  ← main_type
    "ai_comment",       # G
    "utm_source",       # H
    "utm_campaign",     # I
    "pdf_url",          # J  ← いまは空。将来外部ストレージURLに
    "app_version",      # K
    "status",           # L  ← "ok"/"error"など
    "ai_comment_len",   # M
    "risk_level",       # N  ← 低/中/高
    "entry_check",      # O  ← "OK"
    "report_date"       # P  ← YYYY-MM-DD
]

# 日本時間
JST = timezone(timedelta(hours=9))

# 画面設定
st.set_page_config(
    page_title="3分無料診断｜Victor Consulting",
    page_icon="✅",
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
    """障害・警告を“管理者だけ”が後から確認できるように記録。
       優先: Google Sheets の 'events' シート → 無ければ CSV(events.csv)
       画面には出さない。ADMIN_MODE時のみ小さく表示。
    """
    evt = {
        "timestamp": datetime.now(JST).isoformat(timespec="seconds"),
        "level": level,
        "message": message,
        "payload": json.dumps(payload, ensure_ascii=False) if payload else ""
    }
    # Sheets優先
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
    # CSVフォールバック
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

    # 初回ヘッダーが未設定なら自動作成（安全網）
    values = ws.get_all_values()
    if not values:
        ws.append_row(HEADER_ORDER)

    # ヘッダー順に並び替えて追記
    record = [row_dict.get(k, "") for k in HEADER_ORDER]
    ws.append_row(record, value_input_option="USER_ENTERED")

def fallback_append_to_csv(row_dict: dict, csv_path="responses.csv"):
    df = pd.DataFrame([row_dict])
    if os.path.exists(csv_path):
        df.to_csv(csv_path, mode="a", header=False, index=False, encoding="utf-8")
    else:
        df.to_csv(csv_path, index=False, encoding="utf-8")

def auto_save_row(row: dict):
    """ユーザーには何も表示しない。
    - Sheets設定があれば Sheets に追記
    - 無ければ CSV に追記
    - 失敗時は events に記録（画面表示なし）
    """
    secret_json     = read_secret("GOOGLE_SERVICE_JSON", None)
    # Base64フォールバック（必要な場合）
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
    st.markdown("- 入力は Yes/部分的/No と 5段階のみ\n- 機密数値は不要\n- 結果は 6タイプ＋赤/黄/青")
    st.caption("© Victor Consulting")

st.title("製造現場の“隠れたムダ”をあぶり出す｜3分無料診断")
st.write("**10問**に回答するだけで、貴社のリスク“構造”を可視化します。")

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

# ========= 設問 =========
YN3  = ["Yes", "部分的に", "No"]
FIVE = ["5（非常にある）", "4", "3", "2", "1（まったくない）"]

with st.form("diagnose_form"):
    st.subheader("① 在庫・運搬（資金の滞留）")
    q1 = st.radio("Q1. 完成品・仕掛品の在庫基準を数値で管理していますか？", YN3, index=1)
    q2 = st.radio("Q2. 在庫削減の責任部署（またはKPI）が明確ですか？", YN3, index=1)

    st.subheader("② 人材・技能承継（属人化リスク）")
    q3 = st.radio("Q3. 熟練者しか対応できない作業が3割以上ありますか？（Yesはリスク高）", YN3, index=2)
    q4 = st.radio("Q4. 作業標準書・マニュアルを継続更新できる体制がありますか？", YN3, index=1)

    st.subheader("③ 原価意識・改善文化（損失体質）")
    q5 = st.radio("Q5. 改善提案や原価削減の目標を数値で追っていますか？", YN3, index=1)
    q6 = st.radio("Q6. 現場リーダーがコスト感覚を持って行動していますか？", FIVE, index=2)

    st.subheader("④ 生産計画・変動対応（流れの乱れ）")
    q7 = st.radio("Q7. 受注変動や突発対応の標準ルールがありますか？", YN3, index=1)
    q8 = st.radio("Q8. リードタイム短縮の取組を定期的に見直していますか？", YN3, index=1)

    st.subheader("⑤ DX・情報共有（見える化不足）")
    q9  = st.radio("Q9. 現場の進捗や生産実績をリアルタイムで把握できますか？", YN3, index=2)
    q10 = st.radio("Q10. データをもとに経営会議や現場ミーティングを行っていますか？", YN3, index=1)

    st.markdown("---")
    company = st.text_input("会社名（必須）", value=st.session_state["company"])
    email   = st.text_input("メールアドレス（必須）", value=st.session_state["email"])
    st.caption("※ 入力いただいた会社名・メールは診断ログとして保存されます（営業目的以外には利用しません）。")

    submitted = st.form_submit_button("診断する")

# ========= スコア関数 =========
def to_score_yn3(ans: str, invert=False) -> int:
    base = {"Yes": 5, "部分的に": 3, "No": 1}
    val = base.get(ans, 3)
    return {5: 1, 3: 3, 1: 5}[val] if invert else val

def to_score_5scale(ans: str) -> int:
    return int(ans[0])

# ========= 型テキスト =========
TYPE_TEXT = {
    "在庫滞留型": "過剰在庫やWIP滞留で資金が眠っている可能性が高い状態です。生産量ではなく“流れ”の設計に軸足を移しましょう。",
    "熟練依存型": "属人化により技能がブラックボックス化。ベテラン離職に伴う急落リスクが高い状態です。技能棚卸と多能工化の設計が急務です。",
    "原価ブラックボックス型": "コスト意識・原価の見える化が弱く、利益が目減りする体質です。現場まで“見える原価管理”を展開しましょう。",
    "変動脆弱型": "受注変動・突発に弱く、納期トラブルや残業増に直結しています。変動を“なくす”のではなく“流す”バッファ設計が肝要です。",
    "データ断絶型": "進捗・実績が見えず、意思決定が遅れがちです。まずは“見える化”から。現場と経営のデータ接続を整備しましょう。",
    "バランス良好型": "リスク分散と仕組み成熟が進んでいます。次の一手は“利益を生むデータ活用”と継続的なリードタイム短縮です。"
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
    user_prompt = f"""
あなたは元製造部長の経営コンサルタントです。以下の診断結果を受け、経営者向けに約300字（260〜340字）の具体的コメントを日本語で書いてください。箇条書きは使わず、1段落で、余計な前置きや免責は不要。最後は「90分スポット診断」での次アクションを自然に促す一文で締めます。

[会社名] {company or "（未入力）"}
[全体平均] {overall_avg:.2f} / 5
[信号] {"青" if overall_avg>=4.0 else ("黄" if overall_avg>=2.6 else "赤")}
[タイプ] {main_type}
[弱点カテゴリTOP2] {", ".join(worst2)}
[5カテゴリ] {", ".join(df_scores["カテゴリ"].tolist())}
""".strip()

    mode, client = _openai_client(api_key)
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
            text = resp.choices[0].message.content.strip()
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
            text = resp.choices[0].message["content"].strip()
        return text, None
    except Exception as e:
        _report_event("ERROR", f"AIコメント生成エラー: {e}", {})
        return None, f"AIコメント生成でエラー: {e}"

def clamp_comment(text: str, max_chars: int = 520) -> str:
    if not text:
        return ""
    t = " ".join(text.strip().split())
    return t if len(t) <= max_chars else (t[:max_chars - 1] + "…")

# ========= 図・QRユーティリリティ =========
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

    inv_scores    = [to_score_yn3(q1), to_score_yn3(q2)]
    skills_scores = [to_score_yn3(q3, invert=True), to_score_yn3(q4)]
    cost_scores   = [to_score_yn3(q5), to_score_5scale(q6)]
    plan_scores   = [to_score_yn3(q7), to_score_yn3(q8)]
    dx_scores     = [to_score_yn3(q9), to_score_yn3(q10)]

    df = pd.DataFrame({
        "カテゴリ": ["在庫・運搬","人材・技能承継","原価意識・改善文化","生産計画・変動対応","DX・情報共有"],
        "平均スコア": [
            sum(inv_scores)/2,
            sum(skills_scores)/2,
            sum(cost_scores)/2,
            sum(plan_scores)/2,
            sum(dx_scores)/2
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
            "在庫・運搬": "在庫滞留型",
            "人材・技能承継": "熟練依存型",
            "原価意識・改善文化": "原価ブラックボックス型",
            "生産計画・変動対応": "変動脆弱型",
            "DX・情報共有": "データ断絶型"
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
        "在庫・運搬": float(df.loc[df["カテゴリ"]=="在庫・運搬","平均スコア"].values[0]),
        "人材・技能承継": float(df.loc[df["カテゴリ"]=="人材・技能承継","平均スコア"].values[0]),
        "原価意識・改善文化": float(df.loc[df["カテゴリ"]=="原価意識・改善文化","平均スコア"].values[0]),
        "生産計画・変動対応": float(df.loc[df["カテゴリ"]=="生産計画・変動対応","平均スコア"].values[0]),
        "DX・情報共有": float(df.loc[df["カテゴリ"]=="DX・情報共有","平均スコア"].values[0]),
    }
    category_scores_str = json.dumps(category_scores, ensure_ascii=False)

    def to_risk_level(total: float) -> str:
        if total < 2.0:
            return "高リスク"
        elif total < 3.5:
            return "中リスク"
        else:
            return "低リスク"

    pdf_persist_url = ""  # 将来の外部保存連携用
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










