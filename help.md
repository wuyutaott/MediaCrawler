# 下载turingou的部分信息
uv run python main.py --platform twitter --type creator --creator_id "turingou"

# 只爬今天的                                                                  
uv run python main.py --platform twitter --type creator --creator_id
"turingou" --since_date today --until_date today                              
                                                                            
# 爬最近三天的                                                                
uv run python main.py --platform twitter --type creator --creator_id       
"turingou" --since_date 2026-03-29                                            
                                                                            
# 爬指定日期范围                                                              
uv run python main.py --platform twitter --type creator --creator_id "turingou" --since_date 2026-03-25 --until_date 2026-03-31

uv run python main.py --platform twitter --type creator --creator_id "turingou" --since_date 2026-03-25 --until_date 2026-03-31 --max_notes_count 200