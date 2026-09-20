# 模型技术视频讲解

一个可运行的 macOS 本地 Demo：连续滚动阅读 PDF，逐字拖选或跨页选中原文，生成有中文配音、原文动态划线、鼠标引导、流程图和字幕的 MP4；公式和架构图支持框选

## 启动

```bash
python3 scripts/studio.py start --paper '/absolute/path/paper.pdf'
```

打开输出的本地地址，连续浏览 PDF 并自由选字；将原文手动复制到主会话并发送生成指令，右侧仅展示视频进度、播放器、章节、下载入口和历史视频，不再展示选区引用卡片、复制按钮或引导说明

首启会在 `~/.local/share/paper-segment-video/venv` 安装固定版本依赖，插件目录不存储用户论文与生成内容；默认需要已登录的 Codex CLI 和 macOS `say` 中文 Tingting 语音，模型推理使用现有账户配额

## 生成流程

保留选段坐标 → 补齐上下文 → 拆分知识点 → 生成分镜 → 原文引用与知识覆盖校验 → 独立模型内容复核 → 真实中文语音 → 原文批注视频 → 解码检查音视频流

没有预置演示答案，也不会在模型调用失败时以固定文本冒充生成；失败显示可读错误，可取消和重试，进程重启会标记被中断的普通选段任务

## 整章生成

在主会话明确提出整章讲解后，先核对章节边界，再为每章提交一个任务

```bash
python3 scripts/studio.py chapters --document <document-id>
python3 scripts/studio.py chapter --document <document-id> --chapter 4
python3 scripts/studio.py retry --job-id <chapter-job-id>
```

章节按 PDF 原文边界分段，独立生成与审核；暂时性失败最多重试三次，审核失败的大段最多再细分两层，全部原文片段按顺序覆盖且不重复

每段完成后保存视频、字幕和审核指纹；断点续做复用已完成片段及已审核讲稿，损坏或指纹不匹配的缓存须重新生成和审核，服务重启最多自动恢复三次

最终合并会重排章节索引、动态流程时间点和字幕时间，核验音视频流及总时长后才发布一条完整 MP4；原有界面、播放器、语音设置与普通选段入口保持原样

## 可选配置

| 变量 | 用途 |
| --- | --- |
| `PAPER_VIDEO_MODEL` | 可选模型名，默认沿用 Codex 默认模型 |
| `PAPER_VIDEO_VOICE` | macOS 语音名称，默认 Tingting |
| `PAPER_VIDEO_FONT` | 支持中文的 TTF/TTC 文件路径 |
| `PAPER_VIDEO_DATA` | 用户论文、SQLite 与视频目录 |
| `PAPER_VIDEO_API_BASE` | 可选 OpenAI 兼容接口根地址，需要支持图像和 JSON Schema |
| `PAPER_VIDEO_API_KEY` | 仅从进程环境读取的接口密钥 |

使用其他模型服务时同时配置 BASE、KEY、MODEL，所选论文上下文和页面图片会发送至该地址；PDF 与视频合成留在本机，配音依所选本地/在线模式处理，不需要额外视频生成服务

## 验证

```bash
~/.local/share/paper-segment-video/venv/bin/python -m pytest -q
~/.local/share/paper-segment-video/venv/bin/python -m compileall -q paper_video
node --check web/app.js
```

[调研和教学规范](docs/method-and-research.md) · [验收记录](docs/validation.md)

适用范围是单用户小批量验证，不是多人线上版本；动态批注、合成语音与神经语音词级对齐已实现，数字人未实现，模型校验不能保证任意论文的解释绝对正确

## 主会话直接粘贴原文

在启用插件的 Codex 会话发送「把下面这段做成讲解视频：」并粘贴原文，助手会直接创建任务，在当前会话右侧打开进度面板，完成后原位显示视频；不需要再去网页粘贴一次

```bash
python3 scripts/studio.py start
python3 scripts/studio.py paste --text-file '/absolute/path/paragraph.txt'
```

返回 `panel_url`，形如 `http://127.0.0.1:8766/?panel=1&job=<任务ID>`，原文通过本地 POST 提交，不出现在 URL 中。唯一匹配导入论文时保留全文相关上下文，否则按独立片段解读并提示信息不足

## 配音与语速

本机当前使用云扬沉稳中文神经男声 `zh-CN-YunyangNeural`，通过 Edge 在线语音服务发送讲解稿，PDF 和画面不发送给该语音服务；配音明确标为 AI 合成讲解，不能保证完全听不出合成痕迹

在线模式由语音引擎直接采用 `+0%` 语速，避免再次后处理加速；词级时间戳用于流程步骤和鼠标定位，网络失败会明确报错并允许重试，不会悄悄换回系统女声

配置保存在 `~/.local/share/paper-segment-video/voice/config.json`，本机内容为 `{"provider":"edge-neural","voice":"zh-CN-YunyangNeural"}`；配置不随插件分发，新环境无配置时仍使用本地系统语音，在线模式需明确选择，已支持云希、云扬、云健三种中文男声

所有新任务使用 1 倍语速，保留原有音色与语气处理；旧任务保留原来的语速快照和视频文件

本地参考音色适配器仍保留，但权重下载曾遭遇 TLS 中断与超时，未完成真实推理验收；本次男声是通用神经音色，不是李沐声音复刻

## 流程图与鼠标引导

执行链路优先动态流程图，静态关系使用静态图；节点采用克制的蓝灰配色、短名称、同层对齐，箭头标注数据或动作，返回路径从侧边单独走线，条件路径用虚线

每步显示参与节点、输入、输出、条件和证据边界，动画播放到该步时才突出相关节点与数据流；鼠标先指原文证据，再随实际旁白锚点移动到步骤或重点内容

右侧面板新增“流程步骤回看”，点击可定位并暂停视频，从而逐步检查完整机制；视频保留原有播放、暂停、章节和下载功能

图示标准与语音调研见 [本次改动说明](docs/flow-voice-20260918.md)
