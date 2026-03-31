# -*- coding: utf-8 -*-

import asyncio
import os
import random
from asyncio import Task
from typing import Dict, List, Optional

from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Page,
    Playwright,
    async_playwright,
)
from tenacity import RetryError

import config
from base.base_crawler import AbstractCrawler
from model.m_twitter import TweetUrlInfo, CreatorUrlInfo
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import twitter as twitter_store
from tools import utils
from tools.cdp_browser import CDPBrowserManager
from var import crawler_type_var, source_keyword_var

from .client import TwitterClient
from .exception import DataFetchError, TweetNotFoundError
from .field import SearchSortType
from .help import parse_tweet_info_from_url, parse_creator_info_from_url
from .login import TwitterLogin


class TwitterCrawler(AbstractCrawler):
    context_page: Page
    twitter_client: TwitterClient
    browser_context: BrowserContext
    cdp_manager: Optional[CDPBrowserManager]

    def __init__(self) -> None:
        self.index_url = "https://x.com"
        self.user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        self.cdp_manager = None
        self.ip_proxy_pool = None

    async def start(self) -> None:
        playwright_proxy_format, httpx_proxy_format = None, None
        if config.ENABLE_IP_PROXY:
            self.ip_proxy_pool = await create_ip_pool(config.IP_PROXY_POOL_COUNT, enable_validate_ip=True)
            ip_proxy_info: IpInfoModel = await self.ip_proxy_pool.get_proxy()
            playwright_proxy_format, httpx_proxy_format = utils.format_proxy_info(ip_proxy_info)

        async with async_playwright() as playwright:
            # 根据配置选择启动模式
            if config.ENABLE_CDP_MODE:
                utils.logger.info("[TwitterCrawler] 使用CDP模式启动浏览器")
                self.browser_context = await self.launch_browser_with_cdp(
                    playwright,
                    playwright_proxy_format,
                    self.user_agent,
                    headless=config.CDP_HEADLESS,
                )
            else:
                utils.logger.info("[TwitterCrawler] 使用标准模式启动浏览器")
                chromium = playwright.chromium
                self.browser_context = await self.launch_browser(
                    chromium,
                    playwright_proxy_format,
                    self.user_agent,
                    headless=config.HEADLESS,
                )
                await self.browser_context.add_init_script(path="libs/stealth.min.js")

            self.context_page = await self.browser_context.new_page()
            await self.context_page.goto(self.index_url)

            # 创建API客户端
            self.twitter_client = await self.create_twitter_client(httpx_proxy_format)
            if not await self.twitter_client.pong():
                login_obj = TwitterLogin(
                    login_type=config.LOGIN_TYPE,
                    login_phone="",
                    browser_context=self.browser_context,
                    context_page=self.context_page,
                    cookie_str=config.COOKIES,
                )
                await login_obj.begin()
                await self.twitter_client.update_cookies(browser_context=self.browser_context)

            crawler_type_var.set(config.CRAWLER_TYPE)
            if config.CRAWLER_TYPE == "search":
                await self.search()
            elif config.CRAWLER_TYPE == "detail":
                await self.get_specified_tweets()
            elif config.CRAWLER_TYPE == "creator":
                await self.get_creators_and_tweets()

            utils.logger.info("[TwitterCrawler.start] Twitter Crawler 完成 ...")

    async def search(self) -> None:
        """搜索推文并获取评论"""
        utils.logger.info("[TwitterCrawler.search] 开始搜索X平台关键词")
        for keyword in config.KEYWORDS.split(","):
            source_keyword_var.set(keyword)
            utils.logger.info(f"[TwitterCrawler.search] 当前搜索关键词: {keyword}")
            cursor = ""
            tweets_collected = 0
            sort_type = SearchSortType(config.TWITTER_SORT_TYPE) if config.TWITTER_SORT_TYPE else SearchSortType.TOP

            while tweets_collected < config.CRAWLER_MAX_NOTES_COUNT:
                try:
                    utils.logger.info(f"[TwitterCrawler.search] 搜索关键词: {keyword}, 已收集: {tweets_collected}")
                    tweets_res = await self.twitter_client.search_tweets(
                        keyword=keyword,
                        cursor=cursor,
                        sort=sort_type,
                    )

                    if not tweets_res or not tweets_res.get("tweets"):
                        utils.logger.info("[TwitterCrawler.search] 没有更多内容!")
                        break

                    tweet_ids = []
                    semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
                    task_list = [
                        self.get_tweet_detail_async_task(
                            tweet_id=tweet.get("tweet_id"),
                            semaphore=semaphore,
                        ) for tweet in tweets_res.get("tweets", [])
                    ]
                    tweet_details = await asyncio.gather(*task_list)
                    for tweet_detail in tweet_details:
                        if tweet_detail:
                            await twitter_store.update_twitter_tweet(tweet_detail)
                            tweet_ids.append(tweet_detail.get("tweet_id"))

                    tweets_collected += len(tweet_ids)
                    utils.logger.info(f"[TwitterCrawler.search] 推文详情: 获取 {len(tweet_ids)} 条")
                    await self.batch_get_tweet_comments(tweet_ids)

                    if not tweets_res.get("has_more"):
                        break
                    cursor = tweets_res.get("cursor", "")

                    await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)

                except DataFetchError as e:
                    utils.logger.error(f"[TwitterCrawler.search] 获取推文详情错误: {e}")
                    break

    async def get_specified_tweets(self):
        """获取指定推文的信息和评论"""
        get_tweet_detail_task_list = []
        for tweet_url in config.TWITTER_SPECIFIED_TWEET_URL_LIST:
            tweet_url_info: TweetUrlInfo = parse_tweet_info_from_url(tweet_url)
            utils.logger.info(f"[TwitterCrawler.get_specified_tweets] 解析推文URL: {tweet_url_info}")
            crawler_task = self.get_tweet_detail_async_task(
                tweet_id=tweet_url_info.tweet_id,
                semaphore=asyncio.Semaphore(config.MAX_CONCURRENCY_NUM),
            )
            get_tweet_detail_task_list.append(crawler_task)

        need_get_comment_tweet_ids = []
        tweet_details = await asyncio.gather(*get_tweet_detail_task_list)
        for tweet_detail in tweet_details:
            if tweet_detail:
                need_get_comment_tweet_ids.append(tweet_detail.get("tweet_id", ""))
                await twitter_store.update_twitter_tweet(tweet_detail)
        await self.batch_get_tweet_comments(need_get_comment_tweet_ids)

    async def get_creators_and_tweets(self) -> None:
        """获取创作者信息及其推文和评论"""
        utils.logger.info("[TwitterCrawler.get_creators_and_tweets] 开始获取X平台创作者信息")
        for creator_url in config.TWITTER_CREATOR_ID_LIST:
            try:
                creator_info: CreatorUrlInfo = parse_creator_info_from_url(creator_url)
                utils.logger.info(f"[TwitterCrawler.get_creators_and_tweets] 解析创作者URL: {creator_info}")
                screen_name = creator_info.screen_name

                # 获取用户资料
                user_profile = await self.twitter_client.get_user_profile(screen_name=screen_name)
                if user_profile:
                    await twitter_store.save_creator(user_profile.get("user_id", ""), creator=user_profile)
                    user_id = user_profile.get("user_id", "")
                else:
                    utils.logger.error(f"[TwitterCrawler.get_creators_and_tweets] 无法获取用户资料: {screen_name}")
                    continue

            except ValueError as e:
                utils.logger.error(f"[TwitterCrawler.get_creators_and_tweets] 解析创作者URL失败: {e}")
                continue

            crawl_interval = config.CRAWLER_MAX_SLEEP_SEC
            all_tweets = await self.twitter_client.get_all_tweets_by_creator(
                user_id=user_id,
                crawl_interval=crawl_interval,
                callback=self.fetch_creator_tweets_detail,
            )

            tweet_ids = [tweet.get("tweet_id") for tweet in all_tweets if tweet.get("tweet_id")]
            await self.batch_get_tweet_comments(tweet_ids)

    async def fetch_creator_tweets_detail(self, tweet_list: List[Dict]):
        """并发获取指定推文列表并保存数据"""
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [
            self.get_tweet_detail_async_task(
                tweet_id=tweet.get("tweet_id"),
                semaphore=semaphore,
            ) for tweet in tweet_list
        ]

        tweet_details = await asyncio.gather(*task_list)
        for tweet_detail in tweet_details:
            if tweet_detail:
                await twitter_store.update_twitter_tweet(tweet_detail)

    async def get_tweet_detail_async_task(
        self,
        tweet_id: str,
        semaphore: asyncio.Semaphore,
    ) -> Optional[Dict]:
        """获取推文详情"""
        tweet_detail = None
        utils.logger.info(f"[TwitterCrawler.get_tweet_detail_async_task] 开始获取推文详情, tweet_id: {tweet_id}")
        async with semaphore:
            try:
                tweet_detail = await self.twitter_client.get_tweet_by_id(tweet_id)
                if not tweet_detail:
                    utils.logger.warning(f"[TwitterCrawler.get_tweet_detail_async_task] 推文不存在: {tweet_id}")
                    return None

                await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                return tweet_detail

            except TweetNotFoundError as ex:
                utils.logger.warning(f"[TwitterCrawler.get_tweet_detail_async_task] 推文未找到: {tweet_id}, {ex}")
                return None
            except DataFetchError as ex:
                utils.logger.error(f"[TwitterCrawler.get_tweet_detail_async_task] 获取推文详情错误: {ex}")
                return None
            except KeyError as ex:
                utils.logger.error(f"[TwitterCrawler.get_tweet_detail_async_task] 推文详情解析错误 tweet_id:{tweet_id}, err: {ex}")
                return None

    async def batch_get_tweet_comments(self, tweet_ids: List[str]):
        """批量获取推文评论"""
        if not config.ENABLE_GET_COMMENTS:
            utils.logger.info("[TwitterCrawler.batch_get_tweet_comments] 评论抓取模式未开启")
            return

        utils.logger.info(f"[TwitterCrawler.batch_get_tweet_comments] 开始批量获取推文评论, 推文列表: {tweet_ids}")
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list: List[Task] = []
        for tweet_id in tweet_ids:
            task = asyncio.create_task(
                self.get_comments(tweet_id=tweet_id, semaphore=semaphore),
                name=tweet_id,
            )
            task_list.append(task)
        await asyncio.gather(*task_list)

    async def get_comments(self, tweet_id: str, semaphore: asyncio.Semaphore):
        """获取推文评论"""
        async with semaphore:
            utils.logger.info(f"[TwitterCrawler.get_comments] 开始获取推文评论 {tweet_id}")
            crawl_interval = config.CRAWLER_MAX_SLEEP_SEC
            await self.twitter_client.get_all_tweet_comments(
                tweet_id=tweet_id,
                crawl_interval=crawl_interval,
                callback=twitter_store.batch_update_twitter_tweet_comments,
                max_count=config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES,
            )

            await asyncio.sleep(crawl_interval)

    async def create_twitter_client(self, httpx_proxy: Optional[str]) -> TwitterClient:
        """创建X平台API客户端"""
        utils.logger.info("[TwitterCrawler.create_twitter_client] 开始创建X平台API客户端 ...")
        cookie_str, cookie_dict = utils.convert_cookies(await self.browser_context.cookies())
        twitter_client_obj = TwitterClient(
            proxy=httpx_proxy,
            headers={
                "accept": "*/*",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                "cache-control": "no-cache",
                "content-type": "application/json",
                "origin": "https://x.com",
                "pragma": "no-cache",
                "referer": "https://x.com/",
                "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not.A/Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"macOS"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "user-agent": self.user_agent,
                "Cookie": cookie_str,
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
            proxy_ip_pool=self.ip_proxy_pool,
        )
        return twitter_client_obj

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """启动浏览器并创建浏览器上下文"""
        utils.logger.info("[TwitterCrawler.launch_browser] 开始创建浏览器上下文 ...")
        if config.SAVE_LOGIN_STATE:
            user_data_dir = os.path.join(os.getcwd(), "browser_data", config.USER_DATA_DIR % config.PLATFORM)
            browser_context = await chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=headless,
                proxy=playwright_proxy,
                viewport={"width": 1920, "height": 1080},
                user_agent=user_agent,
            )
            return browser_context
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy)
            browser_context = await browser.new_context(viewport={"width": 1920, "height": 1080}, user_agent=user_agent)
            return browser_context

    async def launch_browser_with_cdp(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """使用CDP模式启动浏览器"""
        try:
            self.cdp_manager = CDPBrowserManager()
            browser_context = await self.cdp_manager.launch_and_connect(
                playwright=playwright,
                playwright_proxy=playwright_proxy,
                user_agent=user_agent,
                headless=headless,
            )

            browser_info = await self.cdp_manager.get_browser_info()
            utils.logger.info(f"[TwitterCrawler] CDP浏览器信息: {browser_info}")

            return browser_context

        except Exception as e:
            utils.logger.error(f"[TwitterCrawler] CDP模式启动失败，回退到标准模式: {e}")
            chromium = playwright.chromium
            return await self.launch_browser(chromium, playwright_proxy, user_agent, headless)

    async def close(self):
        """关闭浏览器上下文"""
        if self.cdp_manager:
            await self.cdp_manager.cleanup()
            self.cdp_manager = None
        else:
            await self.browser_context.close()
        utils.logger.info("[TwitterCrawler.close] 浏览器上下文已关闭 ...")
