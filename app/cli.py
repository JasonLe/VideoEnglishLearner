"""命令行入口。

    python -m app.cli ingest <url>   跑完整条链路（同步，看得到进度）
    python -m app.cli serve          起 Web 服务
    python -m app.cli selftest       跑一遍纯本地自检（不联网、不下载）
    python -m app.cli list           看库里有什么
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sys
import traceback
from pathlib import Path

from . import APP_NAME, APP_SLUG


def _progress(frac: float, detail: str) -> None:
    sys.stdout.write(f"\r[{frac * 100:5.1f}%] {detail[:64]:<64}")
    sys.stdout.flush()


def cmd_ingest(args: argparse.Namespace) -> int:
    from . import db, pipeline

    db.init_db()
    print(f"→ {args.url}")
    try:
        pk = pipeline.ingest(args.url, on_progress=_progress)
        print()
        result = pipeline.run(
            pk,
            args.url,
            on_progress=_progress,
            skip_media=args.subs_only,
            skip_subs=getattr(args, "video_only", False),
        )
        print()
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ 失败：{exc}")
        if args.verbose:
            traceback.print_exc()
        return 1

    print(f"✓ video_pk={pk}  行数={result['segments']}  词数={result['words']}  来源={result['source']}")
    print(f"  视频: {result['media']}")
    print(f"  字幕: {result['srt']}")
    return 0


def cmd_subs(args: argparse.Namespace) -> int:
    """只补字幕：视频已在磁盘上，不重复下载（429 被限流之后最快的修法）。"""
    from . import db, pipeline

    db.init_db()
    video = db.get_video(args.pk)
    if not video:
        print(f"✗ 没有 id={args.pk} 的视频")
        return 1
    print(f"→ 补字幕：{video['title'][:60]}")
    try:
        result = pipeline.refetch_subs(args.pk, video["url"], on_progress=_progress)
        print()
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ 失败：{exc}")
        if args.verbose:
            traceback.print_exc()
        return 1
    print(f"✓ 行数={result['segments']}  词数={result['words']}  来源={result['source']}")
    print(f"  字幕: {result['srt']}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .main import main

    main()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from . import db

    db.init_db()
    rows = db.list_videos()
    if not rows:
        print("(空)")
        return 0
    for r in rows:
        print(f"{r['id']:>3}  {r['status']:<12} {r['source']:<15} {r['title'][:56]}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    from . import analyze, db

    db.init_db()
    if not db.get_video(args.pk):
        print(f"✗ 没有 pk={args.pk} 这个视频")
        return 1
    try:
        result = analyze.run(
            args.pk,
            on_progress=lambda done, total: _progress(
                done / total if total else 0, f"解析中 {done}/{total}"
            ),
            force=args.force,
        )
        print()
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ 失败：{exc}")
        if args.verbose:
            traceback.print_exc()
        return 1
    print(
        f"✓ 解析 {result['analyzed']} 行（缓存命中 {result['cached']}）  模型={result['model']}"
    )
    return 0


@contextlib.contextmanager
def _scratch_dir():
    """自检用的临时目录。

    不走 tempfile.mkdtemp：某些 Windows 沙箱下它建出来的目录连创建者都写不进去。
    """
    from .config import DATA_DIR

    path = DATA_DIR / "selftest-tmp"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------- selftest

def _selftest() -> int:
    from . import db
    from .models import Segment, Word
    from .segment import BAD_LINE_START, build_segments, distribute_words, dp_split
    from .subtitles import parse_json3, parse_vtt
    from .text import join_words, visual_len

    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        if cond:
            print(f"  ✓ {name}")
        else:
            failures.append(name)
            print(f"  ✗ {name} {extra}")

    def cfg_get(dotted: str, default=None):
        return {
            "segment.max_chars": 42,
            "segment.target_chars": 32,
            "segment.max_ms": 7000,
            "segment.min_ms": 900,
        }.get(dotted, default)

    print("[1] text.visual_len")
    check("ascii=len", abs(visual_len("hello") - 5.0) < 1e-6)
    check("cjk 宽 1.75", abs(visual_len("你好") - 3.5) < 1e-6)

    print("[2] text.join_words")
    check("拼空格", join_words([Word("hello", 0, 1), Word("world", 1, 2)]) == "hello world")
    check(
        "标点前不留空格",
        join_words([Word("hello", 0, 1), Word(",", 1, 2), Word("world", 2, 3)]) == "hello, world",
    )

    print("[3] segment.distribute_words")
    words = distribute_words(1000, 2000, "one two three")
    check("三个词", len(words) == 3, str(len(words)))
    check("覆盖到末尾", words[-1].end_ms == 2000, str(words[-1].end_ms))
    check("时间单调", all(words[i].start_ms <= words[i + 1].start_ms for i in range(len(words) - 1)))

    print("[4] segment.dp_split 行长与覆盖率")
    sentence = (
        "I went down to the coffee shop this morning and ordered a latte "
        "because I had a really rough night and I badly needed something to wake me up"
    ).split()
    ws = [Word(t, i * 320, i * 320 + 300) for i, t in enumerate(sentence)]
    lines = dp_split(ws, max_chars=42, target_chars=32, max_ms=7000, min_ms=900)
    flat = [w for line in lines for w in line]
    check("不丢词", [w.text for w in flat] == [w.text for w in ws], f"{len(flat)}/{len(ws)}")
    check("无空行", all(line for line in lines))
    over = [line for line in lines if visual_len(join_words([w.text for w in line])) > 42 and len(line) > 1]
    check("边长受控", not over, f"{len(over)} 行超长")
    check("不是每词一行", len(lines) < len(ws) / 2, f"{len(lines)} 行 / {len(ws)} 词")
    bad_starts = [line for line in lines if line[0].text.lower() in BAD_LINE_START]
    check("行首别扭词较少", len(bad_starts) <= max(1, len(lines) // 4), f"{len(bad_starts)}/{len(lines)}")

    print("[5] segment 句子边界")
    two = [Word("Hello.", 0, 200), Word("This", 300, 500), Word("is", 500, 600), Word("a", 600, 700), Word("test.", 700, 900)]
    segs = build_segments(two, cfg_get)
    check("两句分成两行", len(segs) == 2, str(len(segs)))
    check("seq 连续", [s.seq for s in segs] == list(range(len(segs))))
    check("起点有序", all(segs[i].start_ms <= segs[i + 1].start_ms for i in range(len(segs) - 1)))

    print("[6] subtitles.parse_json3")
    with _scratch_dir() as tmp:
        j3 = Path(tmp) / "a.en.json3"
        j3.write_text(
            '{"events":[{"tStartMs":0,"segs":[{"utf8":"hello ","tOffsetMs":0},{"utf8":"world","tOffsetMs":400}]},'
            '{"tStartMs":0,"segs":[{"utf8":"hello ","tOffsetMs":0}]}]}',
            encoding="utf-8",
        )
        jwords = parse_json3(j3)
        check("json3 词数（去重滚动行）", len(jwords) == 2, str(len(jwords)))
        check("json3 首词时间", jwords[0].start_ms == 0, str(jwords[0].start_ms))
        check("json3 次词偏移", jwords[1].start_ms == 400, str(jwords[1].start_ms))
        check("json3 结束时间有效", jwords[1].end_ms > jwords[1].start_ms)

        vtt = Path(tmp) / "a.en.vtt"
        vtt.write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:03.000 align:start\nhello there\n\n"
            "00:00:03.000 --> 00:00:05.000\nhello there\n\n"
            "00:00:05.000 --> 00:00:07.000\n<i>again</i>\n",
            encoding="utf-8",
        )
        vwords = parse_vtt(vtt)
        check("vtt 去掉重复行", len(vwords) == 3, str(len(vwords)))
        check("vtt 剥标签", all("<" not in w.text for w in vwords))

        print("[7] db 读写")
        original = db.DB_FILE
        db.DB_FILE = Path(tmp) / "t.db"
        try:
            db.init_db()
            pk = db.create_video("https://youtu.be/x", "x", "标题", "频道", 60000, "")
            check("插入视频", pk > 0)
            check("UPSERT 不重复", db.create_video("https://youtu.be/x", "x", "标题2") == pk)
            n = db.replace_segments(pk, segs)
            check("写入行", n == len(segs))
            got = db.get_segments(pk)
            check("读回行", len(got) == len(segs))
            check("读回词", len(got[0]["words"]) == len(segs[0].words))
            db.set_playback(pk, 12345, 0.75)
            pb = db.get_playback(pk)
            check("断点续看", pb["position_ms"] == 12345 and abs(pb["rate"] - 0.75) < 1e-6)
            db.set_playback(pk, 9999)
            check("续看不改倍速", abs(db.get_playback(pk)["rate"] - 0.75) < 1e-6)
            db.set_status(pk, "downloading")
            check("中断恢复", db.recover_interrupted() == 1)
            check("恢复后状态", db.get_video(pk)["status"] == "failed")
            db.delete_video(pk)
            check("级联删除", db.get_video(pk) is None and db.get_segments(pk) == [])
        finally:
            db.DB_FILE = original

    print("[8] settings 校验与 toml 回写")
    import tomllib

    from . import config as config_mod
    from . import settings as settings_mod

    def rejected(values: dict) -> bool:
        try:
            staged = {
                path: settings_mod._coerce(settings_mod._BY_PATH[path], raw)
                for path, raw in values.items()
            }
            settings_mod._cross_check(staged)
            return False
        except settings_mod.SettingsError:
            return True

    check("拒绝越界整数", rejected({"segment.max_chars": 5}))
    check("拒绝非枚举值", rejected({"asr.device": "tpu"}))
    check("拒绝过长路径", rejected({"network.proxy": "x" * 800}))
    check("拒绝目标行长 > 上限", rejected({"segment.target_chars": 40, "segment.max_chars": 16}))
    check("拒绝最短 >= 最长", rejected({"segment.min_ms": 9000, "segment.max_ms": 8000}))
    check("接受合法组合", not rejected({"segment.max_chars": 38, "segment.target_chars": 30}))
    check(
        "整数可从字符串来",
        settings_mod._coerce(settings_mod._BY_PATH["segment.max_chars"], "36") == 36,
    )
    check(
        "文本去首尾空格",
        settings_mod._coerce(settings_mod._BY_PATH["network.proxy"], "  http://x  ")
        == "http://x",
    )

    rendered = config_mod.render_toml(config_mod.DEFAULTS)
    with _scratch_dir() as tmp2:
        cfg_path = Path(tmp2) / "cfg.toml"
        cfg_path.write_text(rendered, encoding="utf-8")
        with cfg_path.open("rb") as fh:
            parsed = tomllib.load(fh)
    check(
        "toml 回写可解析",
        parsed.get("download", {}).get("ffmpeg_location") == "",
        str(parsed.get("download")),
    )
    check(
        "toml 保留全部段",
        set(parsed) >= {"server", "network", "download", "asr", "segment"},
        str(sorted(parsed)),
    )
    check("toml 带注释", "# " in rendered)

    print("[9] 进度 + 解析")
    from . import analyze as analyze_mod
    from . import llm as llm_mod

    check(
        "endpoint 拼接",
        llm_mod._endpoint("https://api.deepseek.com/v1")
        == "https://api.deepseek.com/v1/chat/completions",
    )
    check(
        "endpoint 不重复拼",
        llm_mod._endpoint("https://x/v1/chat/completions/") == "https://x/v1/chat/completions",
    )
    check(
        "本地地址识别",
        llm_mod.is_local("http://localhost:11434/v1")
        and not llm_mod.is_local("https://api.deepseek.com/v1"),
    )
    check("剥掉代码围栏", llm_mod._strip_fence('```json\n{"a":1}\n```').strip() == '{"a":1}')

    cleaned = analyze_mod.clean_payload(
        {
            "translation": "  我来了。 ",
            "grammar": [{"point": "a", "explain": "b"}] * 4,
            "phrases": [{"term": "t", "meaning": "m"}] * 5,
            "note": "n",
        },
        7,
    )
    check(
        "payload 清洗",
        cleaned is not None
        and cleaned["translation"] == "我来了。"
        and len(cleaned["grammar"]) == 2
        and len(cleaned["phrases"]) == 3,
    )
    check("空翻译丢弃", analyze_mod.clean_payload({"grammar": []}, 1) is None)

    with _scratch_dir() as tmp3:
        saved_db = db.DB_FILE
        db.DB_FILE = Path(tmp3) / "a.db"
        try:
            db.init_db()
            pk2 = db.create_video("https://youtu.be/y", "y", "解析测试", "ch", 30000, "")
            segs2 = build_segments(
                [
                    Word("Hey", 0, 200),
                    Word("there.", 200, 500),
                    Word("How", 600, 800),
                    Word("are", 800, 900),
                    Word("you?", 900, 1100),
                ],
                cfg_get,
            )
            db.replace_segments(pk2, segs2)
            db.set_status(pk2, "ready", progress=100)

            db.set_progress(pk2, 0.4567)
            check("进度取整", db.get_video(pk2)["progress"] == 46, str(db.get_video(pk2)["progress"]))
            db.set_progress(pk2, 3.0)
            check("进度夹紧", db.get_video(pk2)["progress"] == 100)
            check("进度不改状态", db.get_video(pk2)["status"] == "ready")

            rows = db.get_segments(pk2)
            aligned = analyze_mod._collect(
                {"items": [{"seq": r["seq"], "translation": "x"} for r in rows]}, rows
            )
            check("按 seq 对齐", len(aligned) == len(rows))
            try:
                analyze_mod._collect({"items": []}, rows)
                check("漏行报错", False)
            except llm_mod.LlmError:
                check("漏行报错", True)

            calls = {"n": 0}
            original_chat = analyze_mod.llm.json_chat

            def fake_chat(messages, **_kwargs):
                calls["n"] += 1
                prompt = messages[-1]["content"]
                seqs = [
                    int(line.split(".", 1)[0])
                    for line in prompt.splitlines()
                    if line[:1].isdigit()
                ]
                return {
                    "items": [
                        {"seq": s, "translation": f"译文{s}", "grammar": [], "phrases": [], "note": ""}
                        for s in seqs
                    ]
                }

            analyze_mod.llm.json_chat = fake_chat
            try:
                first = analyze_mod.run(pk2)
                check(
                    "解析落库",
                    first["analyzed"] == len(rows) and db.count_analysis(pk2) == len(rows),
                    f"{first['analyzed']}/{len(rows)}",
                )
                check("解析状态 done", db.get_video(pk2)["analysis_status"] == "done")
                check("解析进度记满", db.get_video(pk2)["analysis_done"] == len(rows))
                second = analyze_mod.run(pk2)
                check(
                    "二次跑走缓存",
                    second["analyzed"] == 0 and calls["n"] == 1,
                    f"analyzed={second['analyzed']} calls={calls['n']}",
                )
                analyze_mod.run(pk2, force=True)
                check("force 重跑", calls["n"] >= 2)
                got = db.get_analysis(pk2)
                check("译文可读回", bool(got) and all(v["translation"].startswith("译文") for v in got.values()))
            finally:
                analyze_mod.llm.json_chat = original_chat

            db.clear_analysis(pk2)
            check(
                "清空解析",
                db.count_analysis(pk2) == 0 and db.get_video(pk2)["analysis_status"] == "none",
            )
        finally:
            db.DB_FILE = saved_db

    print("[10] 下载容错（字幕被限流不该整条失败）")
    from . import download as dl

    check(
        "429 文案可操作",
        "429" in str(dl._friendly_error(Exception("HTTP Error 429: Too Many Requests"))),
    )
    check(
        "识别字幕错误",
        dl._is_subtitle_error(
            Exception("ERROR: Unable to download video subtitles for 'en': HTTP Error 429")
        )
        and not dl._is_subtitle_error(Exception("ERROR: Video unavailable")),
    )
    check(
        "网络错误可重试",
        dl._is_retryable(Exception("HTTP Error 503: Service Unavailable"))
        and not dl._is_retryable(Exception("This video is private")),
    )
    dropped = dl._drop_subtitles({"writesubtitles": True, "writeautomaticsub": True, "subtitlesformat": "json3"})
    check(
        "去掉字幕请求后只剩媒体",
        dropped["writesubtitles"] is False
        and dropped["writeautomaticsub"] is False
        and "subtitlesformat" not in dropped,
    )

    # 默认策略：先拿 YouTube 字幕；被限流 ≠ 没字幕，不该白跑几分钟的本地转写
    from . import pipeline as pipe_mod
    from .config import cfg

    saved_db = db.DB_FILE
    try:
        with _scratch_dir() as tmp:
            db.DB_FILE = Path(tmp) / "t.db"
            db.init_db()
            pk = db.create_video("https://youtu.be/zzzzzzzzzzz", "zzzzzzzzzzz", "t", "c", 1000, "")
            fake_media = Path(tmp) / "m.mp4"
            fake_media.write_bytes(b"x")

            def fake_download(url, stem, on_progress=None, *, skip_media=False, skip_subs=False):
                return fake_media, [], {"source": "", "subs_error": "HTTP Error 429: Too Many Requests"}

            called = {"whisper": False}

            def boom(*a, **k):
                called["whisper"] = True
                raise AssertionError("限流不该跑语音识别")

            saved = (pipe_mod.download, pipe_mod.transcribe_file, dict(cfg._data["asr"]))
            pipe_mod.download = fake_download
            pipe_mod.transcribe_file = boom
            cfg._data["asr"]["backend"] = "auto"
            try:
                pipe_mod.run(pk, "https://youtu.be/zzzzzzzzzzz")
                msg = "没抛错"
            except RuntimeError as exc:
                msg = str(exc)
            finally:
                pipe_mod.download, pipe_mod.transcribe_file = saved[0], saved[1]
                cfg._data["asr"].update(saved[2])
            check(
                "字幕限流不跑语音识别",
                not called["whisper"] and "429" in msg and "补字幕" in msg,
                msg[:60],
            )

            # 真没字幕（无 subs_error）才允许走 Whisper
            def fake_download2(url, stem, on_progress=None, *, skip_media=False, skip_subs=False):
                return fake_media, [], {"source": "", "subs_error": ""}

            def boom2(*a, **k):
                called["whisper"] = True
                raise RuntimeError("模拟：faster-whisper 没装")

            called["whisper"] = False
            pipe_mod.download = fake_download2
            pipe_mod.transcribe_file = boom2
            cfg._data["asr"]["backend"] = "auto"
            try:
                pipe_mod.run(pk, "https://youtu.be/zzzzzzzzzzz")
                msg2 = "没抛错"
            except RuntimeError as exc:
                msg2 = str(exc)
            finally:
                pipe_mod.download, pipe_mod.transcribe_file = saved[0], saved[1]
                cfg._data["asr"].update(saved[2])
            check(
                "真没字幕才用语音识别",
                called["whisper"] and "没有英文字幕" in msg2,
                msg2[:60],
            )
    finally:
        db.DB_FILE = saved_db

    print("[11] 分开下载（视频 / 字幕）")
    from . import api as api_mod

    check(
        "mode → 任务类型",
        api_mod.MODES.get("video-only") == "ingest-video"
        and api_mod.MODES.get("subs-only") == "ingest-subs"
        and api_mod.MODES.get("video+subs") == "ingest"
        and api_mod.MODES.get("") == "ingest",
    )
    saved_db = db.DB_FILE
    try:
        with _scratch_dir() as tmp:
            db.DB_FILE = Path(tmp) / "s.db"
            db.init_db()
            pk3 = db.create_video("https://youtu.be/abcdefghijk", "abcdefghijk", "t", "c", 1000, "")
            check("空视频字幕行数 0", db.count_segments(pk3) == 0)
            from .models import Segment

            db.replace_segments(
                pk3,
                [
                    Segment(seq=0, start_ms=0, end_ms=900, text="hi", words=[]),
                    Segment(seq=1, start_ms=900, end_ms=1800, text="there", words=[]),
                ],
            )
            check("行数统计", db.count_segments(pk3) == 2)

            from . import pipeline as pl

            try:
                pl.run(pk3, "https://youtu.be/abcdefghijk", skip_media=True, skip_subs=True)
                ok = False
            except RuntimeError as exc:
                ok = "不能同时" in str(exc)
            check("互斥模式被拦", ok)
    finally:
        db.DB_FILE = saved_db

    print()
    if failures:
        print(f"✗ {len(failures)} 项失败：{', '.join(failures)}")
        return 1
    print("全部通过 ✓")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    return _selftest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_SLUG, description=f"{APP_NAME}：本地 YouTube 英语学习工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="下载 + 字幕 + 切分，一次跑完")
    p.add_argument("url")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument(
        "--subs-only",
        action="store_true",
        help="只取字幕，不下载视频（省带宽；不用 ffmpeg）",
    )
    p.add_argument(
        "--video-only",
        action="store_true",
        help="只下载视频文件，不取字幕（之后再补）",
    )
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("serve", help="启动 Web 服务")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("selftest", help="本地自检")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("analyze", help="逐句解析（译文 + 语法 + 短语），需要先配好大模型")
    p.add_argument("pk", type=int)
    p.add_argument("--force", action="store_true", help="重跑已缓存的行")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("subs", help="只补字幕（视频已在本地时用）")
    p.add_argument("pk", type=int)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_subs)

    p = sub.add_parser("list", help="列出已处理视频")
    p.set_defaults(func=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    from . import ensure_utf8_stdio

    ensure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
