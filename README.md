# VideoEnglishLearner（`vel`）

一个跑在自己电脑上的英语学习播放器：贴一个 YouTube 链接，它把视频和英文字幕拉到本地，
切成适合精读的字幕行，配一个同步滚动、点一下就跳转的播放器；配上大模型后，还能逐句给你
中文翻译、重点语法和短语讲解。

用来做 vlog / 播客 / 访谈的精听和精读，不需要把视频传到任何云端。

![库视图](docs/screenshots/library.png)

![播放页 + 解析面板](docs/screenshots/player-analysis.png)

## 它做什么

- **贴链接就行**：YouTube 视频 + 英文字幕一次拿到本地；也可以「只取字幕」或「只取视频」，分开下载
- **跟得上的播放器**：字幕同步滚动、点字幕跳到对应时间、逐词高亮、AB 复读、0.75 倍速、断点续看
- **精听三档**：双语 / 遮住英文听写 / 只看英文，点一下就揭示
- **逐句解析**：中文翻译 + 重点语法（点到为止，只挑真有用的）+ 短语和语气说明，结果按句缓存
- **处理进度看得见**：卡片上有进度条，处理中顶部有任务条
- **没有字幕也能用**：视频确实没字幕时，用本地 faster-whisper 转写（CPU 能跑，有 N 卡更快）
- **导出**：下载视频、下载字幕（SRT）
- **所有设置都在网页里**：ffmpeg 路径、代理、字幕来源、切分参数、大模型，都有表单和「检测」按钮

## 技术（简述）

- **后端**：Python 3.11+ / FastAPI + Uvicorn，SQLite 存库；任务用进程内单个 worker 串行跑，
  个人使用够用，不需要 Redis / Celery
- **下载**：yt-dlp + ffmpeg
- **字幕**：优先 YouTube 官方 / 自动字幕（自带词级时间戳）；视频确实没有字幕时才用
  faster-whisper（CTranslate2）本地转写
- **切行**：词级时间戳 + 动态规划，按行长、时长、标点、行首虚词打分取最优切法；纯规则，不调模型
  （以后接 LLM 只换代价函数）
- **前端**：单页原生 HTML/CSS/JS，没有构建步骤；播放用原生 `<video>`，后端支持 HTTP Range，所以 seek 很快
- **大模型**：OpenAI 兼容接口（标准库 urllib 实现），DeepSeek、本地 Ollama、任何兼容端点都能用；
  按批请求、按句缓存，重复解析不重复花钱

## 怎么运行

### 需要什么

| 需要 | 说明 |
| --- | --- |
| Python 3.11+ | 3.14 也验证过 |
| ffmpeg | **硬依赖**。YouTube 现在只提供分离的音视频流，没它合不出能播的文件 |
| 代理（国内） | 访问 YouTube 要用，填在 `config.toml` 或网页「设置 → 网络」里 |

### 跑起来

```bat
git clone https://github.com/JasonLe/VideoEnglishLearner.git
cd VideoEnglishLearner
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

先填代理（也可以直接跳到网页「设置」里填，效果一样）。把示例配置复制一份再改：

```bat
copy config.example.toml config.toml
```

```toml
# config.toml
[network]
proxy = "http://127.0.0.1:7890"
```

`config.toml` 里会有你的代理地址和 API Key，所以它不进 Git（已在 `.gitignore` 里）。

然后启动：

```bat
run.bat
```

或者手动 `.venv\Scripts\python -m app.cli serve`，打开 <http://127.0.0.1:8787>。

ffmpeg 如果不在 PATH 里，第一次打开网页会看到顶部提示，点「设置 → 下载」填 `ffmpeg.exe`
的路径，点「检测」确认能用。

### 可选：本地转写

视频真的没有字幕时会走这条路。

```bat
:: CPU（约 150MB 依赖，够用）
.venv\Scripts\python -m pip install -r requirements-asr.txt

:: NVIDIA 显卡（另外要装 CUDA 12 运行库的 wheel，见 requirements-asr-gpu.txt）
.venv\Scripts\python -m pip install -r requirements-asr-gpu.txt
```

模型默认缓存在 `data/models`，国内建议先设镜像：`set HF_ENDPOINT=https://hf-mirror.com`。

### 可选：接大模型做解析

网页「设置 → 解析」里填：

- DeepSeek：`https://api.deepseek.com/v1` + 你的 API Key + `deepseek-chat`
- 本地 Ollama：`http://localhost:11434/v1` + 任意 Key（本地不校验）+ 模型名，比如 `qwen2.5:7b`

填完点「检测」确认通，再回播放页点「解析」。不填也能用——只是没有翻译和语法讲解。

## 怎么用

1. 把 YouTube 链接粘进顶部输入框，选模式（视频 + 字幕 / 只取字幕 / 只取视频），点「添加」
2. 等处理完：卡片上有进度条，处理中顶部会显示当前任务
3. 点卡片进播放器，点字幕跳转、点词看高亮
4. 想精听就切「精听」下拉框到「遮住英文（听写）」，卡住时点那一行揭示
5. 想看懂就点右上「解析」（需要先配好大模型），右侧面板按句给译文 / 语法 / 短语
6. 卡片和播放页都有「下载视频」「下载字幕」

### 快捷键

| 键 | 作用 |
| --- | --- |
| `空格` | 播放 / 暂停 |
| `←` `→` | 后退 / 前进 3 秒 |
| `↑` `↓` | 上一句 / 下一句 |
| `Enter` | 复读当前句 |
| `A` / `B` | 设 AB 复读点 |
| `R` | 循环当前选区 / 清除 |
| `S` | 1.0 ↔ 0.75 倍速 |
| `N` | 切换精听三档 |

## 常见问题

- **顶部提示「ffmpeg 不可用」/ 下载后没声音**：ffmpeg 没装或路径没配，去设置页填路径后点「检测」
- **字幕下载报 429 / Too Many Requests**：YouTube 限流，等 2–5 分钟点「补字幕」就行；
  或者用浏览器插件导出 `cookies.txt`，填进设置页「网络 → cookies 文件」
- **解析报 401 / 402 / 404 / 429**：分别是 Key 不对、余额不足、base_url 或模型名不对、被限流
- **有 N 卡但转写很慢**：日志里如果出现「显卡缺 CUDA 运行库，改用 CPU」，装
  `requirements-asr-gpu.txt` 里的依赖即可
- **数据在哪**：全在 `data/` 下（`vel.db` 数据库、`media/` 视频、`*.srt` 字幕、`models/` 模型缓存）；
  想搬到别的机器，把整个 `data/` 复制过去就行

## 合规

自己学习用：只存本地、不公开分发、不批量搬运。下载功能请遵守 YouTube 服务条款和当地版权规定。
