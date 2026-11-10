#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
douban_reviews.py —— 豆瓣「最受欢迎的影评」前三页采集
"""
import argparse
import json
import os
import re
import sys
import time

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests
from bs4 import BeautifulSoup

import douban_api as api

try:
    import lxml  # noqa: F401
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "reviews_cache")
OUT_DIR = os.path.join(HERE, "reviews")
MD_DIR = os.path.join(OUT_DIR, "md")

BASE = "https://movie.douban.com/review/best/?start={}"
HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'),
    'Referer': 'https://movie.douban.com/review/best/',
}

# 电脑版影片页有反爬，手机版能正常打开
MOBILE_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
                   'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 '
                   'Mobile/15E148 Safari/604.1'),
}

RATING = {"allstar50": "力荐(5)", "allstar40": "推荐(4)", "allstar30": "还行(3)",
          "allstar20": "较差(2)", "allstar10": "很差(1)"}


def log(msg):
    print(msg, flush=True)


def get(url, cache_path, delay=1.5, retries=3, as_json=False, headers=None):
    """带缓存的 GET：本地有了就不联网"""
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 200:
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f) if as_json else f.read()

    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers or HEADERS, timeout=20)
            if r.status_code == 200 and len(r.text) > 200:
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as f:
                    f.write(r.text)
                time.sleep(delay)
                return r.json() if as_json else r.text
            log(f"    HTTP {r.status_code}，第 {attempt} 次重试…")
        except Exception as e:
            log(f"    失败（第 {attempt} 次）：{e}")
        time.sleep(delay * attempt)
    raise RuntimeError(f"抓不下来：{url}")


def text_of(node, default="无"):
    return node.get_text(" ", strip=True) if node is not None else default


def parse_list(html):
    """解析一页「最受欢迎影评」，返回这一页的影评基本信息列表"""
    soup = BeautifulSoup(html, PARSER)
    out = []
    for item in soup.select('div.review-item'):
        try:
            rid = item.get('id', '').strip()
            review_url = f"https://movie.douban.com/review/{rid}/"

            # 影片
            subject_a = item.select_one('a.subject-img')
            movie = '无'
            subject_url = ''
            poster = ''
            if subject_a is not None:
                subject_url = subject_a.get('href', '')
                img = subject_a.find('img')
                if img is not None:
                    movie = img.get('alt') or img.get('title') or '无'
                    poster = img.get('src', '')

            # 作者 / 时间 / 力荐
            author = text_of(item.select_one('a.name'))
            author_url = ''
            a_name = item.select_one('a.name')
            if a_name is not None:
                author_url = a_name.get('href', '')

            meta = item.select_one('span.main-meta')
            pub_time = text_of(meta, '')
            pub_date = meta.get('content', '') if meta is not None else ''

            rating = '无'
            star = item.select_one('span.main-title-rating')
            if star is not None:
                cls = " ".join(star.get('class', []))
                for k, v in RATING.items():
                    if k in cls:
                        rating = v
                        break

            # 标题
            h2 = item.select_one('div.main-bd h2')
            title = text_of(h2, '无')
            if title == '无':
                title = text_of(item.select_one('h2'), '无')

            # 简述
            short = text_of(item.select_one('div.short-content'), '')
            short = re.sub(r'\s*\(展开\)\s*$', '', short).strip()
            spoiler = '这篇影评可能有剧透' in short
            short = short.replace('这篇影评可能有剧透', '').strip()

            # 有用 / 没用 / 回应
            up = text_of(item.select_one('a.action-btn.up'), '')
            down = text_of(item.select_one('a.action-btn.down'), '')
            reply = text_of(item.select_one('a.reply'), '')
            up = re.sub(r'\D', '', up) or '0'
            down = re.sub(r'\D', '', down) or '0'
            reply = re.sub(r'\D', '', reply) or '0'

            out.append({
                '影评id': rid,
                '影评标题': title,
                '影评链接': review_url,
                '作者': author,
                '作者主页': author_url,
                '发布时间': pub_time,
                '发布日期': pub_date,
                '评价': rating,
                '影片名': movie,
                '影片链接': subject_url,
                '海报': poster,
                '有用数': int(up),
                '没用数': int(down),
                '回应数': int(reply),
                '有剧透': '是' if spoiler else '否',
                '简述': short,
            })
        except Exception as e:
            log(f"  一条解析失败：{e}")
            continue
    return out


def parse_full(rid, payload):
    """从 /j/review/{id}/full 的 JSON 里取出完整正文"""
    body_html = payload.get('body', '') if isinstance(payload, dict) else ''
    if not body_html:
        return '', 0
    soup = BeautifulSoup(body_html, PARSER)
    node = soup.select_one('div.review-content') or soup.select_one('div.main-bd')
    if node is None:
        return '', 0
    # 去掉「展开」「有用」这类按钮文字
    for junk in node.select('.expand, .fold, script, style'):
        junk.decompose()
    paras = [p.get_text(" ", strip=True) for p in node.find_all('p')]
    paras = [p for p in paras if p]
    content = "\n\n".join(paras) if paras else node.get_text("\n", strip=True)
    content = re.sub(r'\n{3,}', '\n\n', content).strip()
    return content, len(content)


def fetch_movie_info(sid, delay=1.5):
    """影片信息：走手机 App 的 JSON 接口（电脑版反爬、m 版 HTML 里没有演职员）"""
    m = api.movie(sid, delay=delay)
    if not m:
        return {'豆瓣评分': '无', '评价人数': '无', '影片信息': '无', '剧情简介': '无',
                '影片导演': '无', '影片主演': '无', '片长': '无', '又名': '无'}
    line = " / ".join([x for x in [m.get('国家', ''), m.get('类型', ''),
                                   m.get('上映日期', ''), m.get('片长', '')] if x])
    actors = m.get('主演', '') or '无'
    if len(actors) > 90:
        actors = " / ".join(actors.split(' / ')[:6])
    return {
        '豆瓣评分': m.get('评分') or '无',
        '评价人数': m.get('评价人数') or '无',
        '影片信息': line or '无',
        '剧情简介': (m.get('简介') or '无')[:300],
        '影片导演': m.get('导演') or '无',
        '影片主演': actors,
        '片长': m.get('片长') or '无',
        '又名': m.get('又名') or '无',
    }


def enrich_with_movies(df, delay=1.5):
    """把每篇影评对应的影片信息补上（按影片 id 去重，一部只请求一次）"""
    if '影片链接' not in df.columns:
        return df

    sids = []
    for link in df['影片链接'].fillna(''):
        m = re.findall(r'subject/(\d+)', str(link))
        sids.append(m[0] if m else '')
    df = df.copy()
    df['影片id'] = sids

    uniq = [s for s in dict.fromkeys(sids) if s]
    log(f"  补全影片信息：{len(uniq)} 部不同的影片（App JSON 接口）")
    info = {}
    for i, sid in enumerate(uniq, 1):
        try:
            info[sid] = fetch_movie_info(sid, delay=delay)
            log(f"    [{i}/{len(uniq)}] {sid} 评分 {info[sid]['豆瓣评分']} · 导演 {info[sid]['影片导演'][:14]}")
        except Exception as e:
            log(f"    [{i}/{len(uniq)}] {sid} 失败：{e}")
            info[sid] = {'豆瓣评分': '无', '评价人数': '无', '影片信息': '无',
                         '剧情简介': '无', '影片导演': '无', '影片主演': '无',
                         '片长': '无', '又名': '无'}

    for col in ['豆瓣评分', '评价人数', '影片信息', '剧情简介',
                '影片导演', '影片主演', '片长', '又名']:
        df[col] = df['影片id'].map(lambda s: (info.get(s) or {}).get(col, '无'))
    return df


def save_md(row):
    os.makedirs(MD_DIR, exist_ok=True)
    name = f"{row['影评id']}-{re.sub(r'[\\\\/:*?\"<>|]', '_', row['影评标题'])[:40]}.md"
    path = os.path.join(MD_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {row['影评标题']}\n\n")
        f.write(f"- 影片：{row['影片名']}（{row['影片链接']}）\n")
        if row.get('影片导演') and row['影片导演'] != '无':
            f.write(f"- 导演：{row['影片导演']}\n")
        if row.get('影片主演') and row['影片主演'] != '无':
            f.write(f"- 主演：{row['影片主演']}\n")
        if row.get('影片信息') and row['影片信息'] != '无':
            f.write(f"- 影片信息：{row['影片信息']}\n")
        if row.get('豆瓣评分') and row['豆瓣评分'] != '无':
            f.write(f"- 豆瓣评分：{row['豆瓣评分']}（{row.get('评价人数','?')} 人评价）\n")
        f.write(f"- 作者：{row['作者']}（{row['作者主页']}）\n")
        f.write(f"- 发布：{row['发布时间']}\n")
        f.write(f"- 评价：{row['评价']}　有用 {row['有用数']} · 没用 {row['没用数']} · 回应 {row['回应数']}\n")
        f.write(f"- 原文：{row['影评链接']}\n")
        if row['有剧透'] == '是':
            f.write("- ⚠ 这篇影评可能有剧透\n")
        f.write("\n---\n\n")
        f.write(row['完整正文'] or row['简述'] or '（没抓到正文）')
        f.write("\n")
    return path



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=3, help="抓前几页（每页 10 篇）")
    ap.add_argument("--delay", type=float, default=1.5, help="每次请求间隔秒数")
    ap.add_argument("--no-full", action="store_true", help="不抓完整正文（只列表信息）")
    args = ap.parse_args()

    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)

    rows = []
    for page in range(args.pages):
        start = page * 10
        log(f"[第 {page + 1} 页] start={start}")
        html = get(BASE.format(start), os.path.join(CACHE, f"best_{start}.html"), delay=args.delay)
        page_rows = parse_list(html)
        log(f"  解析到 {len(page_rows)} 篇")
        rows.extend(page_rows)

    if args.no_full:
        log("（--no-full，跳过完整正文）")
    else:
        log(f"\n开始抓完整正文（{len(rows)} 篇，每篇间隔 {args.delay} 秒）…")
        for i, row in enumerate(rows, 1):
            rid = row['影评id']
            try:
                payload = get(f"https://movie.douban.com/j/review/{rid}/full",
                              os.path.join(CACHE, f"full_{rid}.json"),
                              delay=args.delay, as_json=True)
                content, n = parse_full(rid, payload)
                row['完整正文'] = content
                row['正文字数'] = n
                log(f"  [{i}/{len(rows)}] {rid} {row['影评标题'][:24]}… {n} 字")
            except Exception as e:
                row['完整正文'] = ''
                row['正文字数'] = 0
                log(f"  [{i}/{len(rows)}] {rid} 正文失败：{e}")

    for row in rows:
        row.setdefault('完整正文', '')
        row.setdefault('正文字数', len(row['完整正文']))

    df = pd.DataFrame(rows)
    log("\n补全影片信息…")
    df = enrich_with_movies(df)
    rows = df.where(pd.notna(df), None).to_dict("records")

    xlsx = os.path.join(OUT_DIR, "reviews.xlsx")
    with pd.ExcelWriter(xlsx, engine='openpyxl') as w:
        df.to_excel(w, sheet_name='影评', index=False)
    log(f"\n已写入 {xlsx}（{len(df)} 行）")

    with open(os.path.join(OUT_DIR, "reviews.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    log(f"已写入 reviews.jsonl")

    for r in rows:
        save_md(r)
    log(f"已写入 {len(rows)} 个 Markdown 到 reviews/md/")

    log("\n小结：")
    log(f"  篇数        {len(df)}")
    log(f"  抓到正文    {int((df['正文字数'] > 0).sum())}")
    log(f"  平均字数    {int(df['正文字数'].mean())}")
    log(f"  涉及影片    {df['影片名'].nunique()} 部")
    return 0


if __name__ == "__main__":
    sys.exit(main())
