# -*- coding: utf-8 -*-

from pydantic import BaseModel, Field


class TweetUrlInfo(BaseModel):
    """X (Twitter) 推文URL信息"""
    tweet_id: str = Field(title="tweet id")


class CreatorUrlInfo(BaseModel):
    """X (Twitter) 创作者URL信息"""
    screen_name: str = Field(title="screen name")
    user_id: str = Field(default="", title="user id")
