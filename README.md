# 本地 AI 会议工作台

团队内部会议录音、实时转写、说话人识别、AI 助手与定时任务平台。

单仓库 Flask + Jinja2 应用，不使用独立 Vue/React 前端。

## 功能

- 实时会议录音（AudioWorklet PCM 实时流 + MediaRecorder 5 秒可靠分片）
- IndexedDB 本地缓存，服务器 ACK 后才删除本地分片
- 实时 FunASR Paraformer 流式 ASR：稳定前缀才进入在线标点，保留短暂可修订尾部
- 本地 FSMN-VAD 端点断句（800ms 静音 / 20s 硬切）与 FunASR 在线、离线 CT-Transformer 标点
- 固定本机 `Qwen3.8`（`http://127.0.0.1:8000/v1`）负责受约束的批量中文校订、总结与问答
- AI 仅在被明确点名（成就精灵 / AI / 小AI / 助手 + 问句）后发言
- 会后最终转写、总结、决策、Action Items、知识库写入
- 导入本地音频生成完整会议记录（WAV、MP3、M4A/AAC、FLAC、OGG/Opus、WebM）
- APScheduler + LangGraph 定时 AI 任务
- PWA（manifest + Service Worker）

## 环境

- Python 3.11+
- PostgreSQL + pgvector（生产）
- 测试：若未设置 `DATABASE_URL`，`tests/conftest.py` 使用 **SQLite 测试替身**。应用代码仍以 PostgreSQL 方言为默认。

本机若无 Docker，请自行安装 PostgreSQL，或仅跑测试（SQLite 回退）。

## 安装

```bash
cd "/Users/fumen/综合项目/code/会议助手"
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,speech,eval]"
make vendor   # 或见下方手动下载前端 vendor
python scripts/generate_synthetic_meetings.py
python scripts/seed_dev.py
```

### 数据库

```bash
docker compose up -d   # postgres+pgvector，镜像 pgvector/pgvector:pg16
cp .env.example .env
# 编辑 DATABASE_URL=postgresql+psycopg://meeting:meeting@127.0.0.1:5432/meeting_ai
```

未安装 Docker 时：应用仍可启动；pytest 在 `DATABASE_URL` 为空时使用 SQLite。

### 三个进程

Web（Flask-SocketIO `async_mode=threading`）：

```bash
python -m app.web
```

语音 Worker：

```bash
python -m app.workers.speech
```

定时任务 Worker（APScheduler）：

```bash
python -m app.workers.tasks
```

音频导入 Worker（单任务串行，避免与实时会议争抢本机资源）：

```bash
python -m app.workers.imports
```

默认账号：

| 邮箱 | 密码 | 角色 |
|---|---|---|
| admin@local | admin123 | Admin |
| member@local | member123 | Member |
| viewer@local | viewer123 | Viewer |

## 测试

```bash
pytest -m "not local_llm"
```

本机 Qwen3.8 契约测试（确认 8000 服务已启动后）：

```bash
RUN_LOCAL_LLM=1 pytest -m local_llm
```

Playwright：

```bash
playwright install chromium
pytest tests/e2e
```

## 环境变量

见 `.env.example`。关键项：

- `DATABASE_URL` 生产必须是 PostgreSQL
- `SPEECH_BACKEND=funasr`：生产唯一 ASR 后端；`fake` 仅用于测试
- `LLM_BACKEND=qwen38`：生产唯一 LLM 后端
- `LOCAL_LLM_BASE_URL=http://127.0.0.1:8000/v1`
- `LOCAL_LLM_MODEL=mtplx-qwen38-27b-optimized-quality`
- `RUN_LOCAL_LLM=1` 才允许测试本机模型
- `IMPORT_MAX_BYTES=2147483648`、`IMPORT_MAX_DURATION_SECONDS=28800`：导入音频的大小和时长上限

## 本地模型准备

FunASR 权重需在部署机器预先下载到其本地模型缓存；运行期不访问公网。实时链路使用
`funasr/paraformer-zh-streaming`，会后最终转写使用 `paraformer-zh + fsmn-vad + CT-Transformer PNC`。
中文校订只接受带段落 ID 的 JSON，并校验数字不变、长度范围合理；`raw_text` 始终不可修改。

## HTTPS 说明

`getUserMedia`、AudioWorklet、Screen Wake Lock 需要安全上下文。
内网正式部署必须使用 HTTPS（例如 `https://meeting.company.local`），不能用裸 `http://192.168.x.x` 作为手机会议地址。

### 本机局域网访问

本项目可直接用本机生成的内网 CA 运行 HTTPS。服务器证书的 SAN 必须包含当前电脑的局域网 IP；每台手机安装并信任该 CA 后，访问 `https://<电脑局域网IP>:<PORT>`。WebSocket 会自动使用 WSS，浏览器才能正常申请麦克风权限。

对应环境变量为 `HOST=0.0.0.0`、`TLS_CERT_FILE`、`TLS_KEY_FILE`、`TLS_CA_CERT_FILE`；语音 Worker 还应设置 `INTERNAL_LIVE_SCHEME=https` 与 `INTERNAL_LIVE_CA_FILE`，避免实时事件回传因 HTTPS 切换而中断。局域网 IP 变化后，请重新签发服务器证书或改用固定内网 DNS 名称。

迁移到新电脑时可一键生成新的内网证书并更新本项目配置：

```bash
# macOS / Linux
./scripts/generate_lan_tls.sh --write-env
```

```powershell
# Windows PowerShell
.\scripts\generate_lan_tls.ps1 -WriteEnv
```

两套脚本均会自动检测活跃局域网 IP，并生成 `.local/certs/meeting-ai-lan-root-ca.cer`。如自动识别的网卡不对，macOS/Linux 使用 `--ip 192.168.x.x`，Windows 使用 `-Ip 192.168.x.x`。二者需要本机有 OpenSSL；Windows 未安装时可执行 `winget install ShiningLight.OpenSSL.Light` 后重新打开 PowerShell。`--write-env` / `-WriteEnv` 只会更新已有 `.env`，避免意外生成带默认密钥的部署配置；新电脑应先复制 `.env.example` 并完成数据库与 `SECRET_KEY` 配置。执行后重启 Web 和 speech Worker；手机需要重新安装新电脑生成的根证书。已有证书默认不会覆盖，换 IP 时分别用 `--force` 或 `-Force` 重签服务器证书；只有确实需要替换根证书时才使用 `--force --regenerate-ca` 或 `-Force -RegenerateCa`。

## 音频目录

```
data/meetings/<uuid>/
  source/chunk_*.*
  source/import.*              # 导入时保留的原始文件
  meeting_master.wav
  realtime_transcript.json
  final_transcript.json
  summary.json
  metadata.json
```

## 会议状态

实时会议：`CREATED → PREPARING → RECORDING → INTERRUPTED|STOPPING → UPLOADING → PROCESSING → COMPLETED|FAILED`

导入录音：`PROCESSING → COMPLETED|FAILED`；失败后可从会议详情重新处理，原文件不会删除。

非法跳转会被拒绝。原始 `raw_text` 永不可被 AI 覆盖。
