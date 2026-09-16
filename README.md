# Biofuel-Scan
BJ TA
# Platts BiofuelScan 每日中文简报（自动）

每天早上自动：读邮箱拿到 Platts BiofuelScan 邮件 → 下载其 PDF 附件（`BF_YYYYMMDD.pdf`）→ 抽取 **Market Commentary + News and Insights** 正文（自动忽略价格表、走势图、Heards 原始数据、Assessments Rationale、期货表）→ 用大模型翻译并按品类总结成中文晨报 → 发到你邮箱。跑在 GitHub Actions 上，免费。

> 与 `Vegoil-Commentary` 项目结构一致，**共用的 Secret（`IMAP_*` / `SMTP_*` / `LLM_API_KEY`）可直接复用**。比 Vegoil 省掉了整套 Playwright 登录 / cookie / 反爬逻辑——因为 BiofuelScan 是邮件直接附 PDF。

---

## 文件清单

| 文件 | 作用 |
|---|---|
| `biofuelscan_brief.py` | 主程序：拉附件 → 抽正文 → 翻译总结 → 发邮件 |
| `requirements.txt` | 依赖（仅 `pdfplumber` + `openai`，**无需 playwright**） |
| `.github/workflows/daily-biofuelscan-brief.yml` | GitHub Actions 工作流 |

---

## 一、准备工作

### 1. 企业邮箱 IMAP/SMTP 授权码
程序要读邮箱找 BiofuelScan 那封信并取附件，再用 SMTP 发简报。在邮箱网页端开启「IMAP/SMTP 服务」，生成**授权码 / 客户端专用密码**（不是登录密码）。

| 企业邮箱 | IMAP (993/SSL) | SMTP (465/SSL) |
|---|---|---|
| 腾讯企业邮 exmail | imap.exmail.qq.com | smtp.exmail.qq.com |
| 阿里企业邮 | imap.qiye.aliyun.com | smtp.qiye.aliyun.com |
| 网易企业邮 | imaphz.qiye.163.com | smtphz.qiye.163.com |

> 沿用 Vegoil 的经验：若企业邮箱给「自己发自己」的外部 IP 邮件做静默丢弃，把 `MAIL_TO` 设成一个 Gmail 之类的外部地址即可。

### 2. 大模型 API Key
与 Vegoil 用同一个 key 即可。当前默认走 **OpenCode Zen**：

- `LLM_BASE_URL` 默认 `https://opencode.ai/zen/go/v1`
- `LLM_MODEL` 默认 `deepseek-v4.1-flash`
- 在 opencode.ai 登录后加账单、复制 API Key，填到 Secret `LLM_API_KEY`（另外两个有默认值，通常不用填）

换别家（OpenAI 兼容都行）：覆盖 `LLM_BASE_URL` / `LLM_MODEL`。各家 `max_tokens` 上限不同，报 400 就调小 `LLM_MAX_TOKENS`。

---

## 二、建仓库 / 放文件

把三个文件按这个结构放进一个 GitHub 仓库（可与 Vegoil 共用一个 repo，也可单独建）：

```
your-repo/
├─ biofuelscan_brief.py
├─ requirements.txt
└─ .github/
   └─ workflows/
      └─ daily-biofuelscan-brief.yml
```

---

## 三、配置 Secrets

仓库 → Settings → Secrets and variables → Actions → New repository secret。

**必填**：

| Secret | 示例 / 说明 |
|---|---|
| `IMAP_HOST` | imap.exmail.qq.com |
| `IMAP_USER` | 你的完整邮箱地址 |
| `IMAP_PASS` | 邮箱授权码 |
| `SMTP_HOST` | smtp.exmail.qq.com |
| `MAIL_TO` | 简报发到哪（可填多个，逗号分隔） |
| `LLM_API_KEY` | 大模型 API Key（当前 OpenCode Zen） |

**可选**（不填用默认值）：

| Secret | 默认 | 说明 |
|---|---|---|
| `IMAP_PORT` | 993 | |
| `MAILBOX` | INBOX | |
| `SMTP_PORT` | 465 | 用 587/STARTTLS 时配合 `SMTP_SSL=false` |
| `SMTP_SSL` | true | |
| `SMTP_USER` / `SMTP_PASS` | 同 IMAP | 发件账号与收件不同才需填 |
| `MAIL_FROM` | 同 SMTP_USER | |
| `SUBJECT_CONTAINS` | biofuelscan | 主题筛选关键词 |
| `SENDER_CONTAINS` | platts | 发件人加分项（不强制） |
| `ATTACH_PATTERN` | `BF_\d{8}\.pdf` | 附件名正则 |
| `LOOKBACK_DAYS` | 4 | 往回找几天的邮件 |
| `LLM_BASE_URL` | https://opencode.ai/zen/go/v1 | 换服务就覆盖 |
| `LLM_MODEL` | deepseek-v4.1-flash | 换模型就覆盖 |
| `LLM_MAX_TOKENS` | 16000 | 报 400 说明服务端上限更低，调小 |

---

## 四、跑起来

### 手动测试
仓库 → Actions → 选 `daily-biofuelscan-brief` → Run workflow。看日志：出现「命中邮件 / 正文抽取完成 / 已发送」即成功，邮箱应收到简报。

### 每天定时（推荐：cron-job.org 外部触发）
GitHub 自带的 `schedule` 不稳（Vegoil 已踩坑），所以用 cron-job.org 每天 08:30 北京时间用 GitHub API 触发 workflow：

1. 先建一个 GitHub **Fine-grained PAT**：仅授权该仓库的 **Actions: Read and write**。
2. 在 cron-job.org 新建任务：
   - **URL**：`https://api.github.com/repos/<你的用户名>/<仓库名>/actions/workflows/daily-biofuelscan-brief.yml/dispatches`
   - **方法**：POST
   - **时间**：每天 08:30，时区选 Asia/Shanghai
   - **Headers**：
     ```
     Authorization: Bearer <你的PAT>
     Accept: application/vnd.github+json
     X-GitHub-Api-Version: 2022-11-28
     Content-Type: application/json
     ```
   - **Body**：`{"ref":"main"}`

PowerShell 里先手动验证一次触发是否成功（200/204 即对）：
```powershell
$headers = @{
  Authorization          = "Bearer <你的PAT>"
  Accept                 = "application/vnd.github+json"
  "X-GitHub-Api-Version" = "2022-11-28"
}
Invoke-WebRequest -Method POST `
  -Uri "https://api.github.com/repos/<用户名>/<仓库名>/actions/workflows/daily-biofuelscan-brief.yml/dispatches" `
  -Headers $headers -Body '{"ref":"main"}' -ContentType "application/json"
```

---

## 五、本地测试（可选，Windows PowerShell）

```powershell
pip install -r requirements.txt
$env:IMAP_HOST="imap.exmail.qq.com"; $env:IMAP_USER="you@corp.com"; $env:IMAP_PASS="授权码"
$env:SMTP_HOST="smtp.exmail.qq.com"; $env:MAIL_TO="you@gmail.com"
$env:LLM_API_KEY="你的key"
python biofuelscan_brief.py
```

---

## 六、想调的几个点

- **要全文逐句翻译（像 Vegoil 那样）而不是板块总结**：改 `biofuelscan_brief.py` 里的 `PROMPT_TEMPLATE`，把「翻译并总结成…按板块要点」改成「完整、忠实、逐段翻译」，并把 `LLM_MAX_TOKENS` 提到 32000（全文比总结长很多）。脚本其余不动。
- **想纳入 `Subscriber Notes`（方法论/品种变更公告）或 `Assessments Rationale`**：目前抽取在遇到 `Assessments Rationale` 标题时停止。把停止边界后移、或对这两段单独抽取再加一个板块即可——告诉我就帮你改。
- **术语表**：`PROMPT_TEMPLATE` 里已内置一串保留英文缩写（UCOME/SAF/HVO/RINs/FOB ARA/CBOT…），想加专属译法直接在提示词里补。
- **版式若某天变了导致抽取异常**：脚本对「正文 < 800 字符」会报错退出（避免发出空简报），日志能看到。把当天 PDF 发我，照新版式微调过滤规则即可。
