#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每天自动：
  1) 登录邮箱(IMAP)，找到最新一封 Platts BiofuelScan newsletter，下载其 PDF 附件(BF_YYYYMMDD.pdf)
  2) 抽取 PDF 里的「Market Commentary + News and Insights」正文（双栏排版，自动忽略价格表/走势图/
     Heards 原始数据/Assessments Rationale/期货表）
  3) 用大模型把正文翻译并按板块总结成一份中文晨报
  4) 通过 SMTP 把简报发到指定邮箱

配置全部来自环境变量（GitHub Secrets / 本地 .env / export 均可）。
与 Vegoil-Commentary 项目共用的 Secret（IMAP/SMTP/LLM_*）可直接复用。
"""

import os, re, io, ssl, sys, imaplib, smtplib
from datetime import datetime, timedelta
from email import message_from_bytes
from email.header import decode_header, make_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parsedate_to_datetime, formataddr

import pdfplumber
from openai import OpenAI

# ──────────────────────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────────────────────
def env(key, default=None):
    """读环境变量；空字符串视为未设置（容忍空的 GitHub Secret）。"""
    v = os.environ.get(key)
    return v if (v is not None and v.strip() != "") else default

# 收邮件 (IMAP)
IMAP_HOST = env("IMAP_HOST")                              # 例: imap.exmail.qq.com
IMAP_PORT = int(env("IMAP_PORT", "993"))
IMAP_USER = env("IMAP_USER")                              # 完整邮箱地址
IMAP_PASS = env("IMAP_PASS")                              # 邮箱「授权码/客户端专用密码」
MAILBOX   = env("MAILBOX", "INBOX")

# 发邮件 (SMTP) —— 可与收件邮箱相同
SMTP_HOST = env("SMTP_HOST")                              # 例: smtp.exmail.qq.com
SMTP_PORT = int(env("SMTP_PORT", "465"))
SMTP_USER = env("SMTP_USER", IMAP_USER)
SMTP_PASS = env("SMTP_PASS", IMAP_PASS)
SMTP_SSL  = env("SMTP_SSL", "true").lower() == "true"     # 465用SSL；587用STARTTLS则设false
MAIL_TO   = env("MAIL_TO", IMAP_USER)                     # 简报发到哪（默认发回自己）
MAIL_FROM = env("MAIL_FROM", SMTP_USER)

# 识别 BiofuelScan 那封邮件（主题为主、发件人为辅，双保险）
SUBJECT_CONTAINS = env("SUBJECT_CONTAINS", "biofuelscan").lower()
SENDER_CONTAINS  = env("SENDER_CONTAINS", "platts").lower()
ATTACH_PATTERN   = re.compile(env("ATTACH_PATTERN", r"BF_\d{8}\.pdf"), re.I)  # 锁定 BF_YYYYMMDD.pdf
LOOKBACK_DAYS    = int(env("LOOKBACK_DAYS", "4"))         # 往回找几天的邮件

# 大模型（OpenAI 兼容接口）
# 注意：这里三个配置必须来自同一家。曾经做过“逐个变量回退到 MINIMAX_*/MODEL”的兼容，
# 结果出现「新 key + 旧 endpoint」的错配，向 MiniMax 发 OpenCode 的 key 直接 401
# （2026-09-17 就是这么漏发的）。要换服务就整套一起换，不要只改其中一个。
LLM_API_KEY    = env("LLM_API_KEY")
LLM_BASE_URL   = env("LLM_BASE_URL", "https://opencode.ai/zen/go/v1")
LLM_MODEL      = env("LLM_MODEL", "deepseek-v4.1-flash")
# 输出上限。本报告是「按板块总结」，比 Vegoil 的全文翻译短；若报 400 说明服务端上限更低，调小即可。
LLM_MAX_TOKENS = int(env("LLM_MAX_TOKENS", "16000"))

def log(*a): print("[bf]", *a, flush=True)

# ──────────────────────────────────────────────────────────────────────────
# 一、IMAP：找到最新一封 BiofuelScan 邮件并下载 PDF 附件
# ──────────────────────────────────────────────────────────────────────────
def _decode(s):
    try: return str(make_header(decode_header(s)))
    except Exception: return s or ""

def fetch_latest_pdf():
    log(f"连接 IMAP {IMAP_HOST}:{IMAP_PORT} ...")
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    M.login(IMAP_USER, IMAP_PASS)
    M.select(MAILBOX)
    since = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    typ, data = M.search(None, f'(SINCE "{since}")')
    ids = data[0].split()
    if not ids:
        raise RuntimeError(f"近 {LOOKBACK_DAYS} 天内没有任何邮件。")
    log(f"近 {LOOKBACK_DAYS} 天邮件 {len(ids)} 封，从新到旧筛选 BiofuelScan ...")

    best = None  # (date, pdf_bytes, filename, subject)
    for mid in reversed(ids):                              # 从最新往旧
        typ, msg_data = M.fetch(mid, "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            continue
        msg = message_from_bytes(msg_data[0][1])
        subject = _decode(msg.get("Subject", ""))
        sender  = _decode(msg.get("From", "")).lower()
        if SUBJECT_CONTAINS not in subject.lower():
            continue
        if SENDER_CONTAINS and SENDER_CONTAINS not in sender and SENDER_CONTAINS not in subject.lower():
            # 主题命中即可，发件人只是加分项，不强制
            pass
        # 找 BF_*.pdf 附件
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            fname = _decode(part.get_filename() or "")
            if not fname or not ATTACH_PATTERN.search(fname):
                # 退而求其次：任何 .pdf 附件
                if not (fname.lower().endswith(".pdf")):
                    continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            try:
                d = parsedate_to_datetime(msg.get("Date"))
            except Exception:
                d = datetime.utcnow()
            cand = (d, payload, fname, subject)
            if best is None or cand[0] > best[0]:
                best = cand
        if best is not None:
            break  # reversed 已是最新，命中即可停
    M.logout()
    if best is None:
        raise RuntimeError("没找到含 BiofuelScan PDF 附件的邮件，检查 SUBJECT_CONTAINS / ATTACH_PATTERN。")
    log(f"命中邮件：{best[3]} | 附件：{best[2]} | {len(best[1])} bytes")
    return best[1], best[2]

# ──────────────────────────────────────────────────────────────────────────
# 二、抽取正文（双栏；忽略价格表/走势图/BOTs/Heards/Rationale/期货表）
#     策略：只保留「首个 Market Commentary」到「Assessments Rationale」之间的内容，
#           再按 Platts symbol 代码、表头、单位标签、MOC/BOT 全大写成交行等特征滤掉噪声。
# ──────────────────────────────────────────────────────────────────────────
COL_SPLIT = 300
START_RE  = re.compile(r'\bMarket Commentary\b', re.I)
STOP_RE   = re.compile(r'^\s*Assessments Rationale\s*$', re.I)
DOTS      = re.compile(r'\.{4,}')

SYMBOL_RE = re.compile(r'\b[A-Z]{3,6}\d{2}\b')                       # AAWAA00, UCFCB00, SFSMT00...
TBL_TITLE = re.compile(r'(price assessments|futures|swaps|forward curves|foreign exchange|'
                       r'cost of production|calculated values|carbon\s+credits assessments|'
                       r'tickets price assessments|Bids,?\s*Offers,?\s*Trades|Bids Offers Trades)', re.I)
MOC_RE    = re.compile(r'(MOC (TRADES|BIDS|OFFERS)|(BIDS|OFFERS|TRADES) ON CLOSE|'
                       r'NO (TRADES|BIDS|OFFERS) REPORTED|applies to (the following market|symbol|the symbol)|'
                       r'\bSELLS TO\b|\bBUYS FROM\b|\bBIDS AT\b|\bOFFERS AT\b|FOR \d+K(MT|B)\b|: MW:)', re.I)
FOOTER_RE = re.compile(r'(©\s?20\d\d|unauthorized use|copyrighted material|All rights reserved|'
                       r'intellectual property|legal action|^Platts Biofuelscan\b|^www\.spglobal)', re.I)
SOURCE_RE = re.compile(r'^Source:\s', re.I)
PAGENO_RE = re.compile(r'^\d{1,3}$')
DATEHDR_RE= re.compile(r'^(January|February|March|April|May|June|July|August|September|October|'
                       r'November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},?\s*(20\d\d)?$', re.I)
TBL_HDR   = re.compile(r'^(Symbol\b|Rolling code\b|.*\bLow-High Mid Change\b|.*\bClose Change\b)', re.I)
UNIT_LBL  = re.compile(r'\((\$|¢|Eur|R\$|C\$|Yuan|Rupiah|p|MR|USD)[^)]*\)\s*\*?\^?\s*$')
MONTHTAG  = re.compile(r'\(M\d\)\s*$')
FOOTNOTE  = re.compile(r'^[\*\^].*assess(ed|ments)\b', re.I)
TIMESTAMP = re.compile(r'^\([\d:]+\)$')
REGION_HDR= {"Northwest Europe","North America","South America","Asia","United States",
             "Brazil cargo","Asia Pacific","Americas","Europe","Canada","Houston","Chicago"}

def _upper_ratio(s):
    al = [c for c in s if c.isalpha()]
    return sum(c.isupper() for c in al)/len(al) if al else 0.0

def _is_noise(s):
    if not s: return True
    if FOOTER_RE.search(s) or SOURCE_RE.match(s) or PAGENO_RE.match(s) or DATEHDR_RE.match(s):
        return True
    if TBL_TITLE.search(s) or MOC_RE.search(s) or TBL_HDR.match(s):
        return True
    if SYMBOL_RE.search(s):
        return True
    if FOOTNOTE.match(s) or MONTHTAG.search(s) or TIMESTAMP.match(s):
        return True
    if s in REGION_HDR:
        return True
    toks = s.split()
    if UNIT_LBL.search(s) and len(toks) <= 6:
        return True
    if len(toks) >= 2 and _upper_ratio(s) >= 0.9:           # 全大写 = BOT/MOC 成交行 / 地点表头
        return True
    if len(toks) >= 2:
        numlike = sum(bool(re.fullmatch(r'[-+]?[\d,]+\.?\d*%?', t)) for t in toks)
        if numlike/len(toks) >= 0.6:                        # 数字主导行 = 表格 / 图轴
            return True
    return False

def _page_lines(page):
    """左栏(正文)先、右栏(表格)后，按 top 聚类成行。"""
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    out = []
    for lo, hi in ((0, COL_SPLIT), (COL_SPLIT, 10_000)):
        col = [w for w in words if lo <= w['x0'] < hi]
        col.sort(key=lambda w: (round(w['top']/3), w['x0']))
        line, top = [], None
        for w in col:
            if top is None or abs(w['top']-top) <= 3:
                line.append(w['text']); top = w['top'] if top is None else top
            else:
                out.append(' '.join(line)); line = [w['text']]; top = w['top']
        if line: out.append(' '.join(line))
    return out

def extract_commentary(pdf_bytes):
    kept, cap = [], False
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pi, page in enumerate(pdf.pages):
            if pi == 0:                                     # 封面 + 目录 -> 跳过
                continue
            for ln in _page_lines(page):
                s = ln.strip()
                if DOTS.search(s):                          # 目录点引线
                    continue
                if not cap and START_RE.search(s) and len(s) < 40:
                    cap = True
                if cap and STOP_RE.match(s):
                    return _stitch(kept)
                if cap and not _is_noise(s):
                    kept.append(s)
    return _stitch(kept)

def _stitch(lines):
    """把被 PDF 换行打断的句子接回成段落：行尾无终止标点则与下一行并入同段。"""
    paras, buf = [], ""
    for ln in lines:
        if not buf:
            buf = ln
        elif buf.endswith(("-",)):                          # 连字符断行
            buf = buf[:-1] + ln
        elif buf[-1:] in ".!?。”\"" or ln[:1] in ("■", "•") or len(ln) < 3:
            paras.append(buf); buf = ln
        else:
            buf += " " + ln
    if buf: paras.append(buf)
    return "\n".join(paras)

# ──────────────────────────────────────────────────────────────────────────
# 三、大模型：翻译 + 按板块总结成中文 HTML
# ──────────────────────────────────────────────────────────────────────────
# 术语对照表（强制固定译法；保留项一律纯缩写、不带中文全称）
GLOSSARY = """【固定术语对照（必须遵守；保留项直接用英文缩写，不要附中文全称）】
产品/油种：
- gasoil / ICE gasoil → 「柴油(gasoil)」/「ICE gasoil」，严禁译成「气油」
- ULSD→超低硫柴油(ULSD)；LSGO→低硫柴油(LSGO)
- RD-A / RD-B 保留（分别为 Annex IX-A / IX-B 原料路线可再生柴油）
- HVO / SAF / SPK / HEFA / FAME / RME / PME / SME / UCOME 保留缩写
- B24/B30/B40/B50/B99/B100 保留
- feedstock→原料；UCO 保留；POME / PFAD / DCO 保留；tallow→牛油(tallow)
- crude palm oil / CPO→棕榈原油(CPO)
价格/结构：
- outright / flat price → 统一译「绝对价」
- premium→升水；discount→贴水；spread→价差
- BO-HO→豆油-取暖油价差(BO-HO)；BO-GO→豆油-柴油价差；PO-GO→棕榈油-柴油价差
- margin→利润/利润率；crush margin→压榨利润
- backwardation→逆价差（近高远低）；contango→正价差（近低远高）
- basis→基差；netback→回岸价(netback)
交易/评估机制：
- MOC (Market on Close)→收盘评估(MOC)
- heard→据闻
- bid/offer/trade→买价/卖价/成交
- assessed→评估为；indicative→指示性
- laycan→装期(laycan)；loading→装运
- cargo→整船货(cargo)；barge→驳船(barge)；tradable→可成交
- FOB/CIF/CFR/DAP、FOB ARA/FOB Straits/FOB FARAG 保留
政策/碳信用：
- RED II / RED III、RINs(D3/D4/D5/D6) 保留
- RVO→可再生燃料掺混义务(RVO)
- LCFS→低碳燃料标准(LCFS)；CFP→清洁燃料计划(CFP)；CFS→清洁燃料标准(CFS)
- 45Z / CFPC→45Z 清洁燃料生产抵免(CFPC)；CI→碳强度(CI)
- GHG savings→温室气体减排率(GHG savings)
- CBIO / RTFC / ERE / THG 保留；ticket→合规凭证(ticket)
- mandate→强制掺混令/掺混义务；crop cap→作物原料上限(crop cap)
机构/数据：
- EIA→美国能源信息署(EIA)；USDA→美国农业部(USDA)；WASDE 保留
- CBOT / BMD / ICE / NYMEX 保留；BMD CPO→马来交易所棕榈油期货(BMD CPO)
未列出的英文术语按行业惯例翻译；拿不准的保留英文。"""

PROMPT_TEMPLATE = """你是一名专业的生物燃料/油脂市场翻译兼分析师。下面是 Platts BiofuelScan（标普全球）每日报告中抽取出来的正文部分（双栏 PDF 抽取，已剔除价格数据表、走势图和原始 bid/offer/trade 记录，可能残留少量排版瑕疵，请结合上下文阅读）。

报告正文按品类组织，通常包含：乙醇(Ethanol)、生物柴油/生物船燃/碳信用(Biodiesel, Biobunkers and Credits)、可再生柴油与可持续航空燃料(Renewable Diesel & SAF)、原料(Feedstocks)，以及行业新闻(News and Insights)。

请把这些正文**翻译并总结成一份中文晨报**，按品类详略分明：
1. **详写**生物柴油/生物船燃/碳信用、可再生柴油RD、可持续航空燃料SAF 这三大块——
   每块多给几条要点（4-7 条），把价差、利润率(margin/BO-HO)、原料联动、政策(RED III、45Z、LCFS等)、
   供需与成交逻辑都讲清楚，保留所有具体数字。
2. **乙醇(Ethanol)一笔带过**——只用 1-2 条概括当日方向和最关键的一两个价格，不展开。
3. **原料(Feedstocks)详写**——4-6 条，UCO/POME/PFAD/tallow/DCO 等逐个讲清当日方向、
   关键价格与涨跌、地区价差(如 FOB China vs FOB Straits、Malaysia vs Indonesia)、
   与下游 BD/RD/SAF 的联动逻辑，保留所有具体数字。
4. 行业新闻(News and Insights)单列，按相关性分详略：
   **凡涉及 BD/RD/SAF/原料的新闻多写 1-2 句**，把对供需、价格或政策的影响讲透；
   其余新闻一句话带过。
5. 术语用中文，必要处保留英文缩写：UCOME、UCO、FAME、RME、PME、SME、HVO、RD、SAF、POME、PFAD、DCO、RINs(D4/D5/D6)、RVO、LCFS、CFP、CFR、ETBE、CBIO、RTFC、ERE、THG、FOB ARA、FOB Straits、CBOT、BMD CPO、ICE gasoil、ULSD、BO-HO、CI、RED III 等。
6. **核心总览**侧重 BD/RD/SAF；若当日有美国原料(US feedstock)消息——
   如 tallow、UCO、DCO 的价格/供需变化，进口关税(tariff)、45Z、RFS/RVO/RIN 政策，
   或 US RD/SAF 原料端动态——优先在核心里点出，没有则不必提。

输出要求：**只输出最终 HTML 片段，不要任何思考过程、说明或前言**。不要 markdown、不要 ```、不要 <html>/<body> 外壳。第一行必须就是标题，严格按下面结构（注意乙醇放最后且最简）：
<h2>{date} Platts BiofuelScan 生柴/油脂简报</h2>
<p><strong>核心：</strong>……三四句当日总览，侧重 BD/RD/SAF；有美国原料相关消息时优先点出……</p>
<h3>生物柴油 / 生物船燃 / 碳信用</h3><ul><li>……</li>…</ul>
<h3>可再生柴油 RD / 可持续航空燃料 SAF</h3><ul><li>……</li>…</ul>
<h3>原料 Feedstocks</h3><ul><li>……</li>…</ul>
<h3>行业新闻 News &amp; Insights</h3><ul><li><strong>（标题）：</strong>相关性高的写 1-2 句影响；其余一句话</li>…</ul>
<h3>乙醇 Ethanol（概览）</h3><ul><li>……</li></ul>
报告正文如下：
----------
{body}
----------"""

THINK_TAG = re.compile(r"<think>.*?</think>", re.S | re.I)

def _clean_model_html(text):
    """剥离思考过程：去 <think> 标签、从首个 <h2> 起截取、去掉 markdown 围栏。"""
    if not text: return ""
    t = THINK_TAG.sub("", text)
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t.strip()).strip()
    m = re.search(r"<h2\b", t, re.I)            # 从第一个 <h2> 开始才是正式简报
    if m:
        t = t[m.start():]
    return t.strip()

def summarize_to_chinese(body, date_str):
    if not LLM_API_KEY:
        raise RuntimeError("缺少 LLM_API_KEY，请检查 GitHub Secrets。")
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    prompt = PROMPT_TEMPLATE.format(date=date_str, body=body)
    log(f"调用模型 {LLM_MODEL} @ {LLM_BASE_URL} 生成中文简报，正文 {len(body)} 字符 ...")
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        max_tokens=LLM_MAX_TOKENS,
        temperature=0.3,
        messages=[
            {"role": "system", "content": "你是一名严谨的大宗商品市场分析助理，输出简洁、数字准确、只给最终结果。"},
            {"role": "user", "content": prompt},
        ],
    )
    finish = resp.choices[0].finish_reason
    html = _clean_model_html(resp.choices[0].message.content or "")
    if not html:
        raise RuntimeError(f"模型 {LLM_MODEL} 返回空内容，检查模型名/额度/base_url。")
    if finish == "length":
        log(f"警告：简报可能被长度上限截断（finish_reason=length，LLM_MAX_TOKENS={LLM_MAX_TOKENS}），"
            "若结尾不完整请把它调大。")
    return html

# ──────────────────────────────────────────────────────────────────────────
# 四、SMTP 发送
# ──────────────────────────────────────────────────────────────────────────
def send_email(html_body, subject):
    full = (f'<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
            f'line-height:1.6;color:#222;max-width:760px;">{html_body}'
            f'<hr><p style="color:#999;font-size:12px;">由 Platts BiofuelScan 每日 PDF 自动'
            f'翻译总结生成，仅供个人参考。原始内容版权归 S&P Global / Platts 所有。</p></div>')
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("BiofuelScan 简报", MAIL_FROM))
    msg["To"]   = MAIL_TO
    msg.attach(MIMEText(full, "html", "utf-8"))
    log(f"发送至 {MAIL_TO} via {SMTP_HOST}:{SMTP_PORT} (SSL={SMTP_SSL}) ...")
    if SMTP_SSL:
        s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context())
    else:
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT); s.starttls(context=ssl.create_default_context())
    s.login(SMTP_USER, SMTP_PASS)
    s.sendmail(MAIL_FROM, [a.strip() for a in MAIL_TO.split(",")], msg.as_string())
    s.quit()
    log("已发送。")

# ──────────────────────────────────────────────────────────────────────────
def main():
    pdf_bytes, fname = fetch_latest_pdf()
    body = extract_commentary(pdf_bytes)
    log(f"正文抽取完成：{len(body)} 字符。")
    if len(body) < 800:
        raise RuntimeError("抽取正文过短，可能 PDF 版式有变或匹配到错误附件。")
    # 从附件名 BF_YYYYMMDD.pdf 取日期；取不到则用今天
    m = re.search(r"(\d{4})(\d{2})(\d{2})", fname or "")
    date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else datetime.now().strftime("%Y-%m-%d")
    html = summarize_to_chinese(body, date_str)
    subject = f"【生柴简报】{date_str} Platts BiofuelScan"
    send_email(html, subject)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("失败：", repr(e))
        sys.exit(1)
