# -*- coding: utf-8 -*-
# X (Twitter) 平台配置

# 搜索排序方式，具体枚举值在 media_platform/twitter/field.py 中
TWITTER_SORT_TYPE = "Top"

# 推文日期过滤（格式 "YYYY-MM-DD"，留空表示不过滤）
# 设为 "today" 会在运行时自动替换为当天日期
TWITTER_SINCE_DATE = ""  # 起始日期（含），如 "2026-03-31" 或 "today"
TWITTER_UNTIL_DATE = ""  # 结束日期（含），如 "2026-03-31" 或 "today"

# 指定推文URL列表，用于 detail 模式
TWITTER_SPECIFIED_TWEET_URL_LIST = [
    "https://x.com/elonmusk/status/1234567890123456789"
    # ........................
]

# 指定创作者主页URL列表，用于 creator 模式
TWITTER_CREATOR_ID_LIST = [
    "https://x.com/elonmusk"
    # ........................
]
