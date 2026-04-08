# -*- coding: utf-8 -*-

import asyncio
import os
import time
from asyncio import Task
from datetime import date
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
        # 进度统计
        self._tweets_count = 0
        self._comments_count = 0
        self._start_time = 0.0

    async def start(self) -> None:
        self._start_time = time.time()
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

            # 从浏览器JS中动态发现GraphQL端点ID
            await self.twitter_client.discover_graphql_endpoints()

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
                # 登录后重新发现端点（页面JS可能已更新）
                await self.twitter_client.discover_graphql_endpoints()

            crawler_type_var.set(config.CRAWLER_TYPE)
            if config.CRAWLER_TYPE == "search":
                await self.search()
            elif config.CRAWLER_TYPE == "detail":
                await self.get_specified_tweets()
            elif config.CRAWLER_TYPE == "creator":
                await self.get_creators_and_tweets()

            utils.logger.info(f"[TwitterCrawler] 完成! 推文: {self._tweets_count}, 评论: {self._comments_count}, 总耗时: {self._elapsed()}")

    async def search(self) -> None:
        """搜索推文并获取评论"""
        for keyword in config.KEYWORDS.split(","):
            source_keyword_var.set(keyword)
            utils.logger.info(f"[TwitterCrawler] 搜索关键词: {keyword}")
            cursor = ""
            tweets_collected = 0
            sort_type = SearchSortType(config.TWITTER_SORT_TYPE) if config.TWITTER_SORT_TYPE else SearchSortType.TOP

            while tweets_collected < config.CRAWLER_MAX_NOTES_COUNT:
                try:
                    tweets_res = await self.twitter_client.search_tweets(
                        keyword=keyword,
                        cursor=cursor,
                        sort=sort_type,
                    )

                    if not tweets_res or not tweets_res.get("tweets"):
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
                    self._log_progress(f"搜索 \"{keyword}\"")
                    await self.batch_get_tweet_comments(tweet_ids)

                    if not tweets_res.get("has_more"):
                        break
                    cursor = tweets_res.get("cursor", "")

                    await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)

                except DataFetchError as e:
                    utils.logger.error(f"[TwitterCrawler] 搜索错误: {e}")
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
        for creator_url in config.TWITTER_CREATOR_ID_LIST:
            try:
                creator_info: CreatorUrlInfo = parse_creator_info_from_url(creator_url)
                screen_name = creator_info.screen_name
                utils.logger.info(f"[TwitterCrawler] 开始采集创作者: @{screen_name}")

                user_profile = await self.twitter_client.get_user_profile(screen_name=screen_name)
                if user_profile:
                    await twitter_store.save_creator(user_profile.get("user_id", ""), creator=user_profile)
                    user_id = user_profile.get("user_id", "")
                    utils.logger.info(f"[TwitterCrawler] 创作者: @{screen_name} ({user_profile.get('nickname', '')}), 粉丝: {user_profile.get('followers_count', 0)}")
                else:
                    utils.logger.error(f"[TwitterCrawler] 无法获取用户资料: @{screen_name}")
                    continue

            except ValueError as e:
                utils.logger.error(f"[TwitterCrawler] 解析创作者URL失败: {e}")
                continue

            crawl_interval = config.CRAWLER_MAX_SLEEP_SEC
            since_date, until_date = self._resolve_date_filters()
            all_tweets = await self.twitter_client.get_all_tweets_by_creator(
                user_id=user_id,
                crawl_interval=crawl_interval,
                callback=self.fetch_creator_tweets_detail,
                since_date=since_date,
                until_date=until_date,
            )

            self._log_progress("推文采集完成")

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
        async with semaphore:
            try:
                tweet_detail = await self.twitter_client.get_tweet_by_id(tweet_id)
                if not tweet_detail:
                    utils.logger.debug(f"[TwitterCrawler] 推文不存在: {tweet_id}")
                    return None

                self._tweets_count += 1
                await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                return tweet_detail

            except TweetNotFoundError:
                utils.logger.debug(f"[TwitterCrawler] 推文未找到: {tweet_id}")
                return None
            except DataFetchError as ex:
                utils.logger.error(f"[TwitterCrawler] 获取推文详情错误: {ex}")
                return None
            except KeyError as ex:
                utils.logger.error(f"[TwitterCrawler] 推文解析错误 {tweet_id}: {ex}")
                return None

    async def batch_get_tweet_comments(self, tweet_ids: List[str]):
        """批量获取推文评论"""
        if not config.ENABLE_GET_COMMENTS:
            return

        utils.logger.info(f"[TwitterCrawler] 开始获取 {len(tweet_ids)} 条推文的评论 ...")
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list: List[Task] = []
        for tweet_id in tweet_ids:
            task = asyncio.create_task(
                self.get_comments(tweet_id=tweet_id, semaphore=semaphore),
                name=tweet_id,
            )
            task_list.append(task)
        await asyncio.gather(*task_list)
        self._log_progress("评论采集完成")

    async def _count_and_store_comments(self, tweet_id: str, comments: List[Dict]):
        """评论存储回调，同时统计数量"""
        self._comments_count += len(comments)
        await twitter_store.batch_update_twitter_tweet_comments(tweet_id, comments)

    async def get_comments(self, tweet_id: str, semaphore: asyncio.Semaphore):
        """获取推文评论"""
        async with semaphore:
            crawl_interval = config.CRAWLER_MAX_SLEEP_SEC
            await self.twitter_client.get_all_tweet_comments(
                tweet_id=tweet_id,
                crawl_interval=crawl_interval,
                callback=self._count_and_store_comments,
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

    def _elapsed(self) -> str:
        """返回已运行时间，格式 HH:MM:SS"""
        secs = int(time.time() - self._start_time)
        h, remainder = divmod(secs, 3600)
        m, s = divmod(remainder, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _log_progress(self, phase: str = ""):
        """输出当前进度"""
        msg = f"[进度] 已采集推文: {self._tweets_count}, 评论: {self._comments_count}, 耗时: {self._elapsed()}"
        if phase:
            msg = f"[进度] {phase} | 已采集推文: {self._tweets_count}, 评论: {self._comments_count}, 耗时: {self._elapsed()}"
        utils.logger.info(msg)

    @staticmethod
    def _resolve_date_filters():
        """解析日期过滤配置，支持 "today" 关键字"""
        today_str = date.today().strftime("%Y-%m-%d")
        since_date = getattr(config, "TWITTER_SINCE_DATE", "") or ""
        until_date = getattr(config, "TWITTER_UNTIL_DATE", "") or ""

        if since_date.lower() == "today":
            since_date = today_str
        if until_date.lower() == "today":
            until_date = today_str

        if since_date:
            utils.logger.info(f"[TwitterCrawler] 日期过滤: since={since_date}, until={until_date or '不限'}")

        return since_date or None, until_date or None

    async def close(self):
        """关闭浏览器上下文"""
        if self.cdp_manager:
            await self.cdp_manager.cleanup()
            self.cdp_manager = None
        else:
            await self.browser_context.close()
        utils.logger.info("[TwitterCrawler.close] 浏览器上下文已关闭 ...")
