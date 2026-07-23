import os
import requests
import yfinance as yf
import pandas as pd
import urllib3
from datetime import datetime
import pytz
from bs4 import BeautifulSoup

# 關閉略過 SSL 驗證警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def get_stock_list():
    """取得上市與上櫃股票清單"""
    stock_dict = {}
    print("正在取得上市與上櫃股票清單...")
    
    twse_url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    tpex_url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
    
    try:
        res_twse = requests.get(twse_url, verify=False, timeout=10)
        if res_twse.status_code == 200:
            for item in res_twse.json():
                code, name = str(item.get('Code', '')), str(item.get('Name', ''))
                if len(code) == 4: stock_dict[f"{code}.TW"] = name
        
        res_tpex = requests.get(tpex_url, verify=False, timeout=10)
        if res_tpex.status_code == 200:
            for item in res_tpex.json():
                code = str(item.get('SecuritiesCompanyCode', ''))
                name = str(item.get('CompanyName', ''))
                if len(code) == 4: stock_dict[f"{code}.TWO"] = name
    except Exception as e:
        print(f"取得清單失敗: {e}")
    return stock_dict

def get_yahoo_pe(stock_code):
    """直接爬取台灣奇摩股市網頁上的本益比"""
    url = f"https://tw.stock.yahoo.com/quote/{stock_code}/technical-analysis"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        res = requests.get(url, headers=headers, timeout=5, verify=False)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            pe_label = soup.find("span", string=lambda t: t and "本益比" in t)
            if pe_label:
                pe_value_span = pe_label.find_parent().find("span", class_=lambda c: c and "Fz(16px)" in c)
                if pe_value_span:
                    full_text = pe_value_span.get_text(strip=True)
                    pe_num = full_text.split("(")[0].strip()
                    return pe_num
    except Exception as e:
        pass
    return "N/A"

def send_discord_message(content):
    """發送至 Discord"""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("未設定 DISCORD_WEBHOOK_URL，將僅印出結果：\n", content)
        return
    chunks = [content[i:i+1900] for i in range(0, len(content), 1900)]
    for chunk in chunks:
        requests.post(webhook_url, json={"content": chunk})

def find_bottom_consolidation_stocks():
    stock_dict = get_stock_list()
    tickers = list(stock_dict.keys())
    
    print(f"開始分析 {len(tickers)} 檔股票的歷史數據 (下載 6 個月資料)...")
    data = yf.download(" ".join(tickers), period="6mo", group_by='ticker', threads=True, progress=False)
    
    matched_stocks = []
    tw_tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tw_tz)
    
    today_str = now.strftime('%Y-%m-%d')
    today_slash_str = now.strftime('%Y/%m/%d')
    
    for ticker in tickers:
        try:
            df = data[ticker].dropna(subset=['Close', 'Volume', 'High', 'Low']).copy()
            if df.empty or len(df) < 90:  
                continue
            
            recent_days = 20     # 近期盤整觀察期改為 20 天 (約一個月)
            past_start = 80      # 尋找歷史爆量的起點往前推至 80 天前
            past_end = 20        # 歷史爆量必須發生在一個月 (20天) 以前
            
            recent_df = df.iloc[-recent_days:]
            past_df = df.iloc[-past_start:-past_end]
            
            # 1. 尋找歷史區間的最大量 (疑似爆量日)
            if past_df.empty:
                continue
            max_vol_idx = past_df['Volume'].idxmax()
            max_vol = past_df.loc[max_vol_idx, 'Volume']
            
            if max_vol < (past_df['Volume'].mean() * 3):
                continue
                
            # 2. 尋找爆量前的「起漲點」基準價格
            max_vol_pos = df.index.get_loc(max_vol_idx)
            if max_vol_pos < 5: 
                continue
            base_price = df['Close'].iloc[max_vol_pos-5:max_vol_pos].mean()
            
            # 3. 檢查近期是否處於「量縮盤整」且「回到起漲點」
            recent_mean_p = recent_df['Close'].mean()
            recent_max_p = recent_df['Close'].max()
            recent_min_p = recent_df['Close'].min()
            recent_mean_vol = recent_df['Volume'].mean()
            
            current_close = df['Close'].iloc[-1]
            current_vol = df['Volume'].iloc[-1]
            
            # 條件 A: 一個月內的均價與當初起漲點差異極小 (誤差 5% 內)
            if abs(recent_mean_p - base_price) / base_price > 0.05:
                continue
                
            # 條件 B: 一個月內的最高最低價差極小 (維持振幅小於 8%)
            if (recent_max_p - recent_min_p) / recent_min_p > 0.08:
                continue
                
            # 條件 C: 一個月內極度量縮 (近期均量不到當初爆量日成交量的 10%)
            if recent_mean_vol > (max_vol * 0.10):
                continue
                
            # 條件 D: 確保最後一個交易日依然保持絕對靜止 (不可大於一個月極低均量的 1.5 倍)
            if current_vol > (recent_mean_vol * 1.5):
                continue
                
            # 條件 E: 排除流動性過差的個股，並鎖定收盤價小於 20 元的低價股
            if recent_mean_vol < 100000 or current_close >= 20: 
                continue

            clean_code = ticker.split('.')[0]
            name = stock_dict[ticker]
            
            # 計算距離基準價的幅度
            diff_pct = (current_close - base_price) / base_price * 100
            
            pe_str = get_yahoo_pe(clean_code)
            yahoo_link = f"<https://tw.stock.yahoo.com/quote/{clean_code}/technical-analysis>"
            
            matched_stocks.append(
                f"📊 **{clean_code} {name}** | {today_slash_str}\n"
                f"收盤價: `{current_close:.2f}` | 一個月盤整均量: `{int(recent_mean_vol / 1000)}` 張 | 本益比: `{pe_str}`\n"
                f"🔍 歷史爆量基準價: `{base_price:.2f}` (距基準價幅度: `{diff_pct:+.2f}%`)\n"
                f"🔗 {yahoo_link}"
            )
                
        except Exception as e:
            continue

    # 組合 Discord 訊息
    message = f"🎯 **台股 {today_str} 爆量拉回沉澱一個月策略清單**\n" + "="*30 + "\n"
    message += "(條件：20元以下、歷史出量、長達一個月量縮至10%以下、價格振幅8%內)\n\n"
    if matched_stocks:
        message += "\n\n".join(matched_stocks)
    else:
        message += "今天沒有符合此極度靜止型態的個股。"
    
    send_discord_message(message)

if __name__ == "__main__":
    find_bottom_consolidation_stocks()
