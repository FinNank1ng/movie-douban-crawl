#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
douban_api.py —— 豆瓣手机 App 的 JSON 接口（2026 年）

接口：
    movie/{id}            影片：导演、主演、类型、国家、片长、评分、简介、又名、语言
    movie/{id}/interests  短评：作者、内容、评分、时间
    movie/{id}/reviews    长评列表：id、标题、摘要、有用数、回应数、时间
    review/{rid}          单篇长评：完整正文 HTML、有用/没用、回应数、剧透、作者、评分
"""
import json
import os
import random
import sys
import time

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "api_cache")
BASE = "https://m.douban.com/rexxar/api/v2"

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
                   'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 '
                   'Mobile/15E148 Safari/604.1'),
    'Referer': 'https://m.douban.com/movie/',
    'Accept': 'application/json',
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# 每 N 次请求强制歇 M 秒（豆瓣对"连着几百次"很敏感，分批会舒服很多）
_PACING = {"every": 0, "sleep": 0, "count": 0}

# 豆瓣会按"接口"单独限流：code 1309 = subject_ip_rate_limit。
# 一旦发现详情接口被限，就本次运行直接跳过它，不要每次都干等 75 秒。
_BLOCKED = {"movie": False, "tv": False}


def set_pacing(every: int, sleep_sec: float):
    """every>0 时，每 every 次请求暂停 sleep_sec 秒。"""
    _PACING["every"] = max(0, int(every))
    _PACING["sleep"] = max(0.0, float(sleep_sec))
    _PACING["count"] = 0


def _pace():
    if _PACING["every"] <= 0:
        return
    _PACING["count"] += 1
    if _PACING["count"] % _PACING["every"] == 0:
        log(f"    已请求 {_PACING['count']} 次，按计划暂停 {_PACING['sleep']:.0f} 秒…")
        time.sleep(_PACING["sleep"])


def log(msg):
    print(msg, flush=True)


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def get_json(url, cache_name, delay=2.0, retries=4):
    """带缓存 + 退避的 JSON 请求缓存命中就不联网"""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, cache_name)
    cached = _load(path)
    if cached is not None:
        return cached

    backoff = 5
    for attempt in range(1, retries + 1):
        try:
            r = SESSION.get(url, timeout=25)
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    log(f"    返回的不是 JSON（{len(r.text)} 字节），可能被挡了")
                    data = None
                if data is not None:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
                    time.sleep(delay + random.random() * 1.2)
                    _pace()
                    return data
            elif r.status_code == 400:
                try:
                    err = r.json()
                except ValueError:
                    err = {}
                if isinstance(err, dict) and err.get('code') == 1309:
                    log("    该接口被豆瓣按 IP 限流了（code 1309），本次跳过；"
                        "等 1 小时左右再来")
                    for k in _BLOCKED:
                        _BLOCKED[k] = True
                    return None
                log(f"    HTTP 400，第 {attempt} 次重试…")
            elif r.status_code in (403, 418, 429):
                log(f"    HTTP {r.status_code}（被限流），等 {backoff} 秒再试…")
                time.sleep(backoff)
                backoff *= 2
                continue
            else:
                log(f"    HTTP {r.status_code}，第 {attempt} 次重试…")
        except Exception as e:
            log(f"    请求异常（第 {attempt} 次）：{e}")
        time.sleep(delay + random.random())
    return None



def movie(sid, delay=2.0):
    """影片/剧集信息返回中文键的 dict（取不到就给空值）"""
    # 先看缓存：缓存里有就照用，不受限流影响
    d = _load(os.path.join(CACHE, f"movie_{sid}.json"))
    if d is None:
        d = _load(os.path.join(CACHE, f"tv_{sid}.json"))
    if d is None:
        if _BLOCKED["movie"]:
            return {}
        d = get_json(f"{BASE}/movie/{sid}?for_mobile=1", f"movie_{sid}.json", delay=delay)
        if not d:
            # 剧集在 tv/ 下面
            d = get_json(f"{BASE}/tv/{sid}?for_mobile=1", f"tv_{sid}.json", delay=delay)
    if not d:
        return {}

    def names(key):
        v = d.get(key) or []
        out = []
        for x in v:
            out.append(x.get('name') if isinstance(x, dict) else str(x))
        return [n for n in out if n]

    rating = d.get('rating') or {}
    return {
        '影片id': str(sid),
        '影片名': d.get('title', ''),
        '原名': d.get('original_title', ''),
        '年份': d.get('year', ''),
        '导演': " / ".join(names('directors')),
        '主演': " / ".join(names('actors')),
        '类型': " ".join(d.get('genres') or []),
        '国家': " ".join(d.get('countries') or []),
        '片长': " / ".join(d.get('durations') or []),
        '语言': " ".join(d.get('languages') or []),
        '又名': " / ".join(d.get('aka') or []),
        '上映日期': " / ".join(d.get('pubdate') or []),
        '评分': rating.get('value', '') if rating else '',
        '评价人数': rating.get('count', '') if rating else '',
        '简介': (d.get('intro') or '').strip(),
        '海报': ((d.get('pic') or {}).get('large') or ''),
        '短评数': d.get('comments_count', ''),
        '长评数': d.get('review_count', ''),
    }


def review(rid, delay=2.0):
    """单篇长评的完整数据（正文是 HTML）"""
    d = get_json(f"{BASE}/review/{rid}", f"review_{rid}.json", delay=delay)
    if not d:
        return {}
    return {
        '影评id': str(rid),
        '标题': d.get('title', ''),
        '作者': ((d.get('user') or {}).get('name') or ''),
        '作者id': ((d.get('user') or {}).get('id') or ''),
        '发布时间': d.get('create_time') or d.get('updated_time') or '',
        '评分': ((d.get('rating') or {}).get('value') or ''),
        '有用数': d.get('useful_count', 0),
        '没用数': d.get('useless_count', 0),
        '回应数': d.get('comments_count', 0),
        '剧透': '是' if d.get('spoiler') else '否',
        '正文HTML': d.get('content', '') or '',
        '摘要': d.get('abstract', '') or '',
        '影片id': ((d.get('subject') or {}).get('id') or ''),
        '影片名': ((d.get('subject') or {}).get('title') or ''),
    }


def interests(sid, count=20, start=0, delay=2.0):
    """短评列表"""
    d = get_json(f"{BASE}/movie/{sid}/interests?count={count}&start={start}",
                 f"interests_{sid}_{start}_{count}.json", delay=delay)
    if not d:
        return []
    out = []
    for it in d.get('interests', []) or []:
        out.append({
            '影片id': str(sid),
            '短评id': it.get('id', ''),
            '作者': ((it.get('user') or {}).get('name') or ''),
            '评分': ((it.get('rating') or {}).get('value') or ''),
            '时间': it.get('create_time', ''),
            '有用数': it.get('vote_count', 0),
            '作者主页': ((it.get('user') or {}).get('url') or ''),
            '内容': (it.get('comment') or '').strip(),
        })
    return out


def html_to_text(html):
    """把正文 HTML 转纯文本（保留段落）"""
    from bs4 import BeautifulSoup
    try:
        import lxml  # noqa: F401
        parser = "lxml"
    except ImportError:
        parser = "html.parser"
    soup = BeautifulSoup(html or "", parser)
    for junk in soup.select('script, style, .expand, .fold'):
        junk.decompose()
    paras = [p.get_text(" ", strip=True) for p in soup.find_all('p')]
    paras = [p for p in paras if p]
    text = "\n\n".join(paras) if paras else soup.get_text("\n", strip=True)
    import re
    return re.sub(r'\n{3,}', '\n\n', text).strip()


if __name__ == "__main__":
    # 自测：python douban_api.py 36847425 17891670
    sid = sys.argv[1] if len(sys.argv) > 1 else "36847425"
    rid = sys.argv[2] if len(sys.argv) > 2 else "17891670"
    m = movie(sid)
    print("影片：", {k: m.get(k) for k in ['影片名', '导演', '主演', '类型', '片长', '评分']})
    r = review(rid)
    print("长评：", {k: r.get(k) for k in ['标题', '作者', '有用数', '回应数', '剧透']})
    print("正文字数：", len(html_to_text(r.get('正文HTML', ''))))
    cs = interests(sid, count=3)
    print("短评条数：", len(cs), "示例：", cs[0] if cs else None)
