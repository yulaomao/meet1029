# 团队内部 AI 会议记录、会议助手及定时任务系统设计方案

## 一、项目目标

建设一套团队内部使用的 AI 工作平台，核心能力包括：

1. 实时会议录音、语音转文字；
2. 自动区分不同说话人并识别已登记团队成员；
3. AI 定时/定量修正语音识别错误；
4. 自动形成会议总结、决策、风险、待办事项；
5. AI 在被明确点名后，根据当前会议内容和历史知识参与讨论，并通过本地 TTS 语音回答；
6. 支持定时执行 AI 任务；
7. 后续接入飞书、企业微信、邮件等提醒渠道；
8. 支持 Mac、Windows、手机、平板通过浏览器访问；
9. 第一阶段运行于 Apple Silicon MacBook 128GB；
10. 后续能够迁移到 RTX 3090、RTX A6000 或其他 Linux GPU 服务器；
11. 语音识别、说话人分析、TTS 默认全部本地运行；
12. 大模型固定使用本机已部署的 Qwen3.8 OpenAI 兼容网关（`http://127.0.0.1:8000/v1`），不调用外部 API。

---

# 二、总体设计原则

系统采用：

```text
Browser
   │
   ▼
Flask Web Application
   │
   ├── Meeting Engine
   ├── Speech Engine
   ├── AI Engine
   ├── Task Engine
   └── Knowledge Engine
```

前端和后端处于同一个 Python 项目中。

不采用独立 Vue/React 前端工程，不要求单独维护 Node.js 项目。

网页层采用：

```text
Flask
+
Jinja2
+
Bootstrap 5
+
Alpine.js
+
Socket.IO JavaScript Client
+
原生 JavaScript Audio API
```

所有 JavaScript/CSS 依赖均可以保存到项目本地，系统在完全断网环境下仍可正常加载前端。

---

# 三、系统总体架构

```text
                   用户浏览器
       Mac / Windows / iPhone / Android / iPad
                         │
                 HTTPS + Socket.IO
                         │
                         ▼
┌──────────────────────────────────────────────┐
│                  Flask App                   │
│                                              │
│  登录/权限                                   │
│  会议管理                                    │
│  实时会议页面                                │
│  历史会议                                    │
│  AI助手                                      │
│  定时任务                                    │
│  成员管理                                    │
│  知识库                                      │
│  系统设置                                    │
└─────────────────────┬────────────────────────┘
                      │
               Internal Service Layer
                      │
       ┌──────────────┼────────────────┐
       │              │                │
       ▼              ▼                ▼
 Speech Engine     LLM Gateway     Task Engine
       │              │                │
       │              │             LangGraph
       │              │             APScheduler
       │              │
       │         本机 Qwen3.8
       │      127.0.0.1:8000/v1
       │
       ├── VAD
       ├── ASR
       ├── Diarization
       ├── Speaker ID
       ├── Final ASR
       └── TTS
                      │
                      ▼
                PostgreSQL
                  +
                pgvector
                      │
                      ▼
                 文件存储
```

系统采用模块化 Provider 机制，使 Mac MLX 和 NVIDIA CUDA 模型可以互换。

---

# 四、项目部署形态

## Apple Silicon

首选部署：

```text
MacBook 128GB
│
├── Flask
├── PostgreSQL
├── FunASR 本地模型
├── Speaker Models
├── LangGraph
├── APScheduler
└── 本机 Qwen3.8 网关
```

Apple Silicon 模型原生运行，不放入 Docker。

原因是 MLX 需要直接访问 Apple Metal/统一内存。

---

## NVIDIA服务器

以后迁移 RTX 3090/A6000：

```text
Ubuntu Server
│
├── Flask
├── PostgreSQL
├── CUDA Speech Models
├── Local LLM Server
├── LangGraph
├── APScheduler / Temporal
└── nginx
```

网页、数据库结构、会议业务逻辑、Agent 逻辑全部保持不变。

只更换模型 Provider。

---

# 五、网页前端

推荐使用：

```text
Flask Jinja2
Bootstrap 5
Alpine.js
Socket.IO Client
原生 ES Modules
```

页面包括：

```text
/
├── 首页
├── 会议
│   ├── 新建会议
│   ├── 实时会议
│   └── 历史会议
│
├── AI助手
├── 定时任务
├── 知识库
├── 团队成员
└── 系统设置
```

界面必须从一开始采用响应式布局。

同一套网页直接适配：

```text
电脑
手机
平板
```

不另外开发移动网页。

---

# 六、手机会议设计

手机必须能够直接作为会议录音设备。

会议页面点击：

```text
开始会议
```

后执行：

```text
getUserMedia()
      │
      ▼
MediaStream
      │
      ├──────────────► 实时音频流
      │
      └──────────────► 可靠录音流
```

这是整个移动端可靠性的关键。

不能只使用一条 WebSocket 实时音频流。

---

# 七、手机双音频通道

## 7.1 实时音频通道

用于：

```text
实时ASR
实时Speaker识别
实时AI会议记录
```

流程：

```text
手机麦克风
     │
getUserMedia
     │
AudioWorklet
     │
重采样
     │
16kHz Mono PCM16
     │
每100ms左右一个Frame
     │
Socket.IO Binary Event
     │
Flask
     │
Speech Engine
```

AudioWorklet 工作在独立音频线程，适合低延迟实时处理。AudioWorklet 和麦克风访问均要求 HTTPS 安全上下文，因此正式内网部署必须提供 HTTPS。

推荐实时音频：

```text
16000 Hz
16 bit
Mono
```

每100ms约：

```text
3200 bytes
```

网络压力非常小。

---

# 八、可靠录音通道

实时 WebSocket 可能遇到：

- Wi-Fi短暂断线；
- 手机切换AP；
- 浏览器Socket重连；
- 页面暂时冻结。

因此另外运行：

```text
MediaRecorder
```

每：

```text
5 秒
```

生成一个录音分片。

例如：

```text
chunk_000001
chunk_000002
chunk_000003
...
```

Safari 已支持 MediaRecorder；较新的 Safari 还加入了 WebM/Opus 等录制能力。实现时必须使用 `MediaRecorder.isTypeSupported()` 动态选择实际支持格式，而不是写死编码格式。

---

# 九、IndexedDB 本地缓存

每个 MediaRecorder 分片首先保存：

```text
IndexedDB
```

记录：

```text
meeting_id
client_id
sequence
start_time
duration
mime_type
blob
uploaded
```

然后后台上传：

```text
POST /api/meetings/{id}/audio/chunks
```

服务器返回：

```text
ACK
```

确认成功以后才能删除 IndexedDB 中对应分片。

流程：

```text
录音5秒
   │
   ▼
IndexedDB
   │
   ▼
上传服务器
   │
   ▼
服务器ACK
   │
   ▼
删除本地分片
```

如果网络中断：

```text
录音继续
↓
IndexedDB继续保存
↓
网络恢复
↓
自动补传
```

因此：

**实时字幕可以暂时中断，但正式录音不能因为网络瞬断而丢失。**

---

# 十、手机长时间会议运行机制

系统目标支持：

```text
0～2小时持续会议
```

会议开始后请求：

```text
Screen Wake Lock
```

保持手机屏幕不自动关闭。

Screen Wake Lock 已在现代浏览器广泛支持，iOS/iPadOS 的 Home Screen Web App 也已有支持。

同时监听：

```text
visibilitychange

MediaStreamTrack.onended

AudioContext.statechange

Socket reconnect

WakeLock release
```

如果用户切后台：

```text
显示警告：
“会议录音正在进行，请保持会议页面处于前台。”
```

重新回到页面后：

```text
重新申请 Wake Lock
检查麦克风
检查 AudioContext
检查未上传录音
恢复 Socket
```

---

# 十一、移动端能力边界

手机浏览器可以作为正式的会议终端：

```text
手机打开网页
↓
点击开始会议
↓
持续录音
↓
实时上传
↓
实时看到字幕
↓
AI被点名后直接语音回答
```

正式运行条件是：

**会议期间网页保持前台并启用 Screen Wake Lock。**

浏览器无法保证 iPhone 在锁屏或长时间进入系统后台后麦克风仍持续工作。

如果未来明确要求：

```text
锁屏
+
后台
+
连续录音2小时
```

则保留同一套 Flask 后端和网页架构，只需增加轻量：

```text
iOS原生客户端
或
WKWebView Wrapper
```

后端无需修改。

---

# 十二、会议主录音设备

一个会议默认只允许一台设备成为：

```text
Recording Host
```

例如：

```text
会议室MacBook
```

或者：

```text
主持人的iPhone
```

其他人打开会议页面可以：

- 查看实时字幕；
- 查看AI总结；
- 查看Action Items；
- 调用AI；
- 修改会议内容；

但不会同时上传会议室声音。

避免多个手机产生重复录音。

后续如果开发远程会议，再扩展：

```text
Multi Track Audio
```

模式。

---

# 十三、实时语音处理链路

推荐：

```text
PCM Audio
   │
   ▼
VAD
   │
   ▼
Streaming ASR
   │
   ├──────────────┐
   │              │
   ▼              ▼
Transcript     Diarization
                  │
                  ▼
              Speaker ID
                  │
                  ▼
           Transcript Fusion
                  │
                  ▼
              实时逐字稿
```

---

# 十四、VAD

负责判断：

```text
Speech
Silence
```

建议：

```text
Silero VAD
```

用于：

- 避免大量静音送给ASR；
- 判断一句话开始；
- 判断一句话结束；
- AI问题结束判断；
- 优化ASR分片。

---

# 十五、实时ASR

Apple Silicon 首选：

```text
Nemotron 3.5 ASR Streaming 0.6B
+
MLX-Audio
```

推荐模型：

```text
mlx-community/
nemotron-3.5-asr-streaming-0.6b-8bit
```

该模型约600M参数，MLX版本支持 cache-aware streaming，并支持包括 `zh-CN` 在内的40种语言区域。

MLX-Audio本身已经提供实时 WebSocket transcription 和流式STT能力。

系统将模型封装为：

```python
ASRProvider
```

接口：

```text
start_session()

feed_audio()

get_partial()

get_final()

reset()

close()
```

---

# 十六、专业词库

系统维护：

```text
Terminology
```

内容例如：

```text
FusionTrack
Atracsys
NDI
SlicerIGT
EtherCAT
针尖球心补偿
球心标定
股骨远端
胫骨平台
截骨
```

字段：

```text
term
aliases
description
project_id
priority
enabled
```

词库同时提供给：

```text
ASR
LLM纠错
AI会议助手
最终转写
```

---

# 十七、实时说话人分离

典型会议为：

```text
2～4人
```

当前本机实现：

```text
VAD 完整话轮
↓
CAM++ 本地说话人向量（CPU）
↓
在线余弦聚类（Speaker 1 … Speaker 8）
```

只对至少 1.5 秒的完整人声话轮取向量；短片段保持上一位说话人，以避免单人会议被错误拆分。相似度足够高则归入既有簇，低相似度且未满上限时才开新簇。已登记声纹仅在高置信时将匿名簇替换为姓名。

`Sortformer Streaming` 保留为以后可验证的 MLX Provider；在真实模型与流式缓存尚未部署前不得以占位 Provider 冒充在线分离。

输出：

```text
Speaker_0
Speaker_1
Speaker_2
Speaker_3
```

---

# 十八、超过4人的会议

系统不能依赖固定4个Speaker Slot。

Diarization统一抽象：

```python
DiarizationProvider
```

实现：

```text
SortformerProvider

EmbeddingClusteringProvider

PyannoteProvider

CUDAProvider
```

当：

```text
预计人数 <= 4
```

采用：

```text
Sortformer
```

当：

```text
预计人数 > 4
```

采用：

```text
Speaker Segmentation
      ↓
Speaker Embedding
      ↓
Online Clustering
      ↓
Speaker 0 ... N
```

会议设置中允许用户提前输入：

```text
预计参会人数
```

同时系统允许动态切换/最终重新计算。

---

# 十九、成员声纹注册

团队成员可以进入：

```text
成员管理
↓
声纹注册
```

录制：

```text
30～60秒清晰语音
```

生成 Speaker Embedding。

数据库保存：

```text
SpeakerProfile
```

会议中：

```text
Speaker_2
     │
     ▼
Speaker Embedding
     │
     ▼
Voiceprint Matching
     │
     ▼
张工
```

设置两个阈值：

```text
高置信度 → 自动确认

中置信度 → 候选姓名

低置信度 → 未知成员
```

不能低置信度强行认人。

---

# 二十、ASR与Speaker融合

实时ASR产生：

```text
start_time
end_time
text
```

Diarization产生：

```text
speaker
start_time
end_time
probability
```

Transcript Fusion 根据：

```text
时间重叠比例
+
Speaker probability
```

确定文本所属人员。

例如：

```text
14:31:05  张工
这个针尖球心补偿需要重新验证。
```

如果检测到明显重叠讲话：

```text
overlap = true
```

最终会议处理中重新校正。

---

# 二十一、实时转写数据

每一条内容保存：

```text
TranscriptSegment
```

字段：

```text
id
meeting_id

start_time
end_time

speaker_cluster_id
speaker_profile_id
speaker_name

raw_text
corrected_text
final_text

asr_confidence
speaker_confidence

is_overlap

created_at
updated_at
```

必须保留：

```text
raw_text
```

AI永远不允许覆盖原始识别结果。

---

# 二十二、AI自动纠错

实时ASR不是最终会议文本。

系统设置：

```text
Correction Queue
```

满足任意条件触发：

```text
30秒
```

或：

```text
500～800中文字
```

或：

```text
检测到一个完整讨论段落结束
```

执行（每条目标句只取前 3 条、后 1 条已落库人类发言作只读上下文；不得把上下文拼入目标句）：

```text
raw transcript
+
前后文
+
专业词库
↓
LLM
↓
corrected transcript
```

---

# 二十三、AI纠错原则

LLM只允许：

- 修复明显ASR错字；
- 修复同音词；
- 修复专业术语；

实时校订不负责断句和标点；这两项由本地 PNC 模型独立完成。

禁止：

- 增加没有说过的信息；
- 删除观点；
- 修改数字；
- 修改事实；
- 改变原意；
- 将原话直接总结掉。

校订分两层：

1. 本地规则先删除可机械证明的紧邻口吃（如 `我我我觉得→我觉得`、`这个这个方案→这个方案`）；保留“嗯嗯”、`看看` 等可能表达语气或词义的重复；
2. Qwen3.8 在完整片段和有限前后文上输出可审计的 `corrected_text`。它可一次修复多处明确同音错字、术语、英文型号、ASR 重启造成的重复与无意义测试问候；不能总结、补写上下文信息或改变原意。

LLM输出完整修订稿及置信度，而不是不自然的单点替换：

```json
{
  "segments": [
    {
      "id": "segment-id",
      "corrected_text": "目前来看，实施性目标以及各种问题都还可以。",
      "confidence": "high"
    }
  ]
}
```

服务端只接受：原文至少8字、修订稿长度为原文的45%–135%、所有独立数字/日期保持不变、字符相似度不低于阈值（默认0.55）。这允许多点 ASR 修复而拒绝摘要式重写；低置信度和语义不明的片段一律保留 ASR+PNC 原文。原始 ASR 永不覆盖，所有校订均可恢复。

前端允许：

```text
查看原文
查看修正版
查看Diff
人工修改
恢复原文
```

---

# 二十四、Meeting State

会议最长可能达到2小时。

AI不能每次收到问题时重新读取完整会议。

因此维护：

```text
MeetingState
```

内容：

```text
current_topic

rolling_summary

confirmed_facts[]

decisions[]

action_items[]

open_questions[]

risks[]

participants[]
```

每：

```text
1～2分钟
```

更新一次。

同时保存版本：

```text
MeetingStateVersion
```

---

# 二十五、AI会议助手

AI默认：

```text
不主动发言
```

只响应明确点名。

唤醒词例如：

```text
AI
小AI
助手
```

例如：

```text
AI，你怎么看这个方案？

AI，刚才讨论的方案有什么风险？

AI，总结一下现在的结论。
```

无需单独训练 Wake Word 模型。

直接利用ASR文字识别：

```text
唤醒词 + 问题
```

---

# 二十六、AI回答上下文

用户点名AI后：

```text
Current Question
      │
      ▼
最近3～5分钟Transcript
      +
MeetingState
      +
项目Terminology
      +
相关历史会议
      +
知识库RAG
      │
      ▼
LLM
```

这样无需把两小时会议记录全部发送给模型。

---

# 二十七、LLM Gateway

大模型统一封装：

```python
LLMProvider
```

实现：

```text
Qwen38LocalProvider
```

当前采用：

```text
本机 Qwen3.8 OpenAI-compatible Gateway
http://127.0.0.1:8000/v1
```

模型：

```text
mtplx-qwen38-27b-optimized-quality
```

负责：

- ASR纠错；
- MeetingState；
- 会议问答；
- 普通总结；
- 普通Agent。

所有总结、问答、结构化抽取和批量校订均通过这个本机网关串行调度；不把会议内容发送到公网。

---

# 二十八、本地 Qwen3.8 配置

部署时固定：

```yaml
llm:
  provider: qwen38
  base_url: http://127.0.0.1:8000/v1
  model: mtplx-qwen38-27b-optimized-quality
```

系统统一调用：

```text
OpenAI Compatible API
```

业务层只依赖 OpenAI-compatible 协议；模型替换必须先通过本地契约与质量基准测试。

---

# 二十九、本地TTS

采用：

```text
Qwen3-TTS 1.7B CustomVoice（6-bit）
+
MLX-Audio
```

首选：

```text
mlx-community/
Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit
```

Qwen3-TTS的MLX实现已经支持流式生成，可以在整段文字未完全生成之前持续输出音频。

流程：

```text
LLM Streaming Token
       │
       ▼
Sentence Buffer
       │
       ▼
Qwen3-TTS
       │
       ▼
Audio Chunk
       │
       ▼
Socket.IO
       │
       ▼
Browser Audio Queue
       │
       ▼
扬声器
```

---

# 三十、AI回答播放

LLM不等待完整答案。

例如：

```text
“这里主要有三个问题。”
```

句子缓冲达到自然短句边界后马上送TTS；首句优先等到标点或逗号等自然边界，最长约36个字符才切出，后续片段优先在标点或约72个字符边界切分；不等待整段回答。所有片段固定同一 CustomVoice、`chinese` 语言、同一风格指令与受控采样，并以轻微固定播放速率播放，避免逐片音色、口音和语速漂移。

发送给 TTS 前必须做**仅影响朗读、不改 UI 文本与会议记录**的规范化：移除 Markdown 强调/列表符号，合并空白，按中文朗读转换常见量纲（如 `850nm` → `八百五十纳米`）。Qwen3-TTS 的默认 `4096` 声学 token 上限不得直接用于短回答；按朗读字符数动态限制 token（默认每字约 4.2 token，且最多 420 token），防止遗漏 EOS 时出现分钟级拖长音。采样使用中等温度、受限 top-k/top-p 和重复惩罚，而不是过低温度。

随后继续：

```text
“第一，当前补偿值……”
```

因此：

```text
LLM生成
+
TTS合成
+
浏览器播放
```

并行执行。

---

# 三十一、AI说话防止自我识别

AI播放语音时，麦克风可能重新录到AI自己的声音。

采用四层措施：

1. `getUserMedia` 开启浏览器 echoCancellation；
2. AI生成文本直接作为 `speaker=AI` 写进会议记录；
3. AI播放期间禁止相同内容再次触发Wake检测；
4. 对与当前TTS文本高度相似的ASR结果进行Echo过滤。

状态：

```text
AI_SPEAKING
```

AI结束：

```text
LISTENING
```

## 三十一点一、可打断AI（Barge-in）

AI 说话不是独占麦克风。状态机为：

```text
IDLE → THINKING → SPEAKING → BARGE_IN_PENDING → DIALOGUE_LISTENING
```

规则：

1. AI回答中，浏览器仍持续发送原始麦克风 PCM；
2. VAD 只用于收集话语，**不**直接打断；必须等到本地 ASR 最终句，先通过回声过滤，再进行有效表达判定。默认要求至少 5 个词法字符且有效发声不少于 600ms；“请慢点”“再说”等包含控制/提问意图的短句允许从 3 个字符触发；
3. “嗯、啊、啊呀、哦、好、好的、哈哈”等语气词/附和词、纯填充音以及短促噪声均不打断。通过判定后才立即取消尚未消费的 LLM token、停止 TTS chunk 生成，并向浏览器发送 `tts_stop`；浏览器必须停止所有已排队 AudioBuffer 和 HTMLAudio；
4. 页面同时提供“打断回答”按钮，按钮通过本机控制标记通知语音 worker；该按钮只停止当前声音，不结束本轮对话；
5. 为避免扬声器回授误触发，必须保留浏览器 AEC、TTS 文本回声过滤和只允许本机回环控制；外接麦克风/耳机仍是最佳实践。

被打断的草稿只显示在当次 UI，不写入正式 AI 会议记录；完成的回答才持久化为 `speaker=AI`。

明确唤起（如“成就精灵，帮我分析……”）后进入连续对话窗口。若用户以通过上述语义判定的发言打断，当前生成与播放立即取消；该最终 ASR 句作为追问继续回答，不要求再次唤醒。只有明确口令（如“成就精灵停止回复”）才结束本轮；120 秒无有效追问则自动回到普通监听，防止会议中无关谈话被误接管。

会议室条件允许时建议：

```text
外接会议麦克风
+
外接音箱
```

进一步改善效果。

---

# 三十二、会议结束后的最终处理

结束会议以后，不直接把实时转写当正式记录。

执行：

```text
Final Meeting Pipeline
```

流程：

```text
可靠录音分片
      │
      ▼
合并完整会议音频
      │
      ▼
Final ASR
      │
      ▼
Final Diarization
      │
      ▼
Speaker Identity
      │
      ▼
LLM Correction
      │
      ▼
Final Transcript
```

---

# 三十三、最终高质量转写

首选：

```text
MOSS-Transcribe-Diarize 0.9B
```

MOSS主要针对：

- 长时间音频；
- 多说话人；
- 时间戳；
- 会议转写；
- Speaker Diarization。

官方模型支持50+语言。

MLX-Audio中当前也已经存在 MOSS-Transcribe-Diarize 的MLX实现，因此可以纳入 Apple Silicon 本地处理链路。

---

# 三十四、最终Speaker校验

对于：

```text
>4人会议

Speaker重叠较多

实时Speaker置信度较低
```

增加：

```text
pyannote Community-1
```

用于最终Speaker重新计算。

Community-1支持离线部署，并改进Speaker数量估计以及与ASR时间戳的融合。

最终正式会议记录以：

```text
Final Transcript
```

为准。

实时Transcript全部保留用于追溯。

---

# 三十五、会议自动分析

Final Transcript完成后自动执行：

```text
Meeting Analysis
```

生成：

```json
{
  "summary": "",
  "topics": [],
  "decisions": [],
  "action_items": [],
  "risks": [],
  "open_questions": [],
  "follow_ups": []
}
```

---

# 三十六、Action Item

每个Action Item：

```text
description

owner

deadline

priority

status

source_segment_id
```

必须保存：

```text
source_segment_id
```

用户点击：

```text
“张工负责完成针尖球心补偿验证”
```

即可跳转到：

```text
原始会议文字
+
对应录音时间
```

---

# 三十七、知识库

系统预留：

```text
KnowledgeDocument
KnowledgeChunk
Embedding
```

使用：

```text
PostgreSQL
+
pgvector
```

内容可以包括：

- 历史会议；
- 技术资料；
- 产品说明；
- 项目文档；
- 测试报告；
- 工作流程；
- 团队规范。

会议AI被点名后，可基于：

```text
当前会议
+
历史会议
+
知识库
```

回答。

---

# 三十八、定时AI任务

任务系统采用：

```text
APScheduler
+
LangGraph
```

APScheduler负责：

```text
什么时候执行
```

LangGraph负责：

```text
AI怎么完成
```

APScheduler支持将任务持久化到SQLAlchemy/PostgreSQL Job Store，避免程序重启后定时任务丢失。

---

# 三十九、定时任务模型

```text
ScheduledTask
```

字段：

```text
id
name
description

prompt

schedule_type
run_at
cron_expression

llm_provider
model
reasoning_level

tool_permissions

project_id

timeout
retry_count

enabled

created_by
created_at
updated_at
```

---

# 四十、任务执行记录

每次执行生成：

```text
TaskRun
```

包括：

```text
task_id

started_at
finished_at

status

input

output

tool_calls

error

retry_count

token_usage
```

状态：

```text
PENDING
RUNNING
SUCCESS
FAILED
RETRYING
CANCELLED
```

---

# 四十一、Agent Tools

第一阶段允许：

```text
search_meetings

get_meeting

search_transcripts

search_action_items

search_knowledge

read_file

write_file

create_report

http_request
```

高风险工具默认禁止：

```text
execute_shell

delete_file

send_message

modify_system
```

以后通过权限系统单独开放。

---

# 四十二、提醒接口

建立统一：

```python
NotificationProvider
```

接口：

```text
send(
    recipient,
    title,
    content
)
```

后续实现：

```text
FeishuProvider

WeComProvider

EmailProvider

SMSProvider
```

第一阶段保留空实现即可。

---

# 四十三、数据库

正式采用：

```text
PostgreSQL
+
pgvector
```

主要表：

```text
users
roles

projects

members
speaker_profiles

terminology

meetings
meeting_clients
meeting_participants

audio_chunks
audio_upload_sessions

transcript_segments

meeting_state_versions

meeting_summaries

decisions
action_items

knowledge_documents
knowledge_chunks

scheduled_tasks
task_runs
tool_calls

notifications

audit_logs
```

---

# 四十四、音频文件结构

```text
data/
└── meetings/
    └── <meeting_uuid>/
        │
        ├── source/
        │   ├── chunk_000001.*
        │   ├── chunk_000002.*
        │   └── ...
        │
        ├── meeting_master.wav
        │
        ├── realtime_transcript.json
        │
        ├── final_transcript.json
        │
        ├── summary.json
        │
        └── metadata.json
```

服务端根据sequence检查：

```text
1
2
3
4
...
```

发现缺失：

```text
missing chunk
```

立即通知浏览器重新上传。

---

# 四十五、Flask实时通信

采用：

```text
Flask-SocketIO
```

Flask-SocketIO提供浏览器与Flask之间的低延迟双向通信，并支持WebSocket。官方目前推荐优先使用 threading 或 gevent；eventlet已不再积极维护。

本项目建议：

```text
async_mode = threading
```

模型推理不直接运行在Socket事件线程中。

---

# 四十六、Socket.IO事件

连接：

```text
/meeting
```

客户端发送：

```text
meeting.join

meeting.leave

audio.realtime

ai.request

client.heartbeat
```

服务器返回：

```text
asr.partial

asr.final

speaker.update

transcript.corrected

meeting.state

ai.started

ai.text.delta

ai.audio.chunk

ai.finished

audio.chunk.missing

system.warning

system.error
```

---

# 四十七、AI模型不能阻塞Flask

Flask负责：

```text
HTTP
Socket
用户
会议状态
```

模型运行必须进入：

```text
Worker
```

同一个Python项目内分为：

```text
Web Process

Speech Worker

Task Worker
```

但所有代码仍然属于同一个Python工程。

例如：

```text
python -m app.web

python -m app.workers.speech

python -m app.workers.tasks
```

避免一次MOSS或LLM任务卡住整个网页。

---

# 四十八、项目目录

```text
meeting_ai/

├── app/
│
│   ├── __init__.py
│   ├── config.py
│
│   ├── web/
│   │   ├── routes.py
│   │   ├── auth.py
│   │   └── socket_events.py
│
│   ├── templates/
│   │   ├── base.html
│   │   ├── index.html
│   │   ├── meeting_live.html
│   │   ├── meeting_detail.html
│   │   ├── tasks.html
│   │   ├── knowledge.html
│   │   ├── members.html
│   │   └── settings.html
│
│   ├── static/
│   │   ├── css/
│   │   ├── js/
│   │   │   ├── meeting/
│   │   │   │   ├── microphone.js
│   │   │   │   ├── audio-worklet.js
│   │   │   │   ├── recorder.js
│   │   │   │   ├── indexeddb.js
│   │   │   │   ├── uploader.js
│   │   │   │   └── player.js
│   │   │   │
│   │   │   └── socket.js
│   │   │
│   │   └── vendor/
│
│   ├── meeting/
│   │   ├── service.py
│   │   ├── transcript.py
│   │   ├── correction.py
│   │   ├── state.py
│   │   └── analysis.py
│
│   ├── audio/
│   │   ├── receiver.py
│   │   ├── chunks.py
│   │   ├── recorder.py
│   │   ├── vad.py
│   │   └── fusion.py
│
│   ├── speech/
│   │   ├── asr/
│   │   ├── diarization/
│   │   ├── speaker_id/
│   │   ├── final_asr/
│   │   └── tts/
│
│   ├── llm/
│   │   ├── base.py
│   │   ├── deepseek.py
│   │   └── local.py
│
│   ├── agents/
│   │   ├── graph.py
│   │   ├── tools/
│   │   └── runner.py
│
│   ├── scheduler/
│   │   └── service.py
│
│   ├── knowledge/
│
│   ├── notifications/
│
│   ├── workers/
│   │   ├── speech.py
│   │   └── tasks.py
│
│   └── database/
│       ├── models/
│       └── repositories/
│
├── migrations/
├── models/
├── data/
├── tests/
├── scripts/
├── config/
├── requirements.txt
└── run.py
```

---

# 四十九、Provider设计

核心模型全部Provider化：

```text
ASRProvider

DiarizationProvider

SpeakerIDProvider

FinalASRProvider

TTSProvider

LLMProvider

EmbeddingProvider

NotificationProvider
```

例如：

```text
ASRProvider

├── MLXNemotronProvider
├── CUDAFunASRProvider
└── OtherProvider
```

TTS：

```text
TTSProvider

├── MLXQwen3TTSProvider
├── CUDAQwen3TTSProvider
└── CosyVoiceProvider
```

因此迁移：

```text
MacBook
↓
RTX3090
↓
RTX A6000
```

不修改业务逻辑。

---

# 五十、配置文件

例如：

```yaml
speech:

  asr:
    provider: funasr
    model: funasr/paraformer-zh-streaming

  diarization:
    provider: local_embedding_cluster

  speaker_id:
    provider: sherpa

  tts:
    provider: macos_say


llm:

  default:
    provider: qwen38
    model: mtplx-qwen38-27b-optimized-quality

  advanced:
    provider: qwen38
    model: mtplx-qwen38-27b-optimized-quality


scheduler:
  provider: apscheduler
```

以后：

```yaml
llm:

  default:
    provider: local
    base_url: http://localhost:8000/v1
    model: local-27b
```

即可。

---

# 五十一、HTTPS

因为：

```text
getUserMedia
AudioWorklet
Wake Lock
```

均涉及浏览器安全策略，移动端正式访问必须使用HTTPS。

因此不能正式使用：

```text
http://192.168.1.10
```

作为手机会议地址。

推荐：

```text
https://meeting.company.local
```

或者：

```text
https://meeting.company-domain.com
```

解析到内网服务器。

证书可以采用：

```text
内部CA
```

或者：

```text
DNS Challenge申请正式TLS证书
```

---

# 五十二、PWA

网页增加：

```text
manifest.json
service-worker.js
```

允许手机：

```text
添加到主屏幕
```

形成接近App的体验。

PWA负责：

- 图标；
- 首页入口；
- 静态资源缓存；
- 全屏模式；
- 快速进入会议。

但PWA不能作为后台持续录音的技术保证。

---

# 五十三、断线恢复

Socket.IO断线：

```text
自动重连
```

实时音频可能暂时中断。

但：

```text
MediaRecorder
+
IndexedDB
```

继续保存。

恢复网络：

```text
补传录音
↓
后台重新识别缺失段
↓
补齐Transcript
```

因此系统设计目标是：

```text
Realtime best effort
+
Recording reliable delivery
```

---

# 五十四、会议异常恢复

数据库记录：

```text
MeetingStatus

CREATED
RECORDING
INTERRUPTED
PROCESSING
COMPLETED
FAILED
```

如果：

```text
Flask重启
浏览器刷新
网络中断
```

再次进入会议页面时可以：

```text
恢复会议
```

系统检查：

```text
未上传chunk
最后sequence
最后Transcript
MeetingState
```

继续工作。

---

# 五十五、权限

至少包括：

```text
Admin

Member

Viewer
```

Admin：

- 系统设置；
- 成员；
- 模型配置；
- API Key；
- 删除会议。

Member：

- 创建会议；
- 参加会议；
- 使用AI；
- 创建任务。

Viewer：

- 查看指定会议和结果。

---

# 五十六、数据安全

默认策略：

```text
原始录音不离开内网

ASR不离开内网

Speaker信息不离开内网

TTS不离开内网
```

当前唯一需要外发的是：

```text
给DeepSeek的文本
```

LLM Gateway提供：

```text
allow_external_llm
```

以及：

```text
Local Only
```

模式。

切换本地27B后：

```text
所有数据完全留在内网
```

---

# 五十七、API Key

DeepSeek API Key：

```text
只保存在服务器
```

浏览器绝不能拿到API Key。

建议通过：

```text
.env
```

或者：

```text
OS Keychain / Secret Manager
```

加载。

---

# 五十八、审计

保存：

```text
audit_logs
```

内容：

```text
user
operation
target
time
ip
result
```

重点记录：

```text
会议删除
任务创建
任务修改
成员权限修改
模型配置修改
外部LLM调用
```

---

# 五十九、主要页面

## 首页

```text
开始会议

最近会议

待处理事项

定时任务

知识库
```

---

## 实时会议

```text
┌──────────────────────────────────────┐
│ 项目技术会议       01:21:36   结束   │
├────────────────────┬─────────────────┤
│                    │ 当前会议状态    │
│ 实时逐字稿         │                 │
│                    │ 当前话题        │
│ 张工：……           │ 决策            │
│ 李工：……           │ 待解决问题      │
│ 王工：……           │ Action Items    │
│                    │                 │
├────────────────────┴─────────────────┤
│ AI状态：监听中 / 回答中              │
│ 网络：正常  录音：正常  缓存：0      │
└──────────────────────────────────────┘
```

手机布局自动变成单列。

---

# 六十、实时页面必须显示状态

必须显示：

```text
麦克风：正常

实时上传：正常

可靠录音：正常

本地缓存：3个分片

服务器：在线

ASR：在线

Speaker：在线

AI：监听中
```

如果任何关键模块异常，立即红色提示。

不能让用户会议结束以后才发现没有录上。

---

# 六十一、会议详情

包括：

```text
会议总结

重要议题

完整逐字稿

决策

Action Items

风险

未解决问题

AI发言

原始录音
```

逐字稿支持：

```text
点击文字
↓
播放对应录音
```

---

# 六十二、定时任务页面

支持：

```text
任务名称

执行时间

重复规则

Prompt

关联项目

使用模型

允许工具

最大运行时间

失败重试
```

显示：

```text
下次执行

最近执行

执行结果

耗时

失败原因
```

---

# 六十三、开发顺序

## 第一阶段

打通：

```text
Flask
+
用户
+
会议创建
+
手机/电脑麦克风
+
实时PCM
+
可靠录音Chunk
```

优先保证2小时稳定录音。

---

## 第二阶段

加入：

```text
Streaming ASR
+
实时Transcript
```

---

## 第三阶段

加入：

```text
Sortformer
+
Speaker ID
```

---

## 第四阶段

加入：

```text
DeepSeek Correction
+
MeetingState
```

---

## 第五阶段

加入：

```text
AI点名
+
DeepSeek Streaming
+
Qwen3-TTS
```

---

## 第六阶段

加入：

```text
MOSS Final Processing
+
Final Diarization
+
Summary
+
Action Items
```

---

## 第七阶段

加入：

```text
LangGraph
+
APScheduler
+
定时AI任务
```

---

## 第八阶段

加入：

```text
LocalLLMProvider
```

切换内网27B。

---

# 六十四、核心验收指标

## 手机持续会议

必须实现：

```text
iPhone / Android
网页前台
屏幕常亮
连续录音2小时
```

过程中：

- 不崩溃；
- 录音不丢；
- 网络短暂中断可自动补传；
- 页面刷新后可以恢复未上传Chunk。

---

## 录音完整性

服务器最终Chunk Sequence：

```text
连续
```

发现缺失必须自动补传。

最终：

```text
会议音频缺失时间 = 0
```

在浏览器持续拥有麦克风采集能力的前提下。

---

## ASR

正常会议环境：

```text
讲话后约1～2秒
```

出现可读实时文字。

---

## Speaker

典型2～4名已登记团队人员：

目标：

```text
Speaker归属准确率 >= 90%
```

并允许人工修正。

---

## AI纠错

必须做到：

```text
原文可查看
修正版可查看
Diff可查看
可以恢复
```

---

## AI回答

被点名后：

```text
问题结束
↓
LLM开始输出
↓
TTS开始播放
```

目标：

```text
预热后，首个 LLM token 与首个 TTS PCM 块应在本机分别快速返回；总体目标是短句首音 < 1 秒（不含模型服务排队）
```

主要受网络LLM延迟影响。

---

## 会后处理

2小时会议能够自动产生：

```text
完整正式Transcript
会议总结
决策
Action Items
风险
未解决问题
```

---

## 定时任务

必须支持：

```text
一次性任务

每日

每周

Cron

失败重试

运行历史

人工重新执行
```

---

# 六十五、正式技术选型

```text
Web Framework
Flask

HTML
Jinja2

UI
Bootstrap 5

前端状态
Alpine.js

Realtime
Flask-SocketIO

Browser Audio
getUserMedia
AudioWorklet
MediaRecorder
Web Audio API

可靠缓存
IndexedDB

PWA
Service Worker
Web Manifest

Database
PostgreSQL

Vector
pgvector

VAD
Silero VAD

Mac实时ASR
Nemotron 3.5 ASR Streaming 0.6B
MLX-Audio

实时4人Speaker
Sortformer Streaming v2.1
MLX-Audio

多人Speaker
Segmentation
+
Embedding
+
Clustering

Speaker Identity
Speaker Embedding / Voiceprint

最终ASR
MOSS-Transcribe-Diarize 0.9B

最终Speaker校验
pyannote Community-1

Current LLM
DeepSeek-V4-Flash
DeepSeek-V4-Pro

Future LLM
OpenAI-compatible Local 27B

TTS
Qwen3-TTS 0.6B
MLX-Audio

Agent
LangGraph

Scheduler
APScheduler

Future Enterprise Scheduler
Temporal

Notification
NotificationProvider
```

---

# 六十六、最终系统数据流

```text
                 手机 / 电脑浏览器
                        │
                     麦克风
                        │
              ┌─────────┴──────────┐
              │                    │
              ▼                    ▼
         AudioWorklet         MediaRecorder
              │                    │
         16k PCM实时流          5秒Chunk
              │                    │
         Socket.IO              IndexedDB
              │                    │
              │                可靠上传
              │                    │
              └──────────┬─────────┘
                         ▼
                       Flask
                         │
               ┌─────────┴──────────┐
               │                    │
               ▼                    ▼
           Speech Engine       Recording Store
               │
       ┌───────┼─────────┐
       ▼       ▼         ▼
      VAD     ASR      Speaker
               │         │
               └────┬────┘
                    ▼
             Transcript Fusion
                    │
                    ▼
               实时逐字稿
                    │
             30秒/500字
                    │
                    ▼
                LLM纠错
                    │
                    ▼
              Meeting State
                    │
        ┌───────────┴────────────┐
        │                        │
     未点名AI                 点名AI
        │                        │
      不回答                     ▼
                             LLM Gateway
                                  │
                           DeepSeek / Local27B
                                  │
                            Streaming Text
                                  │
                             Qwen3-TTS
                                  │
                              Audio Chunk
                                  │
                                浏览器
                                  │
                               扬声器


                 【会议结束】

                  完整会议录音
                       │
                       ▼
             MOSS Final Transcribe
                       │
                       ▼
              Final Diarization
                       │
                       ▼
                Speaker Identity
                       │
                       ▼
                  LLM纠错
                       │
                       ▼
                Final Transcript
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
        Summary     Decisions    ActionItems
                       │
                       ▼
               PostgreSQL / pgvector


                 【定时AI任务】

                   APScheduler
                       │
                       ▼
                    LangGraph
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
        Meetings    Knowledge      Files
          └────────────┼────────────┘
                       ▼
                  LLM Gateway
                       │
                       ▼
                   TaskResult
                       │
                       ▼
             NotificationProvider
```

---

# 六十七、最终实施原则

系统正式实施时必须坚持以下原则：

**第一，会议录音可靠性优先于实时字幕。**

实时流发生瞬时异常不能造成正式会议录音丢失。

**第二，手机采用实时PCM + MediaRecorder可靠分片双通道。**

这是移动网页长时间会议的核心设计。

**第三，所有模型均Provider化。**

Mac MLX和NVIDIA CUDA不能写入Meeting业务代码。

**第四，Flask只承担Web和业务调度。**

长时间模型推理由Worker处理。

**第五，原始数据永不被AI覆盖。**

保存：

```text
Raw Audio
Raw Transcript
Corrected Transcript
Final Transcript
```

**第六，AI默认不主动发言。**

只有明确点名才调用LLM和TTS。

**第七，所有前端资源与后端代码属于一个Python工程。**

后续只部署一个项目仓库。

**第八，移动端正式地址必须HTTPS。**

确保麦克风、AudioWorklet、Wake Lock等能力稳定可用。

**第九，模型推理始终留在本机 Qwen3.8 网关内。**

更换本地模型前必须通过 Qwen-compatible 契约、中文转写和校订安全基准测试。

**第十，整个架构必须同时服务于MacBook单机和RTX 3090/A6000服务器。**

不得出现依赖某一硬件平台的业务代码。

按照以上方案开发，可以先在MacBook 128GB上形成完整可用系统，同时保留后续迁移到RTX 3090、RTX A6000以及完全内网27B模型的能力，不需要重新设计前端、数据库、会议业务或定时Agent体系。
