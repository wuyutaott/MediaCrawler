# -*- coding: utf-8 -*-

import re
from typing import Dict, List, Optional

from model.m_twitter import TweetUrlInfo, CreatorUrlInfo


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
        item_content = content.get("itemContent") or content.get("entryType") and content
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
        core = result.get("core", {})
        user_results = core.get("user_results", {}).get("result", {})
        user_legacy = user_results.get("legacy", {})

        return {
            "tweet_id": legacy.get("id_str") or result.get("rest_id", ""),
            "text": legacy.get("full_text", ""),
            "user_id": user_legacy.get("id_str") or user_results.get("rest_id", ""),
            "screen_name": user_legacy.get("screen_name", ""),
            "nickname": user_legacy.get("name", ""),
            "avatar": user_legacy.get("profile_image_url_https", ""),
            "like_count": legacy.get("favorite_count", 0),
            "retweet_count": legacy.get("retweet_count", 0),
            "reply_count": legacy.get("reply_count", 0),
            "quote_count": legacy.get("quote_count", 0),
            "bookmark_count": legacy.get("bookmark_count", 0),
            "created_at": legacy.get("created_at", ""),
            "lang": legacy.get("lang", ""),
            "media_urls": _extract_media_urls(legacy),
            "tweet_url": f"https://x.com/{user_legacy.get('screen_name', '')}/status/{legacy.get('id_str') or result.get('rest_id', '')}",
        }
    except (KeyError, TypeError, AttributeError):
        return None


def extract_user_from_result(user_result: Dict) -> Optional[Dict]:
    """
    从X GraphQL API返回的用户结构中提取用户信息
    """
    try:
        legacy = user_result.get("legacy", {})
        return {
            "user_id": user_result.get("rest_id", ""),
            "screen_name": legacy.get("screen_name", ""),
            "nickname": legacy.get("name", ""),
            "avatar": legacy.get("profile_image_url_https", ""),
            "desc": legacy.get("description", ""),
            "location": legacy.get("location", ""),
            "followers_count": legacy.get("followers_count", 0),
            "following_count": legacy.get("friends_count", 0),
            "tweet_count": legacy.get("statuses_count", 0),
            "listed_count": legacy.get("listed_count", 0),
            "verified": user_result.get("is_blue_verified", False),
            "created_at": legacy.get("created_at", ""),
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
