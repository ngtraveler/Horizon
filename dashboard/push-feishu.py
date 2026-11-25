#!/usr/bin/env python3
"""Push newly collected items to a Feishu (Lark) group bot, translated to Chinese.

Reads the snapshot written by collect.py, keeps a small state file of what has
already been pushed, translates the new titles in one model call, and posts a
Feishu interactive card.

Dependency-free (standard library only), so it runs on the system Python.

Typical use
-----------
    # 先采集，再推送（--collect 会让它自己先跑采集）
    python3 push-feishu.py --collect --hours 6 --limit 8

    # 只看会发什么，不真的发
    python3 push-feishu.py --dry-run --limit 3

    # 定时任务里用：每 30 分钟采集 + 推送
    python3 push-feishu.py --collect --hours 2 --limit 6

Environment
-----------
    HORIZON_WEBHOOK_URL   Feishu custom-bot webhook URL
    DEEPSEEK_API_KEY      model key used for translation (from config.json ai.api_key_env)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

DEFAULT_BASE_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
    "ali": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "minimax": "https://api.minimax.io/v1",
    "ollama": "http://localhost:11434/v1",
}

SYSTEM_PROMPT = (
    "你是船艇与海事行业的新闻编辑，读者的中文母语者。"
    "把每条英文标题翻译成简洁、准确的中文标题（不超过 40 字），"
    "再写一句不超过 60 字的中文要点，说明发生了什么。"
    "保留品牌、型号、船名的原文（如 Ferretti、Bavaria、Garmin、Bayesian）。"
    "不要编造原文没有的数字或结论。只输出 JSON。"
)


def load_env_file(path):
    """Minimal .env loader: KEY=VALUE lines, # comments, no export prefix needed."""
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


def resolve_base_url(ai_config):
    base = (ai_config.get("base_url") or "").strip()
    if base:
        return base.rstrip("/")
    provider = (ai_config.get("provider") or "deepseek").strip()
    return DEFAULT_BASE_URLS.get(provider, DEFAULT_BASE_URLS["deepseek"])


def post_json(url, payload, timeout=25):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"_raw": body}


def translate(items, ai_config):
    """One model call for the whole batch. Returns {id: {title, note}}."""
    key_env = ai_config.get("api_key_env") or "DEEPSEEK_API_KEY"
    api_key = os.environ.get(key_env, "").strip()
    if not api_key:
        print("! {} 为空，跳过翻译，直接发英文标题".format(key_env))
        return {}

    payload_items = [{"id": index, "title": item["title"]} for index, item in enumerate(items)]
    body = {
        "model": ai_config.get("model") or "deepseek-chat",
        "temperature": 0.2,
        "max_tokens": 1500,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "把下面 JSON 里的每一条翻译成中文，输出格式："
                '{"items":[{"id":0,"zh_title":"...","zh_note":"..."}]}\n'
                + json.dumps({"items": payload_items}, ensure_ascii=False),
            },
        ],
    }

    url = resolve_base_url(ai_config) + "/chat/completions"
    try:
        response = post_json(
            url,
            body,
            timeout=60,
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        print("! 翻译请求失败 HTTP {}：{}".format(exc.code, detail))
        return {}
    except Exception as exc:  # noqa: BLE001 - translation is best-effort
        print("! 翻译请求失败：{}: {}".format(type(exc).__name__, exc))
        return {}

    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print("! 翻译返回格式异常：{}".format(str(response)[:200]))
        return {}

    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        print("! 翻译返回不是 JSON：{}".format(content[:200]))
        return {}

    try:
        parsed = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        print("! 翻译 JSON 解析失败")
        return {}

    result = {}
    for row in parsed.get("items", []):
        try:
            index = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= index < len(items):
            result[index] = {
                "title": (row.get("zh_title") or "").strip(),
                "note": (row.get("zh_note") or "").strip(),
            }
    return result


def build_card(items, translations, title):
    elements = [
        {
            "tag": "markdown",
            "content": "共 **{}** 条新内容 · 由 Horizon 源清单自动采集".format(len(items)),
        },
        {"tag": "hr"},
    ]
    for index, item in enumerate(items):
        translated = translations.get(index) or {}
        zh_title = translated.get("title") or item["title"]
        note = translated.get("note") or ""
        when = datetime.fromtimestamp(item["published_ts"]).strftime("%m-%d %H:%M")
        lines = ["**{}. {}**".format(index + 1, zh_title)]
        if note:
            lines.append(note)
        meta = "{} · {}".format(item["source"], when)
        if translated:
            meta = "原文：{}　|　{}".format(item["title"], meta)
        lines.append(meta)
        lines.append("[阅读原文]({})".format(item["url"]))
        elements.append({"tag": "markdown", "content": "\n".join(lines)})
        if index != len(items) - 1:
            elements.append({"tag": "hr"})

    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "body": {"elements": elements},
        },
    }


def load_state(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return dict(data.get("ids") or {})
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path, ids, keep=3000):
    ordered = sorted(ids.items(), key=lambda kv: kv[1], reverse=True)[:keep]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"ids": dict(ordered)}, handle, ensure_ascii=False)
    os.replace(tmp, path)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default=os.path.join(REPO, "data", "config.json"))
    parser.add_argument("-s", "--snapshot", default=os.path.join(HERE, "data.json"))
    parser.add_argument("--state", default=os.path.join(REPO, "data", "pushed.json"))
    parser.add_argument("--env-file", default=os.path.join(REPO, ".env"))
    parser.add_argument("--hours", type=float, default=6, help="只看最近 N 小时的内容")
    parser.add_argument("--limit", type=int, default=8, help="单条卡片最多几条")
    parser.add_argument("--title", default="🌅 船艇行业快讯")
    parser.add_argument("--url-env", default="HORIZON_WEBHOOK_URL")
    parser.add_argument("--no-translate", action="store_true")
    parser.add_argument("--collect", action="store_true", help="先运行 collect.py 再推送")
    parser.add_argument("--dry-run", action="store_true", help="打印卡片 JSON，不发送、不改状态")
    parser.add_argument(
        "--test",
        action="store_true",
        help="只发一张测试卡片：同时验证模型 Key 和飞书机器人，不采集、不改状态",
    )
    args = parser.parse_args(argv)

    load_env_file(args.env_file)

    if not os.path.isfile(args.config):
        print("! 找不到配置文件 {}".format(args.config))
        return 1
    with open(args.config, encoding="utf-8") as handle:
        config = json.load(handle)
    ai_config = config.get("ai") or {}

    if args.test:
        sample = [
            {
                "id": "selftest-{}".format(int(time.time())),
                "title": "Palumbo Superyachts secures 30-year concession for Ancona shipyard expansion",
                "url": "https://www.superyachttimes.com/",
                "source": "SuperYacht Times",
                "published_ts": time.time(),
            }
        ]
        translations = {} if args.no_translate else translate(sample, ai_config)
        if not args.no_translate and not translations:
            print("  翻译没有成功 —— Key 或额度有问题（见上面那行报错）")
        card = build_card(sample, translations, "🧪 Marine Radar 测试卡片")
        if args.dry_run:
            print(json.dumps(card, ensure_ascii=False, indent=2))
            return 0
        url = (os.environ.get(args.url_env) or "").strip()
        if not url:
            print("! 环境变量 {} 为空，无法发送".format(args.url_env))
            return 2
        try:
            response = post_json(url, card)
        except Exception as exc:  # noqa: BLE001
            print("! 发送失败：{}: {}".format(type(exc).__name__, exc))
            return 1
        code = response.get("code", response.get("StatusCode"))
        if code not in (0, None):
            print("! 飞书返回错误：{}".format(json.dumps(response, ensure_ascii=False)[:300]))
            return 1
        print("✓ 测试卡片已发送，去飞书群里看看")
        return 0

    if args.collect:
        cmd = [sys.executable, os.path.join(HERE, "collect.py"), "-c", args.config,
               "--hours", str(int(args.hours) or 24)]
        print("→ 采集：{}".format(" ".join(cmd)))
        if subprocess.call(cmd) != 0:
            print("! 采集失败，中止推送")
            return 1

    if not os.path.isfile(args.snapshot):
        print("! 找不到 {}，先运行 collect.py".format(args.snapshot))
        return 1

    with open(args.snapshot, encoding="utf-8") as handle:
        snapshot = json.load(handle)

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=args.hours)).timestamp()
    future = (now + timedelta(hours=1)).timestamp()
    pushed = load_state(args.state)

    fresh = [
        item
        for item in snapshot.get("items", [])
        if cutoff <= item.get("published_ts", 0) <= future and item.get("id") not in pushed
    ]
    fresh.sort(key=lambda item: item["published_ts"], reverse=True)

    if not fresh:
        print("没有新内容（窗口 {}h，已推送 {} 条历史记录）".format(args.hours, len(pushed)))
        return 0

    selected = fresh[: args.limit]
    print("窗口内新内容 {} 条，本次推送 {} 条".format(len(fresh), len(selected)))

    translations = {}
    if not args.no_translate:
        print("→ 翻译 {} 条…".format(len(selected)))
        translations = translate(selected, ai_config)
        print("  翻译成功 {} 条".format(len(translations)))

    stamp = datetime.now().strftime("%m-%d %H:%M")
    card = build_card(selected, translations, "{} · {}".format(args.title, stamp))

    if args.dry_run:
        print("\n--- 卡片 JSON（未发送）---")
        print(json.dumps(card, ensure_ascii=False, indent=2)[:4000])
        print("\n--- 预览 ---")
        for index, item in enumerate(selected):
            zh = (translations.get(index) or {}).get("title") or item["title"]
            print("  {}. {}".format(index + 1, zh))
        return 0

    url = (os.environ.get(args.url_env) or "").strip()
    if not url:
        print("! 环境变量 {} 为空，无法发送。请在 .env 里填入飞书 webhook 地址".format(args.url_env))
        return 2

    try:
        response = post_json(url, card)
    except Exception as exc:  # noqa: BLE001
        print("! 发送失败：{}: {}".format(type(exc).__name__, exc))
        return 1

    code = response.get("code", response.get("StatusCode"))
    if code not in (0, None):
        print("! 飞书返回错误：{}".format(json.dumps(response, ensure_ascii=False)[:300]))
        return 1

    now = time.time()
    for item in selected:
        pushed[item["id"]] = now
    save_state(args.state, pushed)
    print("✓ 已发送 {} 条，状态写入 {}".format(len(selected), args.state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
