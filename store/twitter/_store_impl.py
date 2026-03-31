# -*- coding: utf-8 -*-

import json
from typing import List, Dict

from sqlalchemy import select, update

from base.base_crawler import AbstractStore
from database.db_session import get_session
from database.models import TwitterTweet, TwitterTweetComment, TwitterCreator

from tools.async_file_writer import AsyncFileWriter
from tools.time_util import get_current_timestamp
from var import crawler_type_var
from database.mongodb_store_base import MongoDBStoreBase
from tools import utils
from store.excel_store_base import ExcelStoreBase


class TwitterCsvStoreImplement(AbstractStore):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.writer = AsyncFileWriter(platform="twitter", crawler_type=crawler_type_var.get())

    async def store_content(self, content_item: Dict):
        await self.writer.write_to_csv(item_type="contents", item=content_item)

    async def store_comment(self, comment_item: Dict):
        await self.writer.write_to_csv(item_type="comments", item=comment_item)

    async def store_creator(self, creator_item: Dict):
        await self.writer.write_to_csv(item_type="creators", item=creator_item)


class TwitterJsonStoreImplement(AbstractStore):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.writer = AsyncFileWriter(platform="twitter", crawler_type=crawler_type_var.get())

    async def store_content(self, content_item: Dict):
        await self.writer.write_single_item_to_json(item_type="contents", item=content_item)

    async def store_comment(self, comment_item: Dict):
        await self.writer.write_single_item_to_json(item_type="comments", item=comment_item)

    async def store_creator(self, creator_item: Dict):
        await self.writer.write_single_item_to_json(item_type="creators", item=creator_item)


class TwitterJsonlStoreImplement(AbstractStore):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.writer = AsyncFileWriter(platform="twitter", crawler_type=crawler_type_var.get())

    async def store_content(self, content_item: Dict):
        await self.writer.write_to_jsonl(item_type="contents", item=content_item)

    async def store_comment(self, comment_item: Dict):
        await self.writer.write_to_jsonl(item_type="comments", item=comment_item)

    async def store_creator(self, creator_item: Dict):
        await self.writer.write_to_jsonl(item_type="creators", item=creator_item)


class TwitterDbStoreImplement(AbstractStore):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def store_content(self, content_item: Dict):
        tweet_id = content_item.get("tweet_id")
        if not tweet_id:
            return
        async with get_session() as session:
            if await self._content_exists(session, tweet_id):
                await self._update_content(session, content_item)
            else:
                await self._add_content(session, content_item)

    async def _add_content(self, session, content_item: Dict):
        add_ts = int(get_current_timestamp())
        last_modify_ts = int(get_current_timestamp())
        tweet = TwitterTweet(
            tweet_id=content_item.get("tweet_id"),
            text=content_item.get("text"),
            user_id=content_item.get("user_id"),
            screen_name=content_item.get("screen_name"),
            nickname=content_item.get("nickname"),
            avatar=content_item.get("avatar"),
            like_count=str(content_item.get("like_count", 0)),
            retweet_count=str(content_item.get("retweet_count", 0)),
            reply_count=str(content_item.get("reply_count", 0)),
            quote_count=str(content_item.get("quote_count", 0)),
            bookmark_count=str(content_item.get("bookmark_count", 0)),
            created_at=content_item.get("created_at"),
            lang=content_item.get("lang"),
            media_urls=content_item.get("media_urls"),
            tweet_url=content_item.get("tweet_url"),
            source_keyword=content_item.get("source_keyword", ""),
            add_ts=add_ts,
            last_modify_ts=last_modify_ts,
        )
        session.add(tweet)

    async def _update_content(self, session, content_item: Dict):
        tweet_id = content_item.get("tweet_id")
        last_modify_ts = int(get_current_timestamp())
        update_data = {
            "last_modify_ts": last_modify_ts,
            "like_count": str(content_item.get("like_count", 0)),
            "retweet_count": str(content_item.get("retweet_count", 0)),
            "reply_count": str(content_item.get("reply_count", 0)),
            "quote_count": str(content_item.get("quote_count", 0)),
            "bookmark_count": str(content_item.get("bookmark_count", 0)),
        }
        stmt = update(TwitterTweet).where(TwitterTweet.tweet_id == tweet_id).values(**update_data)
        await session.execute(stmt)

    async def _content_exists(self, session, tweet_id: str) -> bool:
        stmt = select(TwitterTweet).where(TwitterTweet.tweet_id == tweet_id)
        result = await session.execute(stmt)
        return result.first() is not None

    async def store_comment(self, comment_item: Dict):
        if not comment_item:
            return
        async with get_session() as session:
            comment_id = comment_item.get("comment_id")
            if not comment_id:
                return
            if await self._comment_exists(session, comment_id):
                await self._update_comment(session, comment_item)
            else:
                await self._add_comment(session, comment_item)

    async def _add_comment(self, session, comment_item: Dict):
        add_ts = int(get_current_timestamp())
        last_modify_ts = int(get_current_timestamp())
        comment = TwitterTweetComment(
            comment_id=comment_item.get("comment_id"),
            tweet_id=comment_item.get("tweet_id"),
            content=comment_item.get("content"),
            user_id=comment_item.get("user_id"),
            screen_name=comment_item.get("screen_name"),
            nickname=comment_item.get("nickname"),
            avatar=comment_item.get("avatar"),
            like_count=str(comment_item.get("like_count", 0)),
            created_at=comment_item.get("created_at"),
            parent_comment_id=str(comment_item.get("parent_comment_id", "")),
            sub_comment_count=int(comment_item.get("sub_comment_count", 0) or 0),
            add_ts=add_ts,
            last_modify_ts=last_modify_ts,
        )
        session.add(comment)

    async def _update_comment(self, session, comment_item: Dict):
        comment_id = comment_item.get("comment_id")
        last_modify_ts = int(get_current_timestamp())
        update_data = {
            "last_modify_ts": last_modify_ts,
            "like_count": str(comment_item.get("like_count", 0)),
            "sub_comment_count": int(comment_item.get("sub_comment_count", 0) or 0),
        }
        stmt = update(TwitterTweetComment).where(TwitterTweetComment.comment_id == comment_id).values(**update_data)
        await session.execute(stmt)

    async def _comment_exists(self, session, comment_id: str) -> bool:
        stmt = select(TwitterTweetComment).where(TwitterTweetComment.comment_id == comment_id)
        result = await session.execute(stmt)
        return result.first() is not None

    async def store_creator(self, creator_item: Dict):
        user_id = creator_item.get("user_id")
        if not user_id:
            return
        async with get_session() as session:
            if await self._creator_exists(session, user_id):
                await self._update_creator(session, creator_item)
            else:
                await self._add_creator(session, creator_item)

    async def _add_creator(self, session, creator_item: Dict):
        add_ts = int(get_current_timestamp())
        last_modify_ts = int(get_current_timestamp())
        creator = TwitterCreator(
            user_id=creator_item.get("user_id"),
            screen_name=creator_item.get("screen_name"),
            nickname=creator_item.get("nickname"),
            avatar=creator_item.get("avatar"),
            desc=creator_item.get("desc"),
            location=creator_item.get("location"),
            followers_count=str(creator_item.get("followers_count", 0)),
            following_count=str(creator_item.get("following_count", 0)),
            tweet_count=str(creator_item.get("tweet_count", 0)),
            listed_count=str(creator_item.get("listed_count", 0)),
            verified=1 if creator_item.get("verified") else 0,
            created_at=creator_item.get("created_at"),
            add_ts=add_ts,
            last_modify_ts=last_modify_ts,
        )
        session.add(creator)

    async def _update_creator(self, session, creator_item: Dict):
        user_id = creator_item.get("user_id")
        last_modify_ts = int(get_current_timestamp())
        update_data = {
            "last_modify_ts": last_modify_ts,
            "nickname": creator_item.get("nickname"),
            "avatar": creator_item.get("avatar"),
            "desc": creator_item.get("desc"),
            "followers_count": str(creator_item.get("followers_count", 0)),
            "following_count": str(creator_item.get("following_count", 0)),
            "tweet_count": str(creator_item.get("tweet_count", 0)),
        }
        stmt = update(TwitterCreator).where(TwitterCreator.user_id == user_id).values(**update_data)
        await session.execute(stmt)

    async def _creator_exists(self, session, user_id: str) -> bool:
        stmt = select(TwitterCreator).where(TwitterCreator.user_id == user_id)
        result = await session.execute(stmt)
        return result.first() is not None


class TwitterSqliteStoreImplement(TwitterDbStoreImplement):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)


class TwitterMongoStoreImplement(AbstractStore):
    """X平台 MongoDB 存储实现"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.mongo_store = MongoDBStoreBase(collection_prefix="twitter")

    async def store_content(self, content_item: Dict):
        tweet_id = content_item.get("tweet_id")
        if not tweet_id:
            return
        await self.mongo_store.save_or_update(
            collection_suffix="contents",
            query={"tweet_id": tweet_id},
            data=content_item,
        )
        utils.logger.info(f"[TwitterMongoStoreImplement.store_content] Saved tweet {tweet_id} to MongoDB")

    async def store_comment(self, comment_item: Dict):
        comment_id = comment_item.get("comment_id")
        if not comment_id:
            return
        await self.mongo_store.save_or_update(
            collection_suffix="comments",
            query={"comment_id": comment_id},
            data=comment_item,
        )
        utils.logger.info(f"[TwitterMongoStoreImplement.store_comment] Saved comment {comment_id} to MongoDB")

    async def store_creator(self, creator_item: Dict):
        user_id = creator_item.get("user_id")
        if not user_id:
            return
        await self.mongo_store.save_or_update(
            collection_suffix="creators",
            query={"user_id": user_id},
            data=creator_item,
        )
        utils.logger.info(f"[TwitterMongoStoreImplement.store_creator] Saved creator {user_id} to MongoDB")


class TwitterExcelStoreImplement:
    """X平台 Excel 存储实现 - 全局单例"""

    def __new__(cls, *args, **kwargs):
        from store.excel_store_base import ExcelStoreBase
        return ExcelStoreBase.get_instance(
            platform="twitter",
            crawler_type=crawler_type_var.get(),
        )
