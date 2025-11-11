# streamlit_app.py — 3分で分かる 資金繰り改善診断（独立アプリ）
# 必要ライブラリ：streamlit, pandas, reportlab, pillow, qrcode, openai, gspread, google-auth
# requirements.txt 例：
# streamlit
# pandas
# reportlab
# pillow
# qrcode
# openai
# gspread
# google-auth

import os
import io
import json
from datetime import datetime, timezone, timedelta
import textwrap
import base64

import streamlit as st
import pandas as pd

from PIL import Image
import qrcode

# ---- OpenAI ----
try:
    # 新SDK
    from openai import OpenAI
    OPENAI_SDK = "new"
except Exception:
    # 旧SDKフォールバック
    import openai
    OPENAI_SDK = "old"

# ---- Google Sheets ----
import gspread
from google.oauth2.service_account import Credentials

# ---- PDF (ReportLab) ----
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.units import mm
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader

# =========================================================
# 基本設定
# =========================================================
APP_NAME = "3分で分かる 資金繰り改善診断（β）"
APP_VERSION = "cashflow-1.0.0"
BRAND_BG = "#f0f7f7"         # 画面背景アクセント
PRIMARY_LINK = "https://victorconsulting.jp/spot-diagnosis/"  # 90分スポット診断リンク
LOGO_PATH = "assets/logo.png"  # リポ内のロゴ（任意/差し替え可）
JST = timezone(timedelta(hours=9))  # 日本時間
ADMIN_MODE = st.experimental_get_query_params().get("admin", ["0"])[0] == "1"

# Secrets（OpenAI / Google / Sheets）
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY"))
SERVICE_JSON = st.secrets.get("GOOGLE_SERVICE_JSON", None)
SPREADSHEET_ID = st.secrets.get("SPREADSHEET_ID", None)

# =========================================================
# スタイル
# =========================================================
st.set_page_config(page_title=APP_NAME, page_icon="💰", layout="centered")
st.markdown(f"""
<style>
/* 画面の淡色BGボックス */
.section {{
  background: {BRAND_BG};
  padding: 1.4rem 1.2rem 1.2rem 1.2rem;
  border-radius: 12px;
  margin-top: 1.2rem;
}}
h1, h2, h3 {{ line-height: 1.3; }}
.small-note {{
  font-size: 0.9rem; color: #555;
}}
.hr {{
  margin: 0.8rem 0; border-top: 1px solid #ddd;
}}
label[data-baseweb="radio"] > div {{
  padding: 4px 8px;
}}
</style>
""", unsafe_allow_html=True)

# =========================================================
# フォント（PDF用・NotoSansJP があれば採用）
# =========================================================
PDF_FONT_NAME = "NotoSansJP"
def setup_pdf_font():
    try:
        # リポ直下/ローカル同梱（あれば採用）
        if os.path.exists("NotoSansJP-Regular.ttf"):
            pdfmetrics.registerFont(TTFont(PDF_FONT_NAME, "NotoSansJP-Regular.ttf"))
            return PDF_FONT_NAME
    except Exception:
        pass
    # 既定のHelvetica（日本語は豆腐の可能性があるが回避不能時のフォールバック）
    return "Helvetica"

PDF_FONT = setup_pdf_font()

# =========================================================
# ロゴの読み込み（任意）
# =========================================================
def load_logo():
    if os.path.exists(LOGO_PATH):
        try:
            return Image.open(LOGO_PATH)
        except Exception:
            return None
    return None

LOGO_IMG = load_logo()

# =========================================================
# Google Sheets クライアント
# =========================================================
def get_gspread_client():
    if not SERVICE_JSON or not SPREADSHEET_ID:
        return None, None, None
    try:
        info = json.loads(SERVICE_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SPREADSHEET_ID)
        return gc, sh, creds
    except Exception as e:
        st.session_state.setdefault("events", [])
        st.session_state["events"].append({
            "timestamp": datetime.now(JST).isoformat(),
            "level": "WARN",
            "message": f"Sheets接続に失敗: {e}"
        })
        return None, None, None

def append_row(sheet, ws_name, values, header=None):
    """ヘッダー存在チェック→なければ作成→行追加"""
    try:
        try:
            ws = sheet.worksheet(ws_name)
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(title=ws_name, rows=1000, cols=50)
            if header:
                ws.append_row(header)
        if header:
            existing = ws.row_values(1)
            if not existing:
                ws.append_row(header)
        ws.append_row(values)
        return True, None
    except Exception as e:
        return False, str(e)

# =========================================================
# 診断質問（資金繰り版・最終確定）
# =========================================================
# scores は ["選択肢"] と同じ順序で 1/3/5 等を割当
QUESTIONS = [
    {"category":"売上・入金管理",
     "text":"得意先からの入金が、『少し遅い』と感じることがありますか？",
     "options":["いつも","ときどき","ほとんどない"],
     "scores":[1,3,5]},
    {"category":"売上・入金管理",
     "text":"『入金されていない得意先』が頭に浮かぶことがありますか？",
     "options":["よくある","たまにある","ほとんどない"],
     "scores":[1,3,5]},
    {"category":"支払・仕入管理",
     "text":"月末や月初に『資金が詰まる』と感じることがありますか？",
     "options":["よくある","たまにある","ほとんどない"],
     "scores":[1,3,5]},
    {"category":"支払・仕入管理",
     "text":"仕入先や外注先との支払条件をこの1年で見直しましたか？",
     "options":["はい","いいえ"],
     "scores":[5,1]},
    {"category":"在庫・固定費管理",
     "text":"倉庫や事業所に『売れ残り在庫』がありますか？",
     "options":["多くある","少しある","ほとんどない"],
     "scores":[1,3,5]},
    {"category":"在庫・固定費管理",
     "text":"売上が下がっても、経費はあまり減らないと感じますか？",
     "options":["強く感じる","やや感じる","ほとんど感じない"],
     "scores":[1,3,5]},
    {"category":"借入・金融機関連携",
     "text":"銀行とは、どの程度の頻度で連絡を取り合いますか？",
     "options":["ほとんどない","たまに","頻繁に"],
     "scores":[1,3,5]},
    {"category":"借入・金融機関連携",
     "text":"『返済が負担になるかもしれない』と感じたことがありますか？",
     "options":["ある","ない"],
     "scores":[1,5]},
    {"category":"資金繰り管理体制",
     "text":"毎月の入出金をまとめた『資金繰り表』はありますか？",
     "options":["ある","ない"],
     "scores":[5,1]},
    {"category":"資金繰り管理体制",
     "text":"経営会議などで『資金繰り』の話題が出ることはありますか？",
     "options":["ほとんどない","たまにある","よくある"],
     "scores":[1,3,5]},
]

CATEGORIES = ["売上・入金管理","支払・仕入管理","在庫・固定費管理","借入・金融機関連携","資金繰り管理体制"]

# =========================================================
# スコア集計・タイプ分類・信号色
# =========================================================
def compute_scores(responses):
    # responses: list of (category, selected_option_index, score_value)
    by_cat = {c: [] for c in CATEGORIES}
    for cat, _, score in responses:
        by_cat[cat].append(score)
    cat_avg = {c: sum(v)/len(v) if v else 0 for c,v in by_cat.items()}
    total = sum(cat_avg.values())/len(cat_avg)
    # 信号色
    if total < 2.5:
        color = "赤"
    elif total < 3.8:
        color = "黄"
    else:
        color = "青"
    # タイプ分類（簡易ルール：もっとも低いカテゴリで代表）
    weakest = min(cat_avg, key=lambda k: cat_avg[k])
    if weakest == "売上・入金管理":
        tlabel = "売上依存型"
    elif weakest == "支払・仕入管理":
        tlabel = "固定費硬直型"  # 支払条件硬直を固定費硬直に包含
    elif weakest == "在庫・固定費管理":
        tlabel = "在庫滞留型"
    elif weakest == "借入・金融機関連携":
        tlabel = "金融機関連携不足型"
    elif weakest == "資金繰り管理体制":
        tlabel = "管理体制未整備型"
    else:
        tlabel = "バランス型"
    return cat_avg, total, color, tlabel

def build_category_summary(cat_avg):
    ordered = [(k, cat_avg[k]) for k in CATEGORIES]
    return ", ".join([f"{k}:{v:.2f}" for k,v in ordered])

# =========================================================
# AIコメント生成（赤/黄/青で誘導強度を変更）
# =========================================================
def generate_ai_comment(type_label, signal_color, cat_avg, total_score):
    # OpenAI利用可否
    if not OPENAI_API_KEY:
        return None, "OpenAI APIキー未設定"

    category_summary = build_category_summary(cat_avg)

    # 信号別の誘導文
    if signal_color == "赤":
        spot_advice = (
            "現状は早急な対策が必要な水準です。\n"
            "今こそ、専門家の視点を取り入れ、資金繰りを安定化させるタイミングです。\n"
            "90分スポット診断で、即実行できる改善策を一緒に設計しましょう。"
        )
    elif signal_color == "黄":
        spot_advice = (
            "現状は大きな問題には至っていませんが、早めの手当てが将来の安心につながります。\n"
            "90分スポット診断で、いま打てる“予防の一手”を確認しておきましょう。"
        )
    else:
        spot_advice = (
            "現状は健全ですが、より強い財務体質を築くチャンスです。\n"
            "90分スポット診断で、資金繰りを“攻めの経営力”へ高める視点を得てみませんか？"
        )

    prompt = f"""
あなたは中小企業診断士として経営者に助言を行う専門家です。
次の診断結果に基づき、約300字で経営者向けコメントを生成してください。
診断タイプ: {type_label}
信号色: {signal_color}
カテゴリ別平均: {category_summary}
総合スコア: {total_score:.2f}
コメントでは「原因」「リスク」「次の一手」を明確に述べ、
専門用語は避け、平易で簡潔な日本語で書いてください。
最後に以下の文を自然に続けて追加してください：
{spot_advice}
"""

    try:
        if OPENAI_SDK == "new":
            os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
            client = OpenAI()
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role":"system","content":"あなたは中小企業の資金繰りに詳しい経営コンサルタントです。"},
                    {"role":"user","content":prompt}
                ],
                temperature=0.5,
                max_tokens=500
            )
            text = resp.choices[0].message.content.strip()
        else:
            openai.api_key = OPENAI_API_KEY
            resp = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[
                    {"role":"system","content":"あなたは中小企業の資金繰りに詳しい経営コンサルタントです。"},
                    {"role":"user","content":prompt}
                ],
                temperature=0.5,
                max_tokens=500
            )
            text = resp["choices"][0]["message"]["content"].strip()
        return text, None
    except Exception as e:
        return None, f"OpenAI呼び出しエラー: {e}"

# =========================================================
# PDF 生成
# =========================================================
def draw_wrapped_text(c, text, x, y, max_width, line_height, font_name, font_size):
    """指定幅で折り返し描画"""
    wrapper = textwrap.TextWrapper(width=100)  # 後でバイト長ではなく座標幅で調整
    # 文字幅での厳密折り返しは難しいため、日本語は短めに分割
    lines = []
    buf = ""
    for ch in text:
        buf += ch
        # 幅を測って超えたら改行
        if c.stringWidth(buf, font_name, font_size) > max_width:
            lines.append(buf[:-1])
            buf = ch
    if buf:
        lines.append(buf)
    for line in lines:
        c.drawString(x, y, line)
        y -= line_height
    return y

def build_pdf(company, email, jst_now_str, cat_avg, total_score, signal_color, type_label, ai_comment_text):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    margin = 20 * mm

    # フォント
    c.setFont(PDF_FONT, 12)

    # ロゴ（上部左）とタイトル
    y = H - margin
    if LOGO_IMG:
        # ロゴのアスペクト維持で幅調整
        max_w = 35 * mm
        ratio = LOGO_IMG.height / LOGO_IMG.width
        lw, lh = max_w, max_w * ratio
        c.drawImage(ImageReader(LOGO_IMG), margin, y - lh, width=lw, height=lh, mask='auto')
        title_x = margin + lw + 10
    else:
        title_x = margin

    c.setFont(PDF_FONT, 16)
    c.drawString(title_x, y - 12, "3分で分かる 資金繰り改善診断")
    c.setFont(PDF_FONT, 10)
    c.drawString(title_x, y - 28, f"会社名：{company or '（未入力）'} ／ 実施日時：{jst_now_str}")
    y -= 40

    # 罫線
    c.setStrokeColorRGB(0.75,0.75,0.75)
    c.line(margin, y, W - margin, y)
    y -= 16

    # 概要（スコア・信号・タイプ）
    c.setFont(PDF_FONT, 12)
    c.drawString(margin, y, f"総合スコア：{total_score:.2f}（信号：{signal_color}）／ タイプ：{type_label}")
    y -= 14

    # カテゴリ別
    for cat in CATEGORIES:
        c.setFont(PDF_FONT, 11)
        c.drawString(margin, y, f"{cat}：{cat_avg[cat]:.2f}")
        y -= 12

    y -= 6
    c.line(margin, y, W - margin, y)
    y -= 16

    # AIコメント（長文は折り返し）
    c.setFont(PDF_FONT, 12)
    c.drawString(margin, y, "AIコメント（要点と次の一手）")
    y -= 14
    c.setFont(PDF_FONT, 10)
    y = draw_wrapped_text(c, ai_comment_text, margin, y, max_width=W - 2*margin, line_height=12, font_name=PDF_FONT, font_size=10)
    y -= 10

    # 次の一手＋QR（右側に寄せる）
    c.setFont(PDF_FONT, 11)
    c.drawString(margin, y, "次の一手：90分スポット診断のご案内")
    y -= 14
    c.setFont(PDF_FONT, 10)
    c.drawString(margin, y, PRIMARY_LINK)
    # QRを右側に
    try:
        qr_img = qrcode.make(PRIMARY_LINK)
        qr_w = 28 * mm
        qr_h = qr_w
        c.drawImage(ImageReader(qr_img), W - margin - qr_w, y - (qr_h - 8), width=qr_w, height=qr_h)
    except Exception:
        pass

    c.showPage()
    c.save()
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes

# =========================================================
# UI
# =========================================================
st.title(APP_NAME)
st.markdown(f'<div class="section">', unsafe_allow_html=True)
st.markdown("**現金繰りの“いま”を見える化し、最適な次の一手を提示します。** 3分で完了。")
st.markdown('</div>', unsafe_allow_html=True)

with st.form("diag_form", clear_on_submit=False):
    st.subheader("基本情報")
    company = st.text_input("会社名（必須）", value="")
    email = st.text_input("メールアドレス（必須）", value="")

    st.markdown('<div class="hr"></div>', unsafe_allow_html=True)
    st.subheader("設問（10問）")

    responses = []
    selections = []
    for i, q in enumerate(QUESTIONS, start=1):
        st.write(f"**Q{i}. {q['text']}**")
        choice = st.radio(
            label="",
            options=q["options"],
            index=None,
            horizontal=False,
            key=f"q{i}"
        )
        selections.append(choice)

    submitted = st.form_submit_button("診断する")

# =========================================================
# バリデーション／診断ロジック
# =========================================================
if submitted:
    # 未入力チェック
    if not company.strip() or not email.strip():
        st.error("会社名とメールアドレスは必須です。ご入力のうえ、再度お試しください。")
        st.stop()
    # 設問未回答チェック
    if any(sel is None for sel in selections):
        st.error("未回答の設問があります。全ての設問にご回答ください。")
        st.stop()

    # 回答→スコア化
    for q, sel in zip(QUESTIONS, selections):
        idx = q["options"].index(sel)
        score = q["scores"][idx]
        responses.append((q["category"], idx, score))

    # 集計
    cat_avg, total_score, signal_color, type_label = compute_scores(responses)

    # 画面表示（結果）
    st.success(f"診断結果：信号 **{signal_color}** ／ タイプ **{type_label}** ／ 総合スコア **{total_score:.2f}**")

    # AIコメント生成（自動）
    with st.spinner("AIコメントを生成しています…"):
        ai_text, ai_err = generate_ai_comment(type_label, signal_color, cat_avg, total_score)
        if ai_err or not ai_text:
            # フォールバック（静的）
            ai_text = (
                f"{type_label}の傾向が見られます。資金繰りの不安定化を避けるため、"
                f"カテゴリ別に弱点へ優先順位をつけ、短期・中期の対策を進めましょう。"
                f"より具体的な改善策は現場の数値と状況次第で変わります。"
                f"\n\n現状整理と方針策定のために、90分スポット診断のご活用をおすすめします。"
            )
            st.info("（OpenAI API未設定/混雑等のためフォールバックコメントを表示しています）")

    # 結果サマリ表示
    with st.expander("カテゴリ別スコアの詳細"):
        df = pd.DataFrame({"カテゴリ": list(cat_avg.keys()), "平均スコア": [f"{v:.2f}" for v in cat_avg.values()]})
        st.dataframe(df, use_container_width=True)
        st.write(f"総合スコア：**{total_score:.2f}** ／ 信号：**{signal_color}** ／ タイプ：**{type_label}**")

    # PDF生成
    jst_now = datetime.now(JST)
    jst_str = jst_now.strftime("%Y-%m-%d %H:%M")
    pdf_bytes = build_pdf(company, email, jst_str, cat_avg, total_score, signal_color, type_label, ai_text)
    pdf_filename = f"資金繰り診断_{company}_{jst_now.strftime('%Y%m%d_%H%M')}.pdf"

    st.download_button(
        label="📄 PDFをダウンロード",
        data=pdf_bytes,
        file_name=pdf_filename,
        mime="application/pdf",
    )

    # Google Sheets 保存（responses）
    utm = st.experimental_get_query_params()
    utm_source = utm.get("utm_source", [""])[0]
    utm_campaign = utm.get("utm_campaign", [""])[0]

    row_header = [
        "timestamp","company","email","category_scores","total_score",
        "type_label","ai_comment","utm_source","utm_campaign","pdf_url",
        "app_version","status","ai_comment_len","risk_level","entry_check","report_date"
    ]
    risk_level = {"赤":"高リスク","黄":"中リスク","青":"低リスク"}[signal_color]
    cat_txt = build_category_summary(cat_avg)
    row_values = [
        jst_now.isoformat(), company, email, cat_txt, round(total_score,2),
        type_label, ai_text, utm_source, utm_campaign, "",  # pdf_urlは未運用（空）
        APP_VERSION, "OK", len(ai_text), risk_level, "OK", jst_now.strftime("%Y-%m-%d")
    ]

    gc, sh, _ = get_gspread_client()
    if sh:
        ok, reason = append_row(sh, "responses", row_values, header=row_header)
        if not ok:
            # eventsにWARN
            append_row(sh, "events",
                       [datetime.now(JST).isoformat(), "WARN",
                        f"Sheets保存に失敗し（responses）、理由: {reason}",
                        json.dumps({"reason":reason}, ensure_ascii=False)], 
                       header=["timestamp","level","message","meta"])
    else:
        # 接続失敗時は events.csv に追記フォールバック
        with open("events.csv","a",encoding="utf-8") as f:
            f.write(f"{datetime.now(JST).isoformat()},WARN,Sheets接続なし,{{}}\n")

    # 次の一手ボックス
    st.markdown(f"""
<div class="section">
  <b>次の一手：</b> <a href="{PRIMARY_LINK}" target="_blank">90分スポット診断のご案内（Victor Consulting）</a><br>
  診断結果をもとに、今すぐ実行できる改善策を“あなたの会社向け”に具体化します。
</div>
""", unsafe_allow_html=True)

# =========================================================
# ADMIN: イベントログの確認（?admin=1 で表示）
# =========================================================
if ADMIN_MODE:
    st.subheader("ADMIN：イベントログの確認（最新50件）")
    shown = False
    gc, sh, _ = get_gspread_client()
    if sh:
        try:
            ws = sh.worksheet("events")
            values = ws.get_all_records()
            if values:
                df_evt = pd.DataFrame(values).sort_values("timestamp", ascending=False).head(50)
                st.dataframe(df_evt, use_container_width=True)
                shown = True
        except Exception:
            pass
    if not shown:
        import os
        if os.path.exists("events.csv"):
            df_evt = pd.read_csv("events.csv", header=None, names=["timestamp","level","message","meta"])
            df_evt = df_evt.sort_values("timestamp", ascending=False).head(50)
            st.dataframe(df_evt, use_container_width=True)
        else:
            st.info("イベントログはまだありません。")











