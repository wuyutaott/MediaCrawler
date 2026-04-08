# -*- coding: utf-8 -*-

import asyncio
import json
import re
import urllib.parse
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union

import httpx
from playwright.async_api import BrowserContext, Page
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_not_exception_type
from tools.httpx_util import make_async_client

import config
from base.base_crawler import AbstractApiClient
from proxy.proxy_mixin import ProxyRefreshMixin
from tools import utils

if TYPE_CHECKING:
    from proxy.proxy_ip_pool import ProxyIpPool

from .exception import DataFetchError, IPBlockError, TweetNotFoundError
from .field import SearchSortType
from .help import extract_tweet_from_result, extract_user_from_result, is_tweet_in_date_range, is_tweet_before_date


# X平台公共Bearer Token（嵌入在x.com的JS中，所有用户共用）
TWITTER_BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"

# 需要发现的GraphQL操作名称
REQUIRED_OPERATIONS = ["SearchTimeline", "TweetDetail", "UserTweets", "UserByScreenName"]


class TwitterClient(AbstractApiClient, ProxyRefreshMixin):

    def __init__(
        self,
        timeout=60,
        proxy=None,
        *,
        headers: Dict[str, str],
        playwright_page: Page,
        cookie_dict: Dict[str, str],
        proxy_ip_pool: Optional["ProxyIpPool"] = None,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.headers = headers
        self._host = "https://x.com"
        self.playwright_page = playwright_page
        self.cookie_dict = cookie_dict
        # GraphQL端点映射，通过 discover_graphql_endpoints() 动态获取
        self._endpoints: Dict[str, str] = {}
        # 初始化代理池 (来自 ProxyRefreshMixin)
        self.init_proxy_pool(proxy_ip_pool)

    async def discover_graphql_endpoints(self):
        """
        从X平台加载的JS Bundle中动态提取GraphQL端点ID
        X会频繁更新这些ID，所以不能硬编码
        """
        utils.logger.info("[TwitterClient.discover_graphql_endpoints] 开始从JS Bundle中提取GraphQL端点ID ...")

        # 从页面中获取所有加载的JS脚本URL
        script_urls = await self.playwright_page.evaluate("""
            () => {
                return Array.from(document.querySelectorAll('script[src]'))
                    .map(s => s.src)
                    .filter(s => s.includes('client-web') || s.includes('api'))
            }
        """)

        utils.logger.info(f"[TwitterClient.discover_graphql_endpoints] 找到 {len(script_urls)} 个候选JS文件")

        found = {}
        for script_url in script_urls:
            if len(found) == len(REQUIRED_OPERATIONS):
                break

            try:
                async with make_async_client(proxy=self.proxy) as client:
                    resp = await client.get(script_url, timeout=30)
                    if resp.status_code != 200:
                        continue
                    content = resp.text
            except Exception as e:
                utils.logger.debug(f"[TwitterClient.discover_graphql_endpoints] 获取脚本失败: {script_url}, {e}")
                continue

            for op_name in REQUIRED_OPERATIONS:
                if op_name in found:
                    continue
                # 匹配模式: queryId:"abc123",operationName:"UserByScreenName"
                pattern = rf'queryId:"([^"]+)",operationName:"{op_name}"'
                match = re.search(pattern, content)
                if match:
                    query_id = match.group(1)
                    found[op_name] = f"{query_id}/{op_name}"
                    utils.logger.info(f"[TwitterClient.discover_graphql_endpoints] 发现端点: {op_name} -> {query_id}")

        if not found:
            utils.logger.warning("[TwitterClient.discover_graphql_endpoints] 未能从JS中提取端点，尝试从网络请求中捕获 ...")
            found = await self._discover_from_network()

        self._endpoints = found

        missing = [op for op in REQUIRED_OPERATIONS if op not in self._endpoints]
        if missing:
            utils.logger.warning(f"[TwitterClient.discover_graphql_endpoints] 以下端点未找到: {missing}")
        else:
            utils.logger.info("[TwitterClient.discover_graphql_endpoints] 所有GraphQL端点已就绪")

    async def _discover_from_network(self) -> Dict[str, str]:
        """
        通过拦截浏览器网络请求来发现GraphQL端点ID
        导航到一些页面让X发起GraphQL请求，从中提取端点ID
        """
        found = {}

        async def _intercept_route(route):
            url = route.request.url
            if "/i/api/graphql/" in url:
                # URL格式: https://x.com/i/api/graphql/{queryId}/{operationName}?...
                path = url.split("/i/api/graphql/")[1].split("?")[0]
                parts = path.split("/")
                if len(parts) == 2:
                    query_id, op_name = parts
                    if op_name in REQUIRED_OPERATIONS and op_name not in found:
                        found[op_name] = f"{query_id}/{op_name}"
                        utils.logger.info(f"[TwitterClient._discover_from_network] 捕获端点: {op_name} -> {query_id}")
            await route.continue_()

        await self.playwright_page.route("**/i/api/graphql/**", _intercept_route)

        try:
            # 导航到首页，触发一些GraphQL请求
            await self.playwright_page.goto("https://x.com/home", wait_until="networkidle", timeout=15000)
            await asyncio.sleep(3)

            # 导航到explore页面，可能触发SearchTimeline
            await self.playwright_page.goto("https://x.com/explore", wait_until="networkidle", timeout=15000)
            await asyncio.sleep(3)
        except Exception as e:
            utils.logger.debug(f"[TwitterClient._discover_from_network] 导航过程中出错: {e}")
        finally:
            await self.playwright_page.unroute("**/i/api/graphql/**")

        return found

    def _get_endpoint(self, operation_name: str) -> str:
        """获取指定操作的GraphQL端点路径"""
        endpoint = self._endpoints.get(operation_name)
        if not endpoint:
            raise DataFetchError(f"GraphQL端点未发现: {operation_name}，请确保 discover_graphql_endpoints() 已执行")
        return f"/i/api/graphql/{endpoint}"

    def _pre_headers(self) -> Dict:
        """
        构建X API请求头
        X使用静态Bearer Token + CSRF token (ct0 cookie) 进行认证
        """
        ct0 = self.cookie_dict.get("ct0", "")
        headers = {
            **self.headers,
            "authorization": f"Bearer {TWITTER_BEARER_TOKEN}",
            "x-csrf-token": ct0,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "zh-cn",
        }
        return headers

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1), retry=retry_if_not_exception_type(TweetNotFoundError))
    async def request(self, method, url, **kwargs) -> Union[str, Any]:
        """
        封装httpx请求方法，处理请求响应
        """
        await self._refresh_proxy_if_expired()

        return_response = kwargs.pop("return_response", False)
        async with make_async_client(proxy=self.proxy) as client:
            response = await client.request(method, url, timeout=self.timeout, **kwargs)

        if response.status_code == 429:
            # 速率限制
            reset_time = response.headers.get("x-rate-limit-reset", "")
            raise IPBlockError(f"请求频率过高，速率限制中。重置时间: {reset_time}")

        if response.status_code == 401:
            raise DataFetchError("认证失败，请检查登录状态")

        if response.status_code == 403:
            raise DataFetchError("访问被拒绝，可能需要重新登录")

        if return_response:
            return response.text

        if response.status_code != 200:
            raise DataFetchError(f"请求失败，状态码: {response.status_code}，响应: {response.text[:500]}")

        data: Dict = response.json()
        return data

    async def get(self, uri: str, params: Optional[Dict] = None) -> Dict:
        """
        GET请求
        """
        headers = self._pre_headers()
        full_url = f"{self._host}{uri}"
        return await self.request(
            method="GET", url=full_url, headers=headers, params=params
        )

    async def get_media(self, url: str) -> Union[bytes, None]:
        """下载媒体内容"""
        await self._refresh_proxy_if_expired()
        async with make_async_client(proxy=self.proxy) as client:
            try:
                response = await client.request("GET", url, timeout=self.timeout)
                response.raise_for_status()
                return response.content
            except httpx.HTTPError as exc:
                utils.logger.error(
                    f"[TwitterClient.get_media] {exc.__class__.__name__} for {exc.request.url} - {exc}"
                )
                return None

    async def pong(self) -> bool:
        """
        检查登录状态是否有效
        """
        utils.logger.info("[TwitterClient.pong] 开始检查登录状态...")
        ping_flag = False
        try:
            headers = self._pre_headers()
            async with make_async_client(proxy=self.proxy) as client:
                response = await client.get(
                    f"{self._host}/i/api/1.1/account/settings.json",
                    headers=headers,
                    timeout=self.timeout,
                )
                if response.status_code == 200:
                    data = response.json()
                    if data.get("screen_name"):
                        ping_flag = True
        except Exception as e:
            utils.logger.error(f"[TwitterClient.pong] 检查登录状态失败: {e}")
            ping_flag = False
        utils.logger.info(f"[TwitterClient.pong] 登录状态结果: {ping_flag}")
        return ping_flag

    async def update_cookies(self, browser_context: BrowserContext):
        """
        更新cookies
        """
        cookie_str, cookie_dict = utils.convert_cookies(await browser_context.cookies())
        self.headers["Cookie"] = cookie_str
        self.cookie_dict = cookie_dict

    async def search_tweets(
        self,
        keyword: str,
        cursor: str = "",
        sort: SearchSortType = SearchSortType.TOP,
        count: int = 20,
    ) -> Dict:
        """
        搜索推文
        Args:
            keyword: 搜索关键词
            cursor: 分页游标
            sort: 排序方式
            count: 每页数量
        Returns:
            包含推文列表和游标信息的字典
        """
        variables = {
            "rawQuery": keyword,
            "count": count,
            "querySource": "typed_query",
            "product": sort.value,
        }
        if cursor:
            variables["cursor"] = cursor

        features = self._get_common_features()

        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features, separators=(",", ":")),
        }

        uri = self._get_endpoint("SearchTimeline")
        data = await self.get(uri, params=params)

        # 解析搜索结果
        return self._parse_search_timeline(data)

    async def get_tweet_by_id(self, tweet_id: str) -> Optional[Dict]:
        """
        获取推文详情
        Args:
            tweet_id: 推文ID
        Returns:
            推文详情字典
        """
        variables = {
            "focalTweetId": tweet_id,
            "with_rux_injections": False,
            "includePromotedContent": True,
            "withCommunity": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withBirdwatchNotes": True,
            "withVoice": True,
            "withV2Timeline": True,
        }
        features = self._get_common_features()

        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features, separators=(",", ":")),
        }

        uri = self._get_endpoint("TweetDetail")
        data = await self.get(uri, params=params)

        # 解析推文详情
        return self._parse_tweet_detail(data, tweet_id)

    async def get_tweet_comments(self, tweet_id: str, cursor: str = "") -> Dict:
        """
        获取推文的回复/评论
        X平台的回复作为TweetDetail的对话线程返回
        Args:
            tweet_id: 推文ID
            cursor: 分页游标
        Returns:
            包含评论列表和游标信息的字典
        """
        variables = {
            "focalTweetId": tweet_id,
            "with_rux_injections": False,
            "includePromotedContent": True,
            "withCommunity": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withBirdwatchNotes": True,
            "withVoice": True,
            "withV2Timeline": True,
        }
        if cursor:
            variables["cursor"] = cursor
            variables["referrer"] = "tweet"

        features = self._get_common_features()

        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features, separators=(",", ":")),
        }

        uri = self._get_endpoint("TweetDetail")
        data = await self.get(uri, params=params)

        return self._parse_tweet_comments(data, tweet_id)

    async def get_user_profile(self, screen_name: str) -> Optional[Dict]:
        """
        获取用户资料
        Args:
            screen_name: 用户名
        Returns:
            用户资料字典
        """
        variables = {
            "screen_name": screen_name,
            "withSafetyModeUserFields": True,
        }
        features = {
            "hidden_profile_likes_enabled": True,
            "hidden_profile_subscriptions_enabled": True,
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
            "subscriptions_verification_info_is_identity_verified_enabled": True,
            "subscriptions_verification_info_verified_since_enabled": True,
            "highlights_tweets_tab_ui_enabled": True,
            "responsive_web_twitter_article_notes_tab_enabled": True,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "responsive_web_graphql_timeline_navigation_enabled": True,
        }

        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features, separators=(",", ":")),
        }

        uri = self._get_endpoint("UserByScreenName")
        data = await self.get(uri, params=params)

        user_result = data.get("data", {}).get("user", {}).get("result", {})
        if not user_result:
            utils.logger.warning(f"[TwitterClient.get_user_profile] 用户资料为空，原始响应: {json.dumps(data, ensure_ascii=False)[:2000]}")
            return None

        utils.logger.debug(f"[TwitterClient.get_user_profile] user_result: {json.dumps(user_result, ensure_ascii=False)[:2000]}")

        return extract_user_from_result(user_result)

    async def get_user_tweets(
        self,
        user_id: str,
        cursor: str = "",
        count: int = 20,
    ) -> Dict:
        """
        获取用户推文列表
        Args:
            user_id: 用户ID
            cursor: 分页游标
            count: 每页数量
        Returns:
            包含推文列表和游标信息的字典
        """
        variables = {
            "userId": user_id,
            "count": count,
            "includePromotedContent": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Timeline": True,
        }
        if cursor:
            variables["cursor"] = cursor

        features = self._get_common_features()

        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features, separators=(",", ":")),
        }

        uri = self._get_endpoint("UserTweets")
        data = await self.get(uri, params=params)

        utils.logger.debug(f"[TwitterClient.get_user_tweets] 原始响应: {json.dumps(data, ensure_ascii=False)[:2000]}")

        return self._parse_user_tweets(data)

    async def get_all_tweets_by_creator(
        self,
        user_id: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        since_date: Optional[str] = None,
        until_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        获取指定用户的所有推文
        Args:
            since_date: 起始日期过滤 "YYYY-MM-DD"（含），早于此日期的推文会被跳过并停止爬取
            until_date: 结束日期过滤 "YYYY-MM-DD"（含），晚于此日期的推文会被跳过
        """
        result = []
        has_more = True
        cursor = ""
        stop_crawling = False

        while has_more and len(result) < config.CRAWLER_MAX_NOTES_COUNT and not stop_crawling:
            tweets_res = await self.get_user_tweets(user_id=user_id, cursor=cursor)
            if not tweets_res:
                break

            has_more = tweets_res.get("has_more", False)
            cursor = tweets_res.get("cursor", "")
            tweets = tweets_res.get("tweets", [])

            if not tweets:
                break

            # 日期过滤：推文按时间倒序排列，置顶推文不影响停止逻辑
            filtered_tweets = []
            for tweet in tweets:
                created_at = tweet.get("created_at", "")
                is_pinned = tweet.get("is_pinned", False)

                # 如果非置顶推文早于 since_date，后面的都不用看了
                if since_date and not is_pinned and is_tweet_before_date(created_at, since_date):
                    stop_crawling = True
                    utils.logger.info(f"[TwitterClient.get_all_tweets_by_creator] 推文已早于 {since_date}，停止爬取")
                    break

                # 检查是否在日期范围内（置顶推文也要过滤日期）
                if is_tweet_in_date_range(created_at, since_date, until_date):
                    filtered_tweets.append(tweet)

            remaining = config.CRAWLER_MAX_NOTES_COUNT - len(result)
            if remaining <= 0:
                break

            tweets_to_add = filtered_tweets[:remaining]
            if tweets_to_add and callback:
                await callback(tweets_to_add)

            result.extend(tweets_to_add)
            await asyncio.sleep(crawl_interval)

        utils.logger.info(
            f"[TwitterClient.get_all_tweets_by_creator] 获取用户 {user_id} 推文完成，总计: {len(result)}"
        )
        return result

    async def get_all_tweet_comments(
        self,
        tweet_id: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        max_count: int = 10,
    ) -> List[Dict]:
        """
        获取指定推文的所有评论
        """
        result = []
        has_more = True
        cursor = ""
        while has_more and len(result) < max_count:
            comments_res = await self.get_tweet_comments(tweet_id=tweet_id, cursor=cursor)
            has_more = comments_res.get("has_more", False)
            cursor = comments_res.get("cursor", "")
            comments = comments_res.get("comments", [])

            if not comments:
                break

            if len(result) + len(comments) > max_count:
                comments = comments[:max_count - len(result)]

            if callback:
                await callback(tweet_id, comments)

            await asyncio.sleep(crawl_interval)
            result.extend(comments)

        return result

    def _get_common_features(self) -> Dict:
        """获取GraphQL请求的通用features参数"""
        return {
            "rweb_tipjar_consumption_enabled": True,
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "communities_web_enable_tweet_community_results_fetch": True,
            "c9s_tweet_anatomy_moderator_badge_enabled": True,
            "articles_preview_enabled": True,
            "tweetypie_unmention_optimization_enabled": True,
            "responsive_web_edit_tweet_api_enabled": True,
            "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
            "view_counts_everywhere_api_enabled": True,
            "longform_notetweets_consumption_enabled": True,
            "responsive_web_twitter_article_tweet_consumption_enabled": True,
            "tweet_awards_web_tipping_enabled": False,
            "creator_subscriptions_quote_tweet_preview_enabled": False,
            "freedom_of_speech_not_reach_fetch_enabled": True,
            "standardized_nudges_misinfo": True,
            "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
            "rweb_video_timestamps_enabled": True,
            "longform_notetweets_rich_text_read_enabled": True,
            "longform_notetweets_inline_media_enabled": True,
            "responsive_web_enhance_cards_enabled": False,
        }

    def _parse_search_timeline(self, data: Dict) -> Dict:
        """解析搜索时间线响应"""
        result = {"tweets": [], "has_more": False, "cursor": ""}

        try:
            instructions = (
                data.get("data", {})
                .get("search_by_raw_query", {})
                .get("search_timeline", {})
                .get("timeline", {})
                .get("instructions", [])
            )

            for instruction in instructions:
                if instruction.get("type") == "TimelineAddEntries":
                    entries = instruction.get("entries", [])
                    for entry in entries:
                        entry_id = entry.get("entryId", "")
                        if entry_id.startswith("tweet-"):
                            tweet = extract_tweet_from_result(entry)
                            if tweet:
                                result["tweets"].append(tweet)
                        elif entry_id.startswith("cursor-bottom-"):
                            cursor_value = (
                                entry.get("content", {})
                                .get("value", "")
                            )
                            if cursor_value:
                                result["cursor"] = cursor_value
                                result["has_more"] = True
        except (KeyError, TypeError) as e:
            utils.logger.error(f"[TwitterClient._parse_search_timeline] 解析搜索结果失败: {e}")

        return result

    def _parse_tweet_detail(self, data: Dict, target_tweet_id: str) -> Optional[Dict]:
        """解析推文详情响应，返回目标推文"""
        try:
            instructions = (
                data.get("data", {})
                .get("threaded_conversation_with_injections_v2", {})
                .get("instructions", [])
            )

            for instruction in instructions:
                if instruction.get("type") == "TimelineAddEntries":
                    entries = instruction.get("entries", [])
                    for entry in entries:
                        entry_id = entry.get("entryId", "")
                        if f"tweet-{target_tweet_id}" == entry_id:
                            return extract_tweet_from_result(entry)
        except (KeyError, TypeError) as e:
            utils.logger.error(f"[TwitterClient._parse_tweet_detail] 解析推文详情失败: {e}")

        return None

    def _parse_tweet_comments(self, data: Dict, target_tweet_id: str) -> Dict:
        """解析推文评论（对话线程中的回复）"""
        result = {"comments": [], "has_more": False, "cursor": ""}

        try:
            instructions = (
                data.get("data", {})
                .get("threaded_conversation_with_injections_v2", {})
                .get("instructions", [])
            )

            for instruction in instructions:
                inst_type = instruction.get("type", "")
                entries = []
                if inst_type == "TimelineAddEntries":
                    entries = instruction.get("entries", [])
                elif inst_type == "TimelineAddToModule":
                    entries = instruction.get("moduleItems", [])

                for entry in entries:
                    entry_id = entry.get("entryId", "")

                    # 跳过目标推文本身
                    if entry_id == f"tweet-{target_tweet_id}":
                        continue

                    # 处理对话线程条目
                    if entry_id.startswith("conversationthread-"):
                        items = (
                            entry.get("content", {})
                            .get("items", [])
                        )
                        for item in items:
                            item_entry_id = item.get("entryId", "")
                            if "tweet-" in item_entry_id:
                                comment = extract_tweet_from_result(item.get("item", {}))
                                if comment:
                                    comment["parent_tweet_id"] = target_tweet_id
                                    result["comments"].append(comment)
                            elif "cursor-showmore" in item_entry_id or "cursor-bottom" in item_entry_id:
                                cursor_val = (
                                    item.get("item", {})
                                    .get("itemContent", {})
                                    .get("value", "")
                                )
                                if cursor_val:
                                    result["cursor"] = cursor_val
                                    result["has_more"] = True
                    elif entry_id.startswith("tweet-"):
                        comment = extract_tweet_from_result(entry)
                        if comment:
                            comment["parent_tweet_id"] = target_tweet_id
                            result["comments"].append(comment)
                    elif entry_id.startswith("cursor-bottom-"):
                        cursor_value = (
                            entry.get("content", {})
                            .get("value", "")
                        )
                        if cursor_value:
                            result["cursor"] = cursor_value
                            result["has_more"] = True

        except (KeyError, TypeError) as e:
            utils.logger.error(f"[TwitterClient._parse_tweet_comments] 解析评论失败: {e}")

        return result

    def _parse_user_tweets(self, data: Dict) -> Dict:
        """解析用户推文列表响应"""
        result = {"tweets": [], "has_more": False, "cursor": ""}

        try:
            user_result = data.get("data", {}).get("user", {}).get("result", {})

            # X API可能返回 timeline_v2 或 timeline
            timeline_obj = user_result.get("timeline_v2") or user_result.get("timeline", {})
            instructions = timeline_obj.get("timeline", {}).get("instructions", [])

            for instruction in instructions:
                inst_type = instruction.get("type", "")

                # 跳过缓存清理等非数据指令
                if inst_type == "TimelineClearCache":
                    continue

                # 处理单个条目（如置顶推文）
                if "entry" in instruction:
                    entry = instruction["entry"]
                    self._process_tweet_entry(entry, result, is_pinned=True)

                # 处理多个条目（正常时间线）
                entries = instruction.get("entries", [])
                for entry in entries:
                    self._process_tweet_entry(entry, result)

        except (KeyError, TypeError) as e:
            utils.logger.error(f"[TwitterClient._parse_user_tweets] 解析用户推文失败: {e}")

        return result

    def _process_tweet_entry(self, entry: Dict, result: Dict, is_pinned: bool = False):
        """处理单个时间线条目，提取推文或游标"""
        entry_id = entry.get("entryId", "")
        content = entry.get("content", {})

        if entry_id.startswith("tweet-") or entry_id.startswith("profile-conversation-"):
            # 检查是否为置顶推文（socialContext 中标记为 Pin）
            item_content = content.get("itemContent", {})
            social_context = item_content.get("socialContext", {})
            if social_context.get("contextType") == "Pin":
                is_pinned = True

            items = content.get("items")
            if items:
                for item in items:
                    tweet = extract_tweet_from_result(item.get("item", {}))
                    if tweet:
                        tweet["is_pinned"] = is_pinned
                        result["tweets"].append(tweet)
            else:
                tweet = extract_tweet_from_result(entry)
                if tweet:
                    tweet["is_pinned"] = is_pinned
                    result["tweets"].append(tweet)
        elif entry_id.startswith("cursor-bottom-"):
            cursor_value = content.get("value", "")
            if cursor_value:
                result["cursor"] = cursor_value
                result["has_more"] = True
