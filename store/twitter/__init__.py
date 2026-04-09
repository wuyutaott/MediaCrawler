# -*- coding: utf-8 -*-
from typing import List, Dict

import config
from var import source_keyword_var
from tools import utils

from ._store_impl import *


class TwitterStoreFactory:
    STORES = {
        "csv": TwitterCsvStoreImplement,
        "db": TwitterDbStoreImplement,
        "postgres": TwitterDbStoreImplement,
        "json": TwitterJsonStoreImplement,
        "jsonl": TwitterJsonlStoreImplement,
        "sqlite": TwitterSqliteStoreImplement,
        "mongodb": TwitterMongoStoreImplement,
        "excel": TwitterExcelStoreImplement,
    }

    @staticmethod
    def create_store() -> AbstractStore:
        store_class = TwitterStoreFactory.STORES.get(config.SAVE_DATA_OPTION)
        if not store_class:
            raise ValueError(
                "[TwitterStoreFactory.create_store] Invalid save option only supported csv or db or json or jsonl or sqlite or mongodb or excel ..."
            )
        return store_class()


async def update_twitter_tweet(tweet_item: Dict):
    """
    更新X平台推文
    """
    tweet_id = tweet_item.get("tweet_id")
    local_db_item = {
        "tweet_id": tweet_item.get("tweet_id"),
        "text": tweet_item.get("text", ""),
        "user_id": tweet_item.get("user_id", ""),
        "screen_name": tweet_item.get("screen_name", ""),
        "nickname": tweet_item.get("nickname", ""),
        "avatar": tweet_item.get("avatar", ""),
        "like_count": tweet_item.get("like_count", 0),
        "retweet_count": tweet_item.get("retweet_count", 0),
        "reply_count": tweet_item.get("reply_count", 0),
        "quote_count": tweet_item.get("quote_count", 0),
        "bookmark_count": tweet_item.get("bookmark_count", 0),
        "created_at": tweet_item.get("created_at", ""),
        "lang": tweet_item.get("lang", ""),
        "media_urls": ",".join(tweet_item.get("media_urls", [])),
        "tweet_url": tweet_item.get("tweet_url", f"https://x.com/{tweet_item.get('screen_name', '')}/status/{tweet_id}"),
        "source_keyword": source_keyword_var.get(),
        "reply_policy": tweet_item.get("reply_policy", ""),
        "quoted_tweet_id": tweet_item.get("quoted_tweet_id", ""),
        "quoted_tweet_text": tweet_item.get("quoted_tweet_text", ""),
        "quoted_tweet_screen_name": tweet_item.get("quoted_tweet_screen_name", ""),
        "quoted_tweet_url": tweet_item.get("quoted_tweet_url", ""),
        "last_modify_ts": utils.get_current_timestamp(),
    }
    utils.logger.debug(f"[store.twitter.update_twitter_tweet] twitter tweet: {local_db_item}")
    await TwitterStoreFactory.create_store().store_content(local_db_item)


async def batch_update_twitter_tweet_comments(tweet_id: str, comments: List[Dict]):
    """
    批量更新X平台推文评论
    """
    if not comments:
        return
    for comment_item in comments:
        await update_twitter_tweet_comment(tweet_id, comment_item)


async def update_twitter_tweet_comment(tweet_id: str, comment_item: Dict):
    """
    更新X平台推文评论
    """
    local_db_item = {
        "comment_id": comment_item.get("tweet_id", ""),
        "tweet_id": tweet_id,
        "content": comment_item.get("text", ""),
        "user_id": comment_item.get("user_id", ""),
        "screen_name": comment_item.get("screen_name", ""),
        "nickname": comment_item.get("nickname", ""),
        "avatar": comment_item.get("avatar", ""),
        "like_count": comment_item.get("like_count", 0),
        "created_at": comment_item.get("created_at", ""),
        "parent_comment_id": comment_item.get("parent_tweet_id", tweet_id),
        "sub_comment_count": comment_item.get("reply_count", 0),
        "last_modify_ts": utils.get_current_timestamp(),
    }
    utils.logger.debug(f"[store.twitter.update_twitter_tweet_comment] twitter comment: {local_db_item}")
    await TwitterStoreFactory.create_store().store_comment(local_db_item)


async def save_creator(user_id: str, creator: Dict):
    """
    保存X平台创作者信息
    """
    local_db_item = {
        "user_id": user_id,
        "screen_name": creator.get("screen_name", ""),
        "nickname": creator.get("nickname", ""),
        "avatar": creator.get("avatar", ""),
        "desc": creator.get("desc", ""),
        "location": creator.get("location", ""),
        "followers_count": creator.get("followers_count", 0),
        "following_count": creator.get("following_count", 0),
        "tweet_count": creator.get("tweet_count", 0),
        "listed_count": creator.get("listed_count", 0),
        "verified": creator.get("verified", False),
        "created_at": creator.get("created_at", ""),
        "last_modify_ts": utils.get_current_timestamp(),
    }
    utils.logger.debug(f"[store.twitter.save_creator] creator: {local_db_item}")
    await TwitterStoreFactory.create_store().store_creator(local_db_item)
