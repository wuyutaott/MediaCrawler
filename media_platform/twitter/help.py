# -*- coding: utf-8 -*-

import re
from datetime import datetime, timezone, tzinfo
from typing import Dict, List, Optional

from model.m_twitter import TweetUrlInfo, CreatorUrlInfo


def parse_tweet_created_at(created_at: str) -> Optional[datetime]:
    """
    解析X推文的created_at时间字符串
    格式: "Sun Sep 21 17:17:54 +0000 2025"
    """
    try:
        return datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
    except (ValueError, TypeError):
        return None


def is_tweet_in_date_range(created_at: str, since_date: Optional[str] = None, until_date: Optional[str] = None) -> bool:
    """
    判断推文是否在指定日期范围内
    Args:
        created_at: 推文的created_at字符串
        since_date: 起始日期 (含)，格式 "YYYY-MM-DD"
        until_date: 结束日期 (含)，格式 "YYYY-MM-DD"
    """
    if not since_date and not until_date:
        return True

    dt = parse_tweet_created_at(created_at)
    if not dt:
        return True  # 无法解析时不过滤

    # 将UTC时间转换为本地时区再取日期
    tweet_date = dt.astimezone().date()

    if since_date:
        since = datetime.strptime(since_date, "%Y-%m-%d").date()
        if tweet_date < since:
            return False

    if until_date:
        until = datetime.strptime(until_date, "%Y-%m-%d").date()
        if tweet_date > until:
            return False

    return True


def is_tweet_before_date(created_at: str, since_date: str) -> bool:
    """判断推文是否早于指定日期（用于提前停止爬取）"""
    dt = parse_tweet_created_at(created_at)
    if not dt:
        return False
    since = datetime.strptime(since_date, "%Y-%m-%d").date()
    return dt.astimezone().date() < since


def parse_tweet_info_from_url(url: str) -> TweetUrlInfo:
    """
    从X推文URL中解析推文信息
    Args:
        url: "https://x.com/elonmusk/status/1234567890123456789"
             或纯推文ID "1234567890123456789"
    Returns:
        TweetUrlInfo
    """
    # 纯数字ID
    if url.isdigit():
        return TweetUrlInfo(tweet_id=url)

    # 从URL中提取 status/<tweet_id>
    match = re.search(r'/status/(\d+)', url)
    if match:
        return TweetUrlInfo(tweet_id=match.group(1))

    raise ValueError(f"无法从URL中解析推文ID: {url}")


def parse_creator_info_from_url(url: str) -> CreatorUrlInfo:
    """
    从X创作者主页URL中解析创作者信息
    支持以下格式:
    1. 完整URL: "https://x.com/elonmusk"
    2. 纯用户名: "elonmusk"
    3. 带@: "@elonmusk"

    Args:
        url: 创作者主页URL或用户名
    Returns:
        CreatorUrlInfo
    """
    # 去掉@前缀
    if url.startswith("@"):
        return CreatorUrlInfo(screen_name=url[1:])

    # 纯用户名（无斜杠、无协议）
    if "/" not in url and ":" not in url:
        return CreatorUrlInfo(screen_name=url)

    # 从URL中提取用户名: https://x.com/<screen_name> 或 https://twitter.com/<screen_name>
    match = re.search(r'(?:x\.com|twitter\.com)/([^/?]+)', url)
    if match:
        screen_name = match.group(1)
        # 排除X平台的保留路径
        if screen_name not in ("i", "search", "explore", "notifications", "messages", "settings", "home"):
            return CreatorUrlInfo(screen_name=screen_name)

    raise ValueError(f"无法从URL中解析创作者信息: {url}")


def extract_tweet_from_result(entry: Dict) -> Optional[Dict]:
    """
    从X GraphQL API返回的嵌套结构中提取推文数据
    路径: entry -> content -> itemContent -> tweet_results -> result -> legacy
    """
    try:
        content = entry.get("content", {})
        # itemContent 可能直接在 content 下，也可能在 entry 本身
        item_content = content.get("itemContent") or entry.get("itemContent")
        if not item_content:
            return None

        tweet_results = item_content.get("tweet_results", {})
        result = tweet_results.get("result", {})

        # 处理可能的 TweetWithVisibilityResults 包装
        if result.get("__typename") == "TweetWithVisibilityResults":
            result = result.get("tweet", {})

        if not result or result.get("__typename") not in ("Tweet", None):
            return None

        legacy = result.get("legacy", {})
        tweet_core = result.get("core", {})
        user_results = tweet_core.get("user_results", {}).get("result", {})
        user_legacy = user_results.get("legacy", {})
        user_core = user_results.get("core", {})
        user_avatar = user_results.get("avatar", {})

        screen_name = user_core.get("screen_name", "") or user_legacy.get("screen_name", "")
        tweet_id = legacy.get("id_str") or result.get("rest_id", "")

        return {
            "tweet_id": tweet_id,
            "text": legacy.get("full_text", ""),
            "user_id": user_results.get("rest_id", ""),
            "screen_name": screen_name,
            "nickname": user_core.get("name", "") or user_legacy.get("name", ""),
            "avatar": user_avatar.get("image_url", "") or user_legacy.get("profile_image_url_https", ""),
            "like_count": legacy.get("favorite_count", 0),
            "retweet_count": legacy.get("retweet_count", 0),
            "reply_count": legacy.get("reply_count", 0),
            "quote_count": legacy.get("quote_count", 0),
            "bookmark_count": legacy.get("bookmark_count", 0),
            "created_at": legacy.get("created_at", ""),
            "lang": legacy.get("lang", ""),
            "media_urls": _extract_media_urls(legacy),
            "tweet_url": f"https://x.com/{screen_name}/status/{tweet_id}",
        }
    except (KeyError, TypeError, AttributeError):
        return None


def extract_user_from_result(user_result: Dict) -> Optional[Dict]:
    """
    从X GraphQL API返回的用户结构中提取用户信息
    实际结构:
      - screen_name, name, created_at 在 user_result["core"] 中
      - avatar 在 user_result["avatar"]["image_url"] 中
      - location 在 user_result["location"]["location"] 中
      - followers_count 等统计数据在 user_result["legacy"] 中
    """
    try:
        legacy = user_result.get("legacy", {})
        core = user_result.get("core", {})
        avatar_obj = user_result.get("avatar", {})
        location_obj = user_result.get("location", {})
        profile_bio = user_result.get("profile_bio", {})

        return {
            "user_id": user_result.get("rest_id", ""),
            "screen_name": core.get("screen_name", "") or legacy.get("screen_name", ""),
            "nickname": core.get("name", "") or legacy.get("name", ""),
            "avatar": avatar_obj.get("image_url", "") or legacy.get("profile_image_url_https", ""),
            "desc": profile_bio.get("description", "") or legacy.get("description", ""),
            "location": location_obj.get("location", "") or legacy.get("location", ""),
            "followers_count": legacy.get("followers_count", 0),
            "following_count": legacy.get("friends_count", 0),
            "tweet_count": legacy.get("statuses_count", 0),
            "listed_count": legacy.get("listed_count", 0),
            "verified": user_result.get("is_blue_verified", False),
            "created_at": core.get("created_at", "") or legacy.get("created_at", ""),
        }
    except (KeyError, TypeError, AttributeError):
        return None


def _extract_media_urls(legacy: Dict) -> List[str]:
    """从推文legacy数据中提取媒体URL列表"""
    media_urls = []
    entities = legacy.get("extended_entities") or legacy.get("entities", {})
    media_list = entities.get("media", [])
    for media in media_list:
        media_type = media.get("type", "")
        if media_type == "photo":
            media_urls.append(media.get("media_url_https", ""))
        elif media_type in ("video", "animated_gif"):
            variants = media.get("video_info", {}).get("variants", [])
            # 选择最高码率的MP4
            mp4_variants = [v for v in variants if v.get("content_type") == "video/mp4"]
            if mp4_variants:
                best = max(mp4_variants, key=lambda v: v.get("bitrate", 0))
                media_urls.append(best.get("url", ""))
    return media_urls
