import code
from pathlib import Path

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import douban_api as api


# 路径与配置

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
IMAGE_DIR = DATA_DIR / "images"
CHART_DIR = BASE_DIR / "charts"
OUTPUT_FILE = BASE_DIR / "response.xlsx"

TOP250_URL = "https://movie.douban.com/top250"
BASE_URL = "https://movie.douban.com"

LIST_CACHE = CACHE_DIR / "top250_pages"
DETAIL_CACHE = CACHE_DIR / "details"
COMMENT_CACHE = CACHE_DIR / "comments"
REVIEW_CACHE = CACHE_DIR / "reviews"

MOVIE_JSONL = DATA_DIR / "movies.jsonl"
COMMENT_JSONL = DATA_DIR / "comments.jsonl"
REVIEW_JSONL = DATA_DIR / "reviews.jsonl"

REQUEST_DELAY = 2.5          # 2026-11-10 从 1.5 提到 2.5：豆瓣现在对高频很敏感
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3

MOVIE_COLUMNS = [
    "排名", "影片名", "原版片名", "影片名（港）", "影片名（台）",
    "导演", "主演", "编剧", "上映日期", "年份", "片长",
    "制片国家/地区", "影片类型", "又名", "IMDb", "星级",
    "评分", "评价人数", "短评摘录", "剧情简介",
    "海报URL", "详情页URL"
]

COMMENT_COLUMNS = [
    "影片名", "影片链接", "评论ID", "评论者", "评论者主页",
    "评分", "评论时间", "有用数", "评论内容", "评论链接"
]

REVIEW_COLUMNS = [
    "影片名", "影片链接", "影评标题", "作者", "作者主页",
    "评分", "发布时间", "有用数", "回应数", "影评正文",
    "影评链接"
]



# 日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("douban")



# HTTP 会话
def build_session() -> requests.Session:
    """创建带重试机制的 HTTP Session"""
    session = requests.Session()

    retry = Retry(
        total=MAX_RETRIES,
        connect=MAX_RETRIES,
        read=MAX_RETRIES,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False
    )

    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": TOP250_URL
    })

    # 可选：使用用户自己有权访问的浏览器 Cookie
    # Windows PowerShell:
    #   $env:DOUBAN_COOKIE="你的 Cookie"
    cookie = os.getenv("DOUBAN_COOKIE", "").strip()
    if cookie:
        session.headers["Cookie"] = cookie

    return session


SESSION = build_session()


def make_soup(html: str) -> BeautifulSoup:
    """优先使用 lxml，没有安装时回退到 html.parser"""
    try:
        import lxml  # noqa: F401
        return BeautifulSoup(html, "lxml")
    except ImportError:
        return BeautifulSoup(html, "html.parser")


def is_restricted_page(html: str, status_code: int) -> bool:
    """判断页面是否为验证码、登录墙或访问限制提示"""
    text = html[:10000].lower()
    markers = (
        "检测到有异常请求",
        "访问过于频繁",
        "安全验证",
        "请输入验证码",
        "sec.douban.com",
        "登录后可查看",
        "请登录后",
        "403 forbidden"
    )
    return status_code in (403, 429) or any(x.lower() in text for x in markers)


def fetch_page(
    url: str,
    cache_file: Path | None = None,
    delay: float = REQUEST_DELAY,
    use_cache: bool = True
) -> str:
    """读取缓存或请求网页；遇到访问限制时明确报错"""
    if use_cache and cache_file and cache_file.exists():
        try:
            cached = cache_file.read_text(encoding="utf-8")
            if len(cached) > 300:
                return cached
        except OSError:
            pass

    response = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    html = response.text

    if is_restricted_page(html, response.status_code):
        raise PermissionError(
            f"豆瓣返回访问限制页面（HTTP {response.status_code}）：{url}"
        )

    response.raise_for_status()

    if len(html.strip()) < 300:
        raise RuntimeError(f"页面内容异常，长度只有 {len(html)}：{url}")

    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = cache_file.with_suffix(cache_file.suffix + ".tmp")
        temp_file.write_text(html, encoding="utf-8")
        temp_file.replace(cache_file)

    time.sleep(max(0, delay))
    return html



# 通用解析工具
def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def absolute_url(url: str | None, base: str = BASE_URL) -> str:
    return urljoin(base, url) if url else ""


def get_text(node: Tag | None, separator: str = " ") -> str:
    return clean_text(node.get_text(separator, strip=True)) if node else ""


def get_meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        node = soup.find("meta", attrs={"property": name})
        if node and node.get("content"):
            return clean_text(node["content"])
        node = soup.find("meta", attrs={"name": name})
        if node and node.get("content"):
            return clean_text(node["content"])
    return ""


def split_info_fields(info: Tag | None) -> dict[str, str]:
    """解析详情页 #info 中的标签字段，兼容 <br> 与文本节点混排"""
    result: dict[str, str] = {}
    if not info:
        return result

    current_label = ""
    current_parts: list[str] = []

    def save():
        nonlocal current_label, current_parts
        if current_label:
            value = clean_text(" ".join(current_parts))
            if value:
                result[current_label] = value
        current_label = ""
        current_parts = []

    for child in info.children:
        if isinstance(child, Tag) and child.name == "br":
            save()
            continue

        if isinstance(child, Tag) and child.name == "span" and child.get("class"):
            label = get_text(child).rstrip(":：")
            if label:
                save()
                current_label = label
                continue

        text = child.get_text(" ", strip=True) if isinstance(child, Tag) else str(child)
        text = clean_text(text)
        if not text:
            continue

        if not current_label:
            match = re.match(r"^([^:：]{1,20})[:：]\s*(.*)$", text)
            if match:
                current_label = match.group(1).strip()
                if match.group(2).strip():
                    current_parts.append(match.group(2).strip())
                continue

        current_parts.append(text)

    save()
    return result


def first_value(mapping: dict[str, str], *keys: str, default: str = "无") -> str:
    for key in keys:
        value = mapping.get(key, "")
        if value:
            return value
    return default


def parse_people(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r"\s*/\s*\.\.\.$", "", value)
    value = re.sub(r"\s*\.\.\.$", "", value)
    return value or "无"



# Top250 列表页
def parse_top250_page(html: str) -> list[dict[str, Any]]:
    soup = make_soup(html)
    movies: list[dict[str, Any]] = []

    for item in soup.select("ol.grid_view > li"):
        try:
            title_nodes = item.select(".hd span.title")
            if not title_nodes:
                continue

            title = get_text(title_nodes[0])
            original_title = get_text(title_nodes[1]).lstrip("/") if len(title_nodes) > 1 else ""

            title_other_node = item.select_one(".hd span.other")
            title_other = get_text(title_other_node).lstrip("/") if title_other_node else ""

            link_node = item.select_one(".hd a")
            detail_url = absolute_url(link_node.get("href")) if link_node else ""

            rank_text = get_text(item.select_one(".pic em"))
            rank_match = re.search(r"\d+", rank_text)
            rank = int(rank_match.group()) if rank_match else None

            rating = get_text(item.select_one(".rating_num"))
            rating_count_text = get_text(item.select_one(".star span:nth-of-type(4)"))
            count_match = re.search(r"(\d+)", rating_count_text)
            rating_count = int(count_match.group(1)) if count_match else None

            star_node = item.select_one("[class*='rating']")
            star_class = " ".join(star_node.get("class", [])) if star_node else ""
            star_match = re.search(r"rating(\d+)", star_class)
            star = int(star_match.group(1)) / 10 if star_match else None

            quote = get_text(item.select_one(".quote .inq"))

            # 列表页导演、主演和年份等信息
            info_text = get_text(item.select_one(".bd p"), " ")
            director_match = re.search(
                r"导演:\s*(.*?)(?=\s+主演:|$)", info_text
            )
            actor_match = re.search(
                r"主演:\s*(.*?)(?=\s+\d{4}\s*/|$)", info_text
            )

            year_match = re.search(r"\b(18|19|20)\d{2}\b", info_text)
            directors = clean_text(director_match.group(1)) if director_match else "无"
            actors = parse_people(actor_match.group(1)) if actor_match else "无"

            movies.append({
                "排名": rank,
                "影片名": title,
                "原版片名": original_title or "无",
                "影片名（港）": "无",
                "影片名（台）": "无",
                "导演": directors or "无",
                "主演": actors,
                "编剧": "无",
                "上映日期": "无",
                "年份": year_match.group(0) if year_match else "无",
                "片长": "无",
                "制片国家/地区": "无",
                "影片类型": "无",
                "又名": title_other or "无",
                "IMDb": "无",
                "星级": star if star is not None else "无",
                "评分": rating or "无",
                "评价人数": rating_count if rating_count is not None else "无",
                "短评摘录": quote or "无",
                "剧情简介": "无",
                "海报URL": "",
                "详情页URL": detail_url
            })

        except Exception as exc:
            logger.warning("列表项解析失败，已跳过：%s", exc)

    return movies



# 电影详情页
def parse_detail_page(html: str, movie: dict[str, Any]) -> dict[str, Any]:
    soup = make_soup(html)
    info = soup.select_one("#info")
    fields = split_info_fields(info)

    result = dict(movie)

    result["导演"] = first_value(fields, "导演", default=result.get("导演", "无"))
    result["主演"] = parse_people(first_value(fields, "主演", default=result.get("主演", "无")))
    result["编剧"] = first_value(fields, "编剧")
    result["上映日期"] = first_value(fields, "上映日期")
    result["片长"] = first_value(fields, "片长")
    result["制片国家/地区"] = first_value(fields, "制片国家/地区")
    result["影片类型"] = first_value(fields, "类型")
    result["又名"] = first_value(fields, "又名", default=result.get("又名", "无"))
    result["IMDb"] = first_value(fields, "IMDb")

    year = first_value(fields, "制片国家/地区", default="")
    if result.get("年份") in ("", "无"):
        date_value = result.get("上映日期", "")
        match = re.search(r"\b(18|19|20)\d{2}\b", date_value)
        result["年份"] = match.group(0) if match else "无"

    summary = soup.select_one("#link-report .all") or soup.select_one("#link-report")
    if summary:
        result["剧情简介"] = get_text(summary, "\n") or "无"
    else:
        result["剧情简介"] = get_meta(soup, "og:description") or "无"

    poster = soup.select_one("#mainpic img") or soup.select_one("img[rel='v:image']")
    poster_url = ""
    if poster:
        poster_url = poster.get("src") or poster.get("data-src") or ""
    result["海报URL"] = absolute_url(poster_url) if poster_url else get_meta(soup, "og:image")

    # 有些字段在页面结构中不稳定，使用 meta 信息补充评分
    if result.get("评分") in ("", "无"):
        result["评分"] = get_text(soup.select_one("strong[property='v:average']")) or "无"

    return result


def movie_id_from_url(url: str) -> str:
    match = re.search(r"/subject/(\d+)", url)
    return match.group(1) if match else ""



# 短评
def parse_comments_page(
    html: str,
    movie: dict[str, Any]
) -> tuple[list[dict[str, Any]], str | None]:
    soup = make_soup(html)
    records: list[dict[str, Any]] = []

    for node in soup.select(".comment-item"):
        try:
            user_node = node.select_one(".comment-info a")
            content_node = node.select_one(".comment .short")
            time_node = node.select_one(".comment-time")
            votes_node = node.select_one(".votes")
            rating_node = node.select_one(".comment-info span[class*='rating']")

            user_url = absolute_url(user_node.get("href")) if user_node else ""
            comment_id = node.get("data-cid", "")

            comment_link = ""
            if comment_id:
                comment_link = f"{movie['详情页URL']}#comment{comment_id}"

            records.append({
                "影片名": movie["影片名"],
                "影片链接": movie["详情页URL"],
                "评论ID": comment_id,
                "评论者": get_text(user_node) or "无",
                "评论者主页": user_url,
                "评分": "无",
                "评论时间": get_text(time_node) or "无",
                "有用数": get_text(votes_node) or "0",
                "评论内容": get_text(content_node) or "无",
                "评论链接": comment_link
            })

            if rating_node:
                records[-1]["评分"] = " ".join(rating_node.get("class", []))

        except Exception as exc:
            logger.warning("短评解析失败：%s", exc)

    next_node = soup.select_one("a.next")
    next_url = absolute_url(next_node.get("href")) if next_node and next_node.get("href") else None
    return records, next_url



# 长评
def parse_reviews_page(
    html: str,
    movie: dict[str, Any]
) -> tuple[list[dict[str, Any]], str | None]:
    soup = make_soup(html)
    records: list[dict[str, Any]] = []

    for node in soup.select(".review-item"):
        try:
            title_node = node.select_one("h2 a")
            author_node = node.select_one(".author")
            date_node = node.select_one(".main-meta")
            useful_node = node.select_one(".action a[data-count]")
            reply_node = node.select_one(".reply")
            rating_node = node.select_one(".main-title-rating")

            review_url = absolute_url(title_node.get("href")) if title_node else ""
            author_url = absolute_url(author_node.get("href")) if author_node else ""

            # 列表页通常只提供摘要；正文在单篇影评页
            content_node = node.select_one(".short-content")
            content = get_text(content_node) or get_text(node.select_one(".review-content"))

            records.append({
                "影片名": movie["影片名"],
                "影片链接": movie["详情页URL"],
                "影评标题": get_text(title_node) or "无",
                "作者": get_text(author_node) or "无",
                "作者主页": author_url,
                "评分": " ".join(rating_node.get("class", [])) if rating_node else "无",
                "发布时间": get_text(date_node) or "无",
                "有用数": useful_node.get("data-count", "0") if useful_node else "0",
                "回应数": get_text(reply_node) or "0",
                "影评正文": content or "无",
                "影评链接": review_url
            })
        except Exception as exc:
            logger.warning("长评解析失败：%s", exc)

    next_node = soup.select_one("a.next")
    next_url = absolute_url(next_node.get("href")) if next_node and next_node.get("href") else None
    return records, next_url


def parse_review_content(html: str) -> str:
    soup = make_soup(html)
    node = (
        soup.select_one(".review-content .review-content")
        or soup.select_one(".review-content")
        or soup.select_one("#link-report")
    )
    return get_text(node, "\n") if node else ""



# JSONL 缓存
def read_jsonl(path: Path, key: str | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
            except json.JSONDecodeError:
                continue
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]], append: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def merge_by_key(
    old_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
    key: str
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in old_rows + new_rows:
        value = str(row.get(key, "")).strip()
        if value:
            merged[value] = row
    return list(merged.values())



# 采集流程
def crawl_top250(
    pages: int | None,
    delay: float,
    refresh: bool = False
) -> list[dict[str, Any]]:
    cached = read_jsonl(MOVIE_JSONL)
    movies_by_id = {
        movie_id_from_url(row.get("详情页URL", "")): row
        for row in cached
        if movie_id_from_url(row.get("详情页URL", ""))
    }

    page_starts = list(range(0, 250, 25))
    if pages is not None:
        page_starts = page_starts[:max(1, pages)]

    for start in page_starts:
        logger.info("[Top250] 正在处理第 %s 页", start // 25 + 1)
        page_cache = LIST_CACHE / f"page_{start}.html"
        url = f"{TOP250_URL}?start={start}&filter="

        try:
            html = fetch_page(url, page_cache, delay, use_cache=not refresh)
            rows = parse_top250_page(html)
            if not rows:
                logger.warning("第 %s 页没有解析到电影，可能是页面结构变化", start // 25 + 1)
                continue

            for row in rows:
                movie_id = movie_id_from_url(row.get("详情页URL", ""))
                if not movie_id:
                    continue
                old = movies_by_id.get(movie_id, {})
                merged = {**old, **row}
                movies_by_id[movie_id] = merged

            logger.info("本页解析 %s 条，累计 %s 条", len(rows), len(movies_by_id))

        except Exception as exc:
            logger.error("列表页 %s 采集失败：%s", start // 25 + 1, exc)

    result = list(movies_by_id.values())
    result.sort(key=lambda x: x.get("排名") or 9999)

    # 列表页数据以完整快照方式保存
    write_jsonl(MOVIE_JSONL, result, append=False)
    return result


def detail_from_api(movie_id: str, delay: float) -> dict[str, Any]:
    """用手机 App 的 JSON 接口取影片详情（HTML 详情页现在会被反爬挡住）。

    字段名跟 parse_detail_page 的产物保持一致，后面不用改。
    """
    m = api.movie(movie_id, delay=delay)
    if not m:
        return {}
    return {
        "导演": m.get("导演") or "无",
        "主演": parse_people(m.get("主演") or "无"),
        "上映日期": m.get("上映日期") or "无",
        "年份": str(m.get("年份") or "无"),
        "片长": m.get("片长") or "无",
        "制片国家/地区": m.get("国家") or "无",
        "影片类型": m.get("类型") or "无",
        "又名": m.get("又名") or "无",
        "评分": str(m.get("评分") or "无"),
        "评价人数": str(m.get("评价人数") or "无"),
        "剧情简介": m.get("简介") or "无",
        "海报URL": m.get("海报") or "",
    }


def comments_from_api(movie: dict[str, Any], movie_id: str, per_movie: int,
                      delay: float) -> list[dict[str, Any]]:
    """短评：走 interests 接口。"""
    rows = []
    for it in api.interests(movie_id, count=per_movie, start=0, delay=delay):
        rows.append({
            "影片名": movie.get("影片名", ""),
            "影片链接": movie.get("详情页URL", ""),
            "评论ID": it.get("短评id", ""),
            "评论者": it.get("作者", ""),
            "评论者主页": it.get("作者主页", ""),
            "评分": it.get("评分", ""),
            "评论时间": it.get("时间", ""),
            "有用数": it.get("有用数", 0),
            "评论内容": it.get("内容", ""),
            "评论链接": f"{BASE_URL}/subject/{movie_id}/comments/",
        })
    return rows


def reviews_from_api(movie: dict[str, Any], movie_id: str, per_movie: int,
                     delay: float, full_content: bool = True) -> list[dict[str, Any]]:
    """长评：走 reviews 列表接口；需要正文再逐条取 review/{id}。"""
    data = api.get_json(
        f"{api.BASE}/movie/{movie_id}/reviews?count={per_movie}&start=0",
        f"reviews_{movie_id}_0_{per_movie}.json",
        delay=delay,
    )
    rows = []
    for r in (data or {}).get("reviews", []) or []:
        rid = str(r.get("id") or "")
        row = {
            "影片名": movie.get("影片名", ""),
            "影片链接": movie.get("详情页URL", ""),
            "影评标题": r.get("title", ""),
            "作者": ((r.get("user") or {}).get("name") or ""),
            "作者主页": ((r.get("user") or {}).get("url") or ""),
            "评分": (r.get("rating") or {}).get("value", ""),
            "发布时间": r.get("create_time", ""),
            "有用数": r.get("useful_count", 0),
            "回应数": r.get("comments_count", 0),
            "影评正文": r.get("abstract", ""),
            "影评链接": r.get("url", "") or f"{BASE_URL}/review/{rid}/",
        }
        if full_content and rid:
            info = api.review(rid, delay=delay)
            if info:
                text = api.html_to_text(info.get("正文HTML", ""))
                if text:
                    row["影评正文"] = text
        rows.append(row)
    return rows


def enrich_movie_details(
    movies: list[dict[str, Any]],
    delay: float,
    refresh: bool = False
) -> list[dict[str, Any]]:
    logger.info("开始补全电影详情，共 %s 部", len(movies))

    for index, movie in enumerate(movies, start=1):
        url = movie.get("详情页URL", "")
        movie_id = movie_id_from_url(url)
        if not movie_id:
            continue

        has_detail = movie.get("剧情简介") not in ("", "无") and movie.get("片长") not in ("", "无")

        if has_detail and not refresh:
            continue

        try:
            enriched = detail_from_api(movie_id, delay)
            if not enriched:
                raise RuntimeError("接口没返回数据")
            movie.update(enriched)
            logger.info("[%s/%s] 详情完成（API）：%s", index, len(movies), movie["影片名"])
        except Exception as exc:
            logger.warning("详情失败：%s - %s", movie.get("影片名"), exc)

    write_jsonl(MOVIE_JSONL, movies, append=False)
    return movies


def crawl_comments(
    movies: list[dict[str, Any]],
    per_movie: int,
    delay: float,
    refresh: bool = False
) -> list[dict[str, Any]]:
    if per_movie <= 0:
        return read_jsonl(COMMENT_JSONL)

    existing = read_jsonl(COMMENT_JSONL)
    by_id = {
        str(row.get("评论ID")): row
        for row in existing
        if row.get("评论ID")
    }

    for index, movie in enumerate(movies, start=1):
        movie_id = movie_id_from_url(movie.get("详情页URL", ""))
        if not movie_id:
            continue

        logger.info("[短评 %s/%s] %s", index, len(movies), movie["影片名"])
        try:
            rows = comments_from_api(movie, movie_id, per_movie, delay)
            for row in rows:
                comment_id = row.get("评论ID")
                if comment_id:
                    by_id[comment_id] = row
            logger.info("    短评 %s 条", len(rows))
        except Exception as exc:
            logger.warning("短评采集失败：%s - %s", movie["影片名"], exc)
            continue

    result = list(by_id.values())
    write_jsonl(COMMENT_JSONL, result, append=False)
    return result


def crawl_reviews(
    movies: list[dict[str, Any]],
    per_movie: int,
    delay: float,
    refresh: bool = False,
    full_content: bool = True
) -> list[dict[str, Any]]:
    if per_movie <= 0:
        return read_jsonl(REVIEW_JSONL)

    existing = read_jsonl(REVIEW_JSONL)
    by_url = {
        row.get("影评链接"): row
        for row in existing
        if row.get("影评链接")
    }

    for index, movie in enumerate(movies, start=1):
        movie_id = movie_id_from_url(movie.get("详情页URL", ""))
        if not movie_id:
            continue

        logger.info("[长评 %s/%s] %s", index, len(movies), movie["影片名"])
        try:
            rows = reviews_from_api(movie, movie_id, per_movie, delay, full_content)
            for row in rows:
                review_url = row.get("影评链接")
                if review_url:
                    by_url[review_url] = row
            logger.info("    长评 %s 条", len(rows))
        except Exception as exc:
            logger.warning("长评采集失败：%s - %s", movie["影片名"], exc)
            continue

    result = list(by_url.values())
    write_jsonl(REVIEW_JSONL, result, append=False)
    return result



# 图片下载
def download_posters(movies: list[dict[str, Any]], delay: float):
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    for movie in movies:
        url = movie.get("海报URL", "")
        if not url:
            continue

        movie_id = movie_id_from_url(movie.get("详情页URL", ""))
        if not movie_id:
            continue

        suffix = Path(urlparse(url).path).suffix or ".jpg"
        target = IMAGE_DIR / f"{movie_id}{suffix}"
        if target.exists() and target.stat().st_size > 0:
            continue

        try:
            response = SESSION.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            target.write_bytes(response.content)
            logger.info("海报已保存：%s", target.name)
            time.sleep(delay)
        except Exception as exc:
            logger.warning("海报下载失败：%s - %s", movie["影片名"], exc)



# Excel
def dataframe_with_columns(rows: list[dict[str, Any]], columns: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for column in columns:
        if column not in df.columns:
            df[column] = ""
    return df[columns]


def save_excel(
    movies: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    output: Path = OUTPUT_FILE
):
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.stem + ".tmp" + output.suffix)

    movie_df = dataframe_with_columns(movies, MOVIE_COLUMNS)
    comment_df = dataframe_with_columns(comments, COMMENT_COLUMNS)
    review_df = dataframe_with_columns(reviews, REVIEW_COLUMNS)

    with pd.ExcelWriter(temp, engine="openpyxl") as writer:
        movie_df.to_excel(writer, sheet_name="top250", index=False)
        comment_df.to_excel(writer, sheet_name="短评", index=False)
        review_df.to_excel(writer, sheet_name="长评", index=False)

        # 保留原脚本按每 25 部电影拆分工作表的习惯
        for page in range(10):
            page_df = movie_df.iloc[page * 25:(page + 1) * 25]
            if not page_df.empty:
                page_df.to_excel(writer, sheet_name=f"第{page + 1}页", index=False)

        for sheet in writer.book.worksheets:
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for column_cells in sheet.columns:
                max_length = max(
                    (len(str(cell.value)) for cell in column_cells if cell.value is not None),
                    default=10
                )
                sheet.column_dimensions[column_cells[0].column_letter].width = min(max(max_length + 2, 12), 45)

    temp.replace(output)
    logger.info("Excel 已保存：%s", output)


def load_excel(path: Path = OUTPUT_FILE) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not path.exists():
        return [], [], []

    try:
        book = pd.ExcelFile(path)
        movies = pd.read_excel(book, "top250").fillna("").to_dict("records") if "top250" in book.sheet_names else []
        comments = pd.read_excel(book, "短评").fillna("").to_dict("records") if "短评" in book.sheet_names else []
        reviews = pd.read_excel(book, "长评").fillna("").to_dict("records") if "长评" in book.sheet_names else []
        book.close()
        return movies, comments, reviews
    except Exception as exc:
        logger.warning("Excel 读取失败：%s", exc)
        return [], [], []



# 数据分析与图表
def split_multi_values(series: pd.Series) -> pd.Series:
    return (
        series.fillna("")
        .astype(str)
        .str.replace("、", " ", regex=False)
        .str.replace(",", " ", regex=False)
        .str.split(r"[\s/]+")
        .explode()
        .str.strip()
    )


def save_chart(chart, filename: str):
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    target = CHART_DIR / filename
    chart.render(str(target))
    logger.info("图表已生成：%s", target)


def analyze_all_countries(df: pd.DataFrame):
    from pyecharts.charts import Pie
    from pyecharts import options as opts

    countries = split_multi_values(df["制片国家/地区"])
    counts = countries[~countries.isin(["", "无", "nan"])].value_counts()

    if counts.empty:
        logger.warning("没有国家数据，跳过国家占比图")
        return

    chart = (
        Pie(init_opts=opts.InitOpts(width="1100px", height="650px"))
        .add("", [list(x) for x in zip(counts.index.tolist(), counts.tolist())],
             radius=["12%", "48%"], center=["45%", "52%"])
        .set_series_opts(label_opts=opts.LabelOpts(formatter="{b}: {c} ({d}%)"))
        .set_global_opts(
            title_opts=opts.TitleOpts(title="各国影片数量占比（Top250）"),
            legend_opts=opts.LegendOpts(
                orient="vertical", pos_top="8%", pos_left="85%", type_="scroll"
            )
        )
    )
    save_chart(chart, "各国占比.html")


def analyze_countries_by_types(df: pd.DataFrame):
    from pyecharts.charts import Bar, Grid
    from pyecharts import options as opts

    work = df[["制片国家/地区", "影片类型"]].copy()
    work["国家"] = split_multi_values(work["制片国家/地区"])
    work["类型"] = split_multi_values(work["影片类型"])
    work = work.explode("国家").explode("类型")
    work = work[~work["国家"].isin(["", "无", "nan"])]
    work = work[~work["类型"].isin(["", "无", "nan"])]

    if work.empty:
        logger.warning("没有国家/类型数据，跳过国家-类型图")
        return

    table = work.groupby(["国家", "类型"]).size().unstack(fill_value=0)
    chart = Bar()
    chart.add_xaxis(table.columns.astype(str).tolist())

    for country in table.index:
        chart.add_yaxis(str(country), table.loc[country].tolist(), stack="总量")

    chart.set_series_opts(label_opts=opts.LabelOpts(is_show=False))
    chart.set_global_opts(
        title_opts=opts.TitleOpts(title="各国在不同类型中的影片数"),
        xaxis_opts=opts.AxisOpts(name="影片类型", axislabel_opts={"rotate": 30, "interval": 0}),
        yaxis_opts=opts.AxisOpts(name="数量"),
        legend_opts=opts.LegendOpts(orient="vertical", pos_top="6%", pos_left="86%", type_="scroll"),
        tooltip_opts=opts.TooltipOpts(trigger="axis")
    )

    grid = Grid(init_opts=opts.InitOpts(width="1100px", height="620px"))
    grid.add(chart, grid_opts=opts.GridOpts(pos_left="7%", pos_right="22%", pos_bottom="18%"))
    save_chart(grid, "各国-类型.html")


def analyze_types_by_countries(df: pd.DataFrame):
    from pyecharts.charts import Bar, Grid
    from pyecharts import options as opts

    work = df[["制片国家/地区", "影片类型"]].copy()
    work["国家"] = split_multi_values(work["制片国家/地区"])
    work["类型"] = split_multi_values(work["影片类型"])
    work = work.explode("国家").explode("类型")
    work = work[~work["国家"].isin(["", "无", "nan"])]
    work = work[~work["类型"].isin(["", "无", "nan"])]

    if work.empty:
        logger.warning("没有国家/类型数据，跳过类型-国家图")
        return

    table = work.groupby(["国家", "类型"]).size().unstack(fill_value=0).T
    chart = Bar()
    chart.add_xaxis(table.columns.astype(str).tolist())

    for film_type in table.index:
        chart.add_yaxis(str(film_type), table.loc[film_type].tolist(), stack="总量")

    chart.set_series_opts(label_opts=opts.LabelOpts(is_show=False))
    chart.set_global_opts(
        title_opts=opts.TitleOpts(title="各类型影片的国家分布"),
        xaxis_opts=opts.AxisOpts(name="国家", axislabel_opts={"rotate": 45, "interval": 0}),
        yaxis_opts=opts.AxisOpts(name="数量"),
        legend_opts=opts.LegendOpts(orient="vertical", pos_top="6%", pos_left="86%", type_="scroll"),
        tooltip_opts=opts.TooltipOpts(trigger="axis")
    )

    grid = Grid(init_opts=opts.InitOpts(width="1100px", height="620px"))
    grid.add(chart, grid_opts=opts.GridOpts(pos_left="7%", pos_right="22%", pos_bottom="20%"))
    save_chart(grid, "类型-国家.html")


def analyze(movies: list[dict[str, Any]]):
    if not movies:
        logger.warning("没有电影数据，无法分析")
        return

    df = pd.DataFrame(movies)
    for column in ("制片国家/地区", "影片类型"):
        if column not in df.columns:
            df[column] = ""

    try:
        analyze_all_countries(df)
        analyze_countries_by_types(df)
        analyze_types_by_countries(df)
    except ImportError:
        logger.warning("未安装 pyecharts，跳过图表。安装：pip install pyecharts")



# 主程序
def main() -> int:
    parser = argparse.ArgumentParser(
        description="豆瓣 Top250 采集与分析工具"
    )
    parser.add_argument("--pages", type=int, default=None, help="抓取前 N 页，每页 25 部")
    parser.add_argument("--comments", type=int, default=0, help="每部电影最多抓取多少条短评，默认 0")
    parser.add_argument("--reviews", type=int, default=0, help="每部电影最多抓取多少篇长评，默认 0")
    parser.add_argument("--no-details", action="store_true", help="不抓详情页")
    parser.add_argument("--download-images", action="store_true", help="下载电影海报")
    parser.add_argument("--no-charts", action="store_true", help="不生成图表")
    parser.add_argument("--analyze-only", action="store_true", help="只读取本地数据并分析")
    parser.add_argument("--refresh", action="store_true", help="忽略 HTML 缓存，重新请求页面")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY, help="请求间隔秒数")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 部影片（调试用，0=全部）")
    parser.add_argument("--no-full-text", action="store_true", help="长评只存摘要，不抓正文（省大量请求）")
    parser.add_argument("--pause-every", type=int, default=0, help="每 N 次请求暂停一下（防限流）")
    parser.add_argument("--pause-sec", type=float, default=300, help="暂停多少秒")
    parser.add_argument("--out", type=Path, default=OUTPUT_FILE, help="Excel 输出路径")
    args = parser.parse_args()
    if args.pause_every:
        api.set_pacing(args.pause_every, args.pause_sec)
        logger.info("节流：每 %s 次请求暂停 %s 秒", args.pause_every, args.pause_sec)

    if args.analyze_only:
        movies, comments, reviews = load_excel(args.out)
        if not movies:
            movies = read_jsonl(MOVIE_JSONL)
        if not comments:
            comments = read_jsonl(COMMENT_JSONL)
        if not reviews:
            reviews = read_jsonl(REVIEW_JSONL)
    else:
        movies = crawl_top250(args.pages, args.delay, args.refresh)
        if args.limit:
            movies = movies[:args.limit]
            logger.info("--limit %s：只处理前 %s 部", args.limit, len(movies))

        if not args.no_details:
            movies = enrich_movie_details(movies, args.delay, args.refresh)

        comments = crawl_comments(
            movies, args.comments, args.delay, args.refresh
        )
        reviews = crawl_reviews(
            movies, args.reviews, args.delay, args.refresh,
            full_content=not args.no_full_text
        )

        if args.download_images:
            download_posters(movies, args.delay)

        # --limit 是调试用的，绝不能把正式数据表覆盖掉
        out_path = args.out
        if args.limit and out_path == OUTPUT_FILE:
            out_path = out_path.with_name(out_path.stem + "_limit.xlsx")
            logger.warning("用了 --limit，结果另存为 %s（不动正式的 response.xlsx）", out_path.name)
        save_excel(movies, comments, reviews, out_path)

    if not movies:
        logger.error("没有可用电影数据，请检查网络、缓存或豆瓣访问限制")
        return 1

    if not args.no_charts:
        analyze(movies)

    logger.info(
        "完成：电影 %s 部，短评 %s 条，长评 %s 篇",
        len(movies), len(comments), len(reviews)
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logger.warning("用户中断，已保存的数据缓存可供下次继续")
        raise SystemExit(130)
