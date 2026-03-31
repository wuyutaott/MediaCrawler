# -*- coding: utf-8 -*-

from enum import Enum
from typing import NamedTuple


class SearchSortType(Enum):
    """搜索排序方式"""
    TOP = "Top"
    LATEST = "Latest"
    PEOPLE = "People"
    MEDIA = "Media"


class TweetType(Enum):
    """推文类型"""
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    GIF = "gif"


class Tweet(NamedTuple):
    """推文数据结构"""
    tweet_id: str
    text: str
    user_id: str
    screen_name: str
    nickname: str
    avatar: str
    like_count: int
    retweet_count: int
    reply_count: int
    quote_count: int
    bookmark_count: int
    created_at: str
    lang: str
    media_urls: list
