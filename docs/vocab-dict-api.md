# 词典 / 生词本 / 覆盖率：实现契约（v1）

本文是后端（Lead 维护）与前端（web/）之间的唯一接口约定。字段名、层级、类型以此为准；
前端可以自行设计视觉和交互，但**不要改名、不要假设额外字段**。后端实现完成后本文档保持同步。

范围：

1. 本地词典（ECDICT 裁剪库）：状态、下载并构建、查词（含词形还原、考试 tag）、AI 语境详解
2. 生词本：收录 / 状态 / 列表筛选 / 出现位置 / 复习评分（FSRS 简化版）/ 导出
3. 视频词汇覆盖率 + 未掌握词表
4. 字幕里点词弹层、生词下划线标记
5. 设置页「词典」分组

限制：前端仍是原生 HTML/CSS/JS，无构建步骤、无第三方库；所有服务端/模型返回的字符串必须经
`esc()` 转义后再进 innerHTML；文案中文；键盘操作要能直达（复习卡 1–4 评分、Esc 关闭弹层）。

---

## 1. 词典数据库（构建产物）

位置：`data/dict/ecdic.db`（可在设置 `dict.db_path` 覆盖）。由 ECDICT 的 `ecdict.csv`
（76 万词条，MIT）裁剪而来；裁剪后预计 10–20 MB。**不进 git，运行时下载/构建。**

```sql
CREATE TABLE dict (
    word        TEXT PRIMARY KEY,   -- 小写词条，如 walk
    phonetic    TEXT DEFAULT '',
    translation TEXT DEFAULT '',    -- 中文释义，多行用 \n
    definition  TEXT DEFAULT '',    -- 英文释义，多行用 \n（可为空）
    pos         TEXT DEFAULT '',    -- 词性比例，如 n:46/v:54
    collins     INTEGER DEFAULT 0,  -- 柯林斯星级 0-5
    oxford      INTEGER DEFAULT 0,  -- 牛津 3000 核心词 0/1
    tag         TEXT DEFAULT '',    -- 空格分隔：zk gk cet4 cet6 ky toefl ielts gre
    bnc         INTEGER DEFAULT 0,  -- BNC 词频排名，0 = 未收录
    frq         INTEGER DEFAULT 0,  -- 当代语料词频排名，0 = 未收录
    forms       TEXT DEFAULT ''     -- 空格分隔的变形（不含原形），如 walked walking walks
);
CREATE TABLE form (                 -- 变形 → 原形
    form TEXT PRIMARY KEY,
    word TEXT NOT NULL
);
CREATE TABLE meta (                 -- entries / forms / built_at / source / max_rank / schema_version
    key TEXT PRIMARY KEY, value TEXT
);
CREATE INDEX idx_dict_bnc ON dict(bnc);
CREATE INDEX idx_dict_frq ON dict(frq);
```

构建逻辑放在 `app/dictbuild.py`，CLI 包装为 `tools/build_dict.py`：

```python
build(csv_path: Path, out_path: Path, *, lemma_path: Path | None = None,
      max_rank: int = 60000, progress: Callable[[int, int], None] | None = None) -> dict
# 返回 {"entries": int, "forms": int, "size_bytes": int, "built_at": str}
```

裁剪规则：`bnc<=max_rank` 或 `frq<=max_rank` 或 `tag` 非空 或 `oxford=1` 或 `collins>0`；
`form` 里只保留其原形也在 `dict` 里的映射。词形映射优先从 `exchange` 字段（`0:` 与
`p/d/i/3/r/t/s:`）生成，`lemma.en.txt` 作为补充。`meta.max_rank` 记录裁剪档位。

## 2. 配置与设置页

`config.DEFAULTS` 新增：

```toml
[dict]
db_path = ""        # 留空 = data/dict/ecdic.db
source_url = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv"
lemma_url = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/lemma.en.txt"
max_rank = 60000
```

设置页 `FIELDS` 新增分组「词典」，字段（前端按 group 顺序渲染，并在该分组顶部插入词典状态卡）：

| path | label | type | 说明 |
|---|---|---|---|
| `dict.db_path` | 词典文件 | path | 留空 = data/dict/ecdic.db |
| `dict.source_url` | 词库源地址 | text | ECDICT csv 下载地址，可换镜像 |
| `dict.max_rank` | 词频裁剪档位 | int (min 5000, max 60000) | 越大词条越多、库越大；60000 档实测 59,137 条 / 14.1 MB，≥50000 后档位基本饱和 |

`GET /api/settings` 的响应新增 `"dict"`，内容与 `GET /api/dict/status` 完全一致。

## 3. HTTP API

### 3.1 词典

`GET /api/dict/status`

```json
{
  "installed": true,
  "path": "/…/data/dict/ecdic.db",
  "size_bytes": 14757888,
  "entries": 59137,
  "forms": 50429,
  "built_at": "2026-11-04T12:30:00",
  "downloading": false,
  "phase": "idle",
  "progress": 100,
  "message": "",
  "error": "",
  "source_url": "https://raw.githubusercontent.com/…/ecdict.csv",
  "max_rank": 60000
}
```

`phase`: `idle | downloading | building | done | error`；`progress` 0–100（整体）。
`downloading=true` 时前端每 1s 轮询本接口刷新进度。

`POST /api/dict/install`（body 可省，`{"force": true}` 覆盖重建）→ `{"started": true}`
正在下载/构建时返回 409。后台线程执行：下载 csv + lemma → 构建 → 替换数据库。

`DELETE /api/dict` → `{"ok": true}`（删库文件；正在构建时 409）

`GET /api/dict/lookup?q=walked&pk=3&seq=12`（`pk`/`seq` 可选，用于回填上下文与生词状态）

```json
{
  "found": true,
  "query": "walked",
  "word": "walk",
  "lemma": "walk",
  "phonetic": "wɔːk",
  "pos": "v:74/n:26",
  "tags": [{"code": "cet4", "label": "四级"}, {"code": "zk", "label": "中考"}],
  "translations": ["走", "步行", "遛"],
  "definition": ["to move along by putting one foot in front of the other"],
  "collins": 5,
  "oxford": 1,
  "bnc": 1180,
  "frq": 980,
  "forms": ["walks", "walked", "walking"],
  "known_rank": 0,
  "status": "learning",
  "vocab_id": 7,
  "source": "lemma"
}
```

`found=false` 时只返回 `{"found": false, "query": "...", "source": "none"}`，前端提示「未收录，
可用 AI 详解」。tag code 与中文标签的映射在前端做（后端同时给 `label`，以后端为准更稳）。

`POST /api/dict/explain`

```json
{"word": "walk", "lemma": "walk", "sentence": "I walked into the room.", "pk": 3, "seq": 12}
→ {"ok": true, "cached": false, "model": "deepseek-chat",
   "explanation": {"meaning": "在句中的意思：走进", "usage": "……", 
                   "collocations": [{"phrase": "walk into", "meaning": "走进（意外进入）"}],
                   "grammar": "……", "examples": [{"en": "…", "zh": "…"}], "note": "……"}}
```

未配置大模型返回 400（detail 指向设置页）；LLM 失败返回 502（detail 为中文原因）。
缓存键 `(word, sentence)`，重复点击返回 `cached: true`。

### 3.2 生词本

`GET /api/vocab?status=all|learning|known|ignored|due&q=&tag=&limit=50&offset=0&sort=recent|due|alpha`

```json
{
  "items": [{
    "id": 7, "word": "walked", "lemma": "walk", "phonetic": "wɔːk", "pos": "v:74/n:26",
    "tags": [{"code": "cet4", "label": "四级"}], "translation": "走；步行",
    "status": "learning", "note": "",
    "source_pk": 3, "source_seq": 12, "source_title": "视频标题",
    "source_sentence": "I walked into the room.", "source_start_ms": 12000,
    "created_at": "2026-11-04 12:00:00",
    "due_at": 1762400000000, "due_in_days": 2, "reps": 3, "lapses": 0,
    "stability": 3.4, "difficulty": 5.1, "last_review": 1762300000000
  }],
  "total": 42,
  "stats": {"all": 42, "learning": 5, "known": 30, "ignored": 7,
            "due_now": 3, "due_today": 5, "reviews_today": 9, "streak_days": 4}
}
```

`due_at`/`last_review` 是**毫秒时间戳**（与前端 Date 直接可用）。`due` 筛选 = `status='learning'
AND due_at<=now`。

`POST /api/vocab`

```json
{"word": "walked", "lemma": "walk", "phonetic": "wɔːk", "pos": "v:74/n:26",
 "tags": [{"code": "cet4", "label": "四级"}], "translation": "走；步行", "definition": "",
 "status": "learning", "note": "",
 "source": {"pk": 3, "seq": 12, "sentence": "I walked into the room.", "start_ms": 12000}}
→ {"ok": true, "created": true, "item": { …同上方 item… }}
```

按 `word` 去重（已存在则更新来源/释义，`created=false`）。

`PATCH /api/vocab/{id}`：`{"status": "known", "note": "…"}`（status 允许 learning/known/ignored）
→ `{"ok": true, "item": {…}}`。标记 known 后该词不再进复习队列、字幕不再打点。

`DELETE /api/vocab/{id}` → `{"ok": true}`

`GET /api/vocab/{id}/occurrences` → `{"items": [{"pk": 3, "title": "…", "seq": 12,
"text": "I walked into the room.", "start_ms": 12000}], "truncated": false}`（最多 30 条，
跨全部视频按词边界匹配 word/lemma）。

`POST /api/vocab/{id}/review`：`{"grade": 1|2|3|4}`（1=忘了，2=勉强，3=记得，4=简单）

```json
{"ok": true, "item": {…更新后的 item…},
 "scheduled_days": 3, "interval_days": 3, "due_at": 1762600000000,
 "stability": 6.2, "difficulty": 4.8, "reps": 4}
```

`GET /api/vocab/known`（字幕打点用）→ `{"learning": ["walked"], "known": ["walk", "…"], "ignored": []}`
（`learning` 里放 word 与 lemma 两种形态，前端按小写精确匹配。）

`GET /api/vocab/export?format=tsv|csv|json` → 文件下载（`Content-Disposition: attachment`）。
列：word, lemma, phonetic, tags, translation, status, note, due_at, reps, lapses, created_at。
TSV 供 Anki 直接导入（`#separator:tab`、`#html:false` 头两行）。

### 3.3 覆盖率

`GET /api/videos/{pk}/coverage`

```json
{
  "dict_ready": true,
  "tokens": 1834, "unique": 612,
  "known_tokens": 1712, "known_unique": 561,
  "coverage": 0.9335,
  "baseline_rank": 2000,
  "target": 0.98,
  "unknown": [{"word": "serendipity", "count": 2, "lemma": "serendipity",
               "rank": 0, "in_vocab": false, "status": null}],
  "unknown_tokens": 122
}
```

统计算法：字幕按词切分并小写，去纯数字/单字母（a/i 除外）；已知 = 词频排名 ≤ baseline_rank
或 `vocab.status='known'`。`unknown` 按出现次数降序最多 120 条，`rank=0` 表示词典未收录。

## 4. 前端行为约定

### 4.1 点词查词弹层

- 播放器字幕里**单击单词** → 弹出查词卡；单击行内非词区域仍跳转该行（保留原有 seek 行为）。
- 查词卡内容：单词 + 音标 + 发音按钮 + 考试 tag chips + 中文释义 + 「收藏生词」/「标为已掌握」+
  「AI 详解」（点开后才请求 `/api/dict/explain`）。
- 弹层定位在词附近（`position: fixed`，窗口内夹紧，窄屏改为底部 sheet）；`Esc`、点遮罩、切行关闭。
- 查词请求失败或未安装词典：显示「本地词典未安装，去设置页下载」+ 跳设置按钮；不能阻塞播放。
- 已收藏词显示状态（学习中/已掌握）；收藏后字幕里的该词立即打点。

### 4.2 字幕生词标记

- 加载视频后拉一次 `/api/vocab/known`：`learning` 里的词加 `.saved`（虚线下划线、accent 色），
  `known`/`ignored` 不加。仅匹配 `[a-z']+` 的小写词形，不改动字幕文本本身。
- 标记必须可随播放/切行保持，且不影响逐词高亮 `.on`。

### 4.3 生词本视图（新增第 4 个视图，导航「生词本」）

- 顶部：统计（总词数 / 学习中 / 待复习 / 今日已复习 / 连续天数）+「开始复习」按钮。
- 列表：状态 tab（全部/学习中/已掌握/已忽略）+ 搜索框 + tag 过滤 + 排序；每行：单词、音标、
  中文释义、tag chips、来源视频标题、到期天数；行内操作：标已掌握 / 删除 / 查看出现句。
- 「开始复习」：卡片式，队列 = `status=due`（没有 due 时可复习学习中全部），卡片显示
  来源句（挖空目标词）+ 单词 + 音标；空格或点击显示答案 + 释义；然后 1–4 评分按钮
  （忘了/勉强/记得/简单）调 `/api/vocab/{id}/review`，保存后下一张，显示剩余数量。
- 详情：出现位置列表（点击跳转到对应视频该行；需要 `openVideo` + `seekLine` 能力）。
- 导出：三个按钮（Anki TSV / CSV / JSON），直接 `window.location = url`。

### 4.4 设置页「词典」分组

- 分组顶部状态卡：已安装 → 「词条 128,340 · 12.3 MB · 构建于 …」；未安装 → 说明 + 「下载并构建」
  按钮；下载/构建中 → 进度条 + 阶段文案（下载中/构建中），每 1s 轮询 `/api/dict/status`；
  失败 → 错误信息 + 「重试」；另有「重建」「删除」按钮（删除要二次确认）。
- 下面三个配置字段照常渲染（来自 FIELDS）。

### 4.5 健康检查/导航

- 顶栏或设置页显示词典是否就绪（`/api/health` 新增 `dict: {installed: bool, entries: int}`）。
- 生词本导航项带待复习数字徽标（来自 `GET /api/vocab?status=due&limit=1` 的 `stats.due_now`，
  避免每次全量拉取；或复用 `/api/vocab` 的 stats）。

## 5. 分工与文件边界

| 文件 | 归属 |
|---|---|
| `docs/vocab-dict-api.md`（本文） | Lead |
| `app/dict.py` `app/srs.py` `app/db.py` `app/api.py` `app/settings.py` `app/config.py` `app/dictbuild.py` | Lead（dictbuild 与 tools 由 dictdata 实现，见任务） |
| `tools/build_dict.py` `app/dictbuild.py` `data/dict/`（构建产物） | dictdata |
| `web/index.html` `web/app.js` `web/style.css` | frontend |

前端只改 `web/*`；不要动后端文件。后端只改 `app/*` 与本文件。
