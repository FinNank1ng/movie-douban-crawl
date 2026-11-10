#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
douban_api_collect.py —— 用豆瓣手机 App 的 JSON 接口，把「短评 / 长评 / 影片补全」补齐
几年不见技术渐涨这一块（nmsl）

为什么单独写这一个：
    原来的 HTML 采集（/subject/{id}/comments、/subject/{id}/reviews）会被反爬挡住
    （报「访问受到限制」的就是这几个页面）。换成 App 的 rexxar 接口就正常了。

做什么：
    读 response.xlsx 里的 top250（只取 影片名 + 详情页URL），对每部影片：
      1. 影片接口 → 片长 / 又名 / 完整主演 / 评分 / 评价人数
      2. 短评接口 → 若干条短评（作者、评分、时间、有用数、内容）
      3. 长评接口 → 若干条长评的元信息（标题、作者、时间、有用数、回应数、摘要）
      4. --full-text 时再逐条取长评正文（请求量大，默认关）

输出：
    api_collect.xlsx    三个工作表：影片补全 / 短评 / 长评
    data/api_短评.jsonl、data/api_长评.jsonl
"""
import argparse
import json
import os
import sys

import pandas as pd

import douban_api as api

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(BASE_DIR, "response.xlsx")
OUT_XLSX = os.path.join(BASE_DIR, "api_collect.xlsx")
DATA_DIR = os.path.join(BASE_DIR, "data")

SHORT_COLS = ['影片名', '影片链接', '评论ID', '评论者', '评论者主页', '评分',
              '评论时间', '有用数', '评论内容', '评论链接']
LONG_COLS = ['影片名', '影片链接', '影评标题', '作者', '作者主页', '评分',
             '发布时间', '有用数', '回应数', '影评正文', '影评链接']
MOVIE_COLS = ['影片名', '片长', '又名', '主演（全）', '评分', '评价人数']


def log(m):
    print(m, flush=True)


def load_movies(limit=None):
    df = pd.read_excel(SOURCE, sheet_name='top250')
    rows = []
    for _, r in df.iterrows():
        link = str(r.get('详情页URL') or r.get('影片链接') or '')
        import re
        m = re.findall(r'subject/(\d+)', link)
        if not m:
            continue
        rows.append({'影片名': r.get('影片名'), '影片链接': link, 'sid': m[0]})
    if limit:
        rows = rows[:limit]
    return rows


def collect_short(m, count):

    out = []
    for it in api.interests(m['sid'], count=count, start=0):
        out.append({
            '影片名': m['影片名'],
            '影片链接': m['影片链接'],
            '评论ID': it.get('短评id', ''),
            '评论者': it.get('作者', ''),
            '评论者主页': '',
            '评分': it.get('评分', ''),
            '评论时间': it.get('时间', ''),
            '有用数': it.get('有用数', 0),
            '评论内容': it.get('内容', ''),
            '评论链接': f"https://movie.douban.com/subject/{m['sid']}/comments/",
        })
    return out


def collect_long(m, count, full_text=False):

    d = api.get_json(f"{api.BASE}/movie/{m['sid']}/reviews?count={count}&start=0",
                     f"reviews_{m['sid']}_0_{count}.json")
    out = []
    for r in (d or {}).get('reviews', []) or []:
        rid = str(r.get('id', ''))
        rating = (r.get('rating') or {}).get('value', '')
        body = ''
        if full_text and rid:
            info = api.review(rid)
            body = api.html_to_text(info.get('正文HTML', '')) if info else ''
        out.append({
            '影片名': m['影片名'],
            '影片链接': m['影片链接'],
            '影评标题': r.get('title', ''),
            '作者': ((r.get('user') or {}).get('name') or ''),
            '作者主页': ((r.get('user') or {}).get('url') or ''),
            '评分': rating,
            '发布时间': r.get('create_time', ''),
            '有用数': r.get('useful_count', 0),
            '回应数': r.get('comments_count', 0),
            '影评正文': body or r.get('abstract', ''),
            '影评链接': r.get('url', '') or f"https://movie.douban.com/review/{rid}/",
        })
    return out


def append_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 部（0 = 全部）")
    ap.add_argument("--comments", type=int, default=20, help="每部取多少条短评")
    ap.add_argument("--reviews", type=int, default=10, help="每部取多少条长评")
    ap.add_argument("--delay", type=float, default=2.5, help="每次请求间隔（秒）")
    ap.add_argument("--full-text", action="store_true", help="长评正文也抓（请求量翻倍）")
    ap.add_argument("--only", choices=["movies", "short", "long"], default=None)
    args = ap.parse_args()

    movies = load_movies(args.limit or None)
    log(f"要处理 {len(movies)} 部影片 · 每部短评 {args.comments} 条 · 长评 {args.reviews} 条")

    short_rows, long_rows, movie_rows = [], [], []
    for i, m in enumerate(movies, 1):
        log(f"[{i}/{len(movies)}] {m['影片名']} (sid={m['sid']})")
        try:
            if args.only in (None, "movies"):
                info = api.movie(m['sid'], delay=args.delay)
                if info:
                    movie_rows.append({
                        '影片名': m['影片名'],
                        '片长': info.get('片长', ''),
                        '又名': info.get('又名', ''),
                        '主演（全）': info.get('主演', ''),
                        '评分': info.get('评分', ''),
                        '评价人数': info.get('评价人数', ''),
                    })
            if args.only in (None, "short"):
                s = collect_short(m, args.comments)
                short_rows.extend(s)
                log(f"    短评 {len(s)} 条")
            if args.only in (None, "long"):
                lo = collect_long(m, args.reviews, args.full_text)
                long_rows.extend(lo)
                log(f"    长评 {len(lo)} 条")
        except Exception as e:
            log(f"    这部失败了，跳过：{e}")
            continue

    if short_rows:
        append_jsonl(os.path.join(DATA_DIR, "api_短评.jsonl"), short_rows)
    if long_rows:
        append_jsonl(os.path.join(DATA_DIR, "api_长评.jsonl"), long_rows)

    with pd.ExcelWriter(OUT_XLSX, engine="openpyxl") as w:
        if movie_rows:
            pd.DataFrame(movie_rows, columns=MOVIE_COLS).to_excel(w, sheet_name="影片补全", index=False)
        if short_rows:
            pd.DataFrame(short_rows, columns=SHORT_COLS).to_excel(w, sheet_name="短评", index=False)
        if long_rows:
            pd.DataFrame(long_rows, columns=LONG_COLS).to_excel(w, sheet_name="长评", index=False)

    log(f"\n已写入 {OUT_XLSX}")
    log(f"  影片补全 {len(movie_rows)} 部 · 短评 {len(short_rows)} 条 · 长评 {len(long_rows)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
