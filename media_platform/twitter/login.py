# -*- coding: utf-8 -*-

import asyncio
import sys
from typing import Optional

from playwright.async_api import BrowserContext, Page
from tenacity import RetryError, retry, retry_if_result, stop_after_attempt, wait_fixed

import config
from base.base_crawler import AbstractLogin
from tools import utils


class TwitterLogin(AbstractLogin):

    def __init__(
        self,
        login_type: str,
        browser_context: BrowserContext,
        context_page: Page,
        login_phone: Optional[str] = "",
        cookie_str: str = "",
    ):
        config.LOGIN_TYPE = login_type
        self.browser_context = browser_context
        self.context_page = context_page
        self.login_phone = login_phone
        self.cookie_str = cookie_str

    @retry(stop=stop_after_attempt(600), wait=wait_fixed(1), retry=retry_if_result(lambda value: value is False))
    async def check_login_state(self) -> bool:
        """
        检查X平台登录状态
        通过检查cookie中是否存在 auth_token 和 ct0 来判断
        """
        try:
            current_cookies = await self.browser_context.cookies()
            _, cookie_dict = utils.convert_cookies(current_cookies)
            auth_token = cookie_dict.get("auth_token")
            ct0 = cookie_dict.get("ct0")
            if auth_token and ct0:
                utils.logger.info("[TwitterLogin.check_login_state] 通过Cookie确认登录状态 (auth_token + ct0)")
                return True
        except Exception:
            pass

        # 检查页面上是否有已登录标识
        try:
            # 检查是否能看到主页时间线或发推按钮
            home_indicator = await self.context_page.query_selector('[data-testid="SideNav_AccountSwitcher_Button"]')
            if home_indicator:
                utils.logger.info("[TwitterLogin.check_login_state] 通过UI元素确认登录状态")
                return True
        except Exception:
            pass

        return False

    async def begin(self):
        """开始登录X平台"""
        utils.logger.info("[TwitterLogin.begin] 开始登录X平台 ...")
        if config.LOGIN_TYPE == "qrcode":
            await self.login_by_qrcode()
        elif config.LOGIN_TYPE == "phone":
            await self.login_by_mobile()
        elif config.LOGIN_TYPE == "cookie":
            await self.login_by_cookies()
        else:
            raise ValueError("[TwitterLogin.begin] 无效的登录类型，当前仅支持 qrcode、phone 或 cookie ...")

    async def login_by_qrcode(self):
        """
        X平台不支持二维码登录，提示用户在浏览器中手动登录
        """
        utils.logger.info("[TwitterLogin.login_by_qrcode] X平台不支持二维码登录，请在打开的浏览器中手动登录 ...")
        utils.logger.info("[TwitterLogin.login_by_qrcode] 等待用户手动登录，剩余时间 600 秒")

        try:
            await self.check_login_state()
        except RetryError:
            utils.logger.info("[TwitterLogin.login_by_qrcode] 手动登录超时 ...")
            sys.exit()

        wait_redirect_seconds = 5
        utils.logger.info(f"[TwitterLogin.login_by_qrcode] 登录成功，等待 {wait_redirect_seconds} 秒跳转 ...")
        await asyncio.sleep(wait_redirect_seconds)

    async def login_by_mobile(self):
        """
        X平台手机号登录
        提示用户在浏览器中手动完成登录流程
        """
        utils.logger.info("[TwitterLogin.login_by_mobile] X平台手机登录，请在浏览器中手动完成登录流程 ...")

        try:
            await self.check_login_state()
        except RetryError:
            utils.logger.info("[TwitterLogin.login_by_mobile] 登录超时 ...")
            sys.exit()

        wait_redirect_seconds = 5
        utils.logger.info(f"[TwitterLogin.login_by_mobile] 登录成功，等待 {wait_redirect_seconds} 秒跳转 ...")
        await asyncio.sleep(wait_redirect_seconds)

    async def login_by_cookies(self):
        """通过cookie登录X平台"""
        utils.logger.info("[TwitterLogin.login_by_cookies] 开始通过Cookie登录X平台 ...")
        for key, value in utils.convert_str_cookie_to_dict(self.cookie_str).items():
            if key not in ("auth_token", "ct0"):
                continue
            await self.browser_context.add_cookies([{
                "name": key,
                "value": value,
                "domain": ".x.com",
                "path": "/",
            }])
