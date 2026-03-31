# -*- coding: utf-8 -*-

from httpx import RequestError


class DataFetchError(RequestError):
    """数据获取错误"""


class IPBlockError(RequestError):
    """IP被封禁或请求频率过高"""


class TweetNotFoundError(RequestError):
    """推文不存在或已被删除"""
