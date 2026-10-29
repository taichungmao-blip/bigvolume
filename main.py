import os
import requests
import yfinance as yf
import pandas as pd
import urllib3
from datetime import datetime
import pytz
from bs4 import BeautifulSoup
import time
import json

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HISTORY_FILE = "signal_history.json"

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"讀取歷史紀錄失敗: {e}")
    return {}

def save_history(history_data):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history_data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"儲存歷史紀錄失敗: {e}")

def get_stock_list():
    stock_dict = {}
    print("正在取得上市與上櫃股票清單...")
    twse_url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    tpex_url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
    
    try:
        res_twse = requests.get(twse_url, verify=False, timeout=10)
        if res_twse.status_code == 200:
            for item in res_twse.json():
                code, name = str(item.get('Code', '')), str(item.get('Name', ''))
                if len(code) == 4 and code.isdigit(): 
                    stock_dict[f"{code}.TW"] = name
        
        res_tpex = requests.get(tpex_url, verify=False, timeout=10)
        if res_tpex.status_code == 200:
            for item in res_tpex.json():
                code = str(item.get('SecuritiesCompanyCode', ''))
                name = str(item.get('CompanyName', ''))
                if len(code) == 4 and code.isdigit(): 
                    stock_dict[f"{code}.TWO"] = name
    except Exception as e:
        print(f"取得清單失敗: {e}")
        
    return stock_dict

def get_yahoo_pe(stock_code):
    url = f"https://tw.stock.yahoo.com/quote/{stock_code}/technical-analysis"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=5, verify=False)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            pe_label = soup.find("span", string=lambda t: t and "本益比" in t)
            if pe_label:
                pe_value_span = pe_label.find_parent().find("span", class_=lambda c: c and "Fz(16px)" in c)
                if pe_value_span:
                    return pe_value_span.get_text(strip=True).split("(")[0].strip()
    except Exception:
        pass
    return "N/A"

def get_yahoo_nav(stock_code):
    """直接爬取台灣奇摩股市網頁上的每股淨值"""
    url = f"https://tw.stock.yahoo.com/quote/{stock_code}/profile"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        res = requests.get(url, headers=headers, timeout=5, verify=False)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            nav_label = soup.find("span", string=lambda t: t and "每股淨值" in t)
            if nav_label:
                nav_value_div = nav_label.find_next_sibling("div")
                if nav_value_div:
                    return nav_value_div.get_text(strip=True)
    except Exception as e:
        print(f"爬取 {stock_code} 每股淨值失敗: {e}")
    return "不清楚"

def send_discord_message(content_list, header):
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("未設定 DISCORD_WEBHOOK_URL，將僅印出結果：\n", header)
        for item in content_list:
            print(item)
        return

    current_chunk = header
    for item in content_list:
        if len(current_chunk) + len(item) + 2 > 1900:
            requests.post(webhook_url, json={"content": current_chunk})
            current_chunk = item
            time.sleep(1) 
        else:
            current_chunk += f"\n\n{item}"
            
    if current_chunk:
        requests.post(webhook_url, json={"content": current_chunk})

def find_bottom_consolidation_stocks():
    stock_dict = get_stock_list()
    
    twse_count = sum(1 for t in stock_dict if t.endswith(".TW"))
    tpex_count = sum(1 for t in stock_dict if t.endswith(".TWO"))
    print(f"上市股票數量: {twse_count}")
    print(f"上櫃股票數量: {tpex_count}")
    
    if twse_count == 0 or tpex_count == 0:
        error_msg = "錯誤：未能完整取得上市或上櫃股票清單，停止執行。"
        print(error_msg)
        send_discord_message([error_msg], "⚠️ **系統異常警告**")
        return
        
    tickers = list(stock_dict.keys())
    
    tw_tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tw_tz)
    
    if 9 <= now.hour < 15:
        print("尚未到安全分析時間（盤中量能未收斂），停止執行。")
        return

    print("正在下載加權指數 (^TWII) 數據以計算相對強度與交易日曆...")
    twii_data = yf.download("^TWII", period="9mo", auto_adjust=True, progress=False)
    
    if twii_data.empty:
        print("錯誤：無法取得加權指數資料，停止執行。")
        return
    trading_days = [d.strftime('%Y/%m/%d') for d in twii_data.index]
    
    print(f"開始分析 {len(tickers)} 檔股票的歷史數據...")
    data = yf.download(" ".join(tickers), period="9mo", group_by='ticker', auto_adjust=True, threads=True, progress=False)
    
    matched_stocks = []
    signal_history = load_history()
    
    for ticker in tickers:
        try:
            df = data[ticker].dropna(subset=['Close', 'Volume', 'High', 'Low']).copy()
            if df.empty or len(df) < 90:  
                continue
                
            benchmark = twii_data['Close']
            if isinstance(benchmark, pd.DataFrame):
                benchmark = benchmark.iloc[:, 0]
                
            if df.index[-1].date() != benchmark.index[-1].date():
                continue
                
            latest_date_str = df.index[-1].strftime('%Y/%m/%d')
                
            df['MA60'] = df['Close'].rolling(window=60).mean()
            
            recent_days = 20
            past_start = 80
            past_end = 20
            
            recent_df = df.iloc[-recent_days:]
            past_df = df.iloc[-past_start:-past_end]
            
            if past_df.empty:
                continue
                
            max_vol_idx = past_df['Volume'].idxmax()
            max_vol = past_df.loc[max_vol_idx, 'Volume']
            max_vol_pos = df.index.get_loc(max_vol_idx)
            
            if max_vol_pos < 21:
                continue
                
            before_boom = df.iloc[max_vol_pos-20:max_vol_pos]
            normal_vol = before_boom['Volume'].mean()
            if normal_vol <= 0 or max_vol < normal_vol * 3:
                continue
                
            base_price = df['Low'].iloc[max_vol_pos]
            boom_high = df['High'].iloc[max_vol_pos]
            boom_low = df['Low'].iloc[max_vol_pos]
            boom_close = df['Close'].iloc[max_vol_pos]
            
            pre_boom_gain = (df['Close'].iloc[max_vol_pos-1] / df['Close'].iloc[max_vol_pos-21]) - 1
            if pre_boom_gain > 0.30:
                continue
            
            if boom_high > boom_low:
                close_position = (boom_close - boom_low) / (boom_high - boom_low)
                if close_position < 0.7:
                    continue
            elif boom_high == boom_low and boom_close > df['Close'].iloc[max_vol_pos-1]:
                pass 
            else:
                continue

            post_boom_df = df.iloc[max_vol_pos+1:]
            if post_boom_df.empty or post_boom_df['Low'].min() < (base_price * 0.98):
                continue

            recent_min_p = recent_df['Low'].min()
            recent_max_p = recent_df['High'].max()
            recent_mean_vol = recent_df['Volume'].mean()
            
            current_close = df['Close'].iloc[-1].item()
            current_vol = df['Volume'].iloc[-1].item()
            current_ma60 = df['MA60'].iloc[-1].item()
            
            boom_gain = (current_close / boom_close) - 1
            if boom_gain > 0.15:
                continue
                
            support_distance = (current_close / base_price) - 1
            if support_distance > 0.12:
                continue
            
            aligned = pd.concat([df['Close'], benchmark], axis=1, join='inner').dropna()
            aligned.columns = ['Stock', 'TWII']
            
            if len(aligned) < 21:
                continue
                
            stock_return = (aligned['Stock'].iloc[-1] / aligned['Stock'].iloc[-21]) - 1
            twii_return = (aligned['TWII'].iloc[-1] / aligned['TWII'].iloc[-21]) - 1
            if stock_return <= twii_return:
                continue
            
            ma60_10days_ago = df['MA60'].iloc[-11]
            if pd.isna(ma60_10days_ago) or current_ma60 <= ma60_10days_ago:
                continue
                
            if pd.isna(current_ma60) or current_close < current_ma60:
                continue
                
            if (recent_max_p - recent_min_p) / recent_min_p > 0.08:
                continue
                
            range_mid = (recent_max_p + recent_min_p) / 2
            if current_close < range_mid:
                continue
                
            if recent_mean_vol > (max_vol * 0.25):
                continue
                
            if current_vol > (recent_mean_vol * 1.5):
                continue
                
            if recent_mean_vol < 300000: 
                continue

            clean_code = ticker.split('.')[0]
            name = stock_dict[ticker]
            pe_str = get_yahoo_pe(clean_code)
            nav_str = get_yahoo_nav(clean_code)  # 取得每股淨值
            
            current_date_idx = trading_days.index(latest_date_str)
            prev_trading_day_str = trading_days[current_date_idx - 1] if current_date_idx > 0 else None
            
            stock_snapshot = {
                "close": round(current_close, 2),
                "ma60": round(current_ma60, 2),
                "volume": int(current_vol),
                "base_price": round(base_price, 2)
            }

            if clean_code not in signal_history:
                signal_status = "🆕 首次入選"
                signal_history[clean_code] = {
                    "first_seen": latest_date_str,
                    "last_seen": latest_date_str,
                    "signal_dates": [latest_date_str],
                    "history_data": {latest_date_str: stock_snapshot}
                }
            else:
                hist = signal_history[clean_code]
                if latest_date_str in hist["signal_dates"]:
                    signal_status = f"🔄 今日已記錄 (首次: {hist['first_seen']})"
                else:
                    if hist["last_seen"] == prev_trading_day_str:
                        signal_status = f"🔄 持續觀察 (首次: {hist['first_seen']})"
                    else:
                        signal_status = f"⚠️ 重新入選 (前次: {hist['last_seen']})"
                        hist["first_seen"] = latest_date_str
                        
                    hist["last_seen"] = latest_date_str
                    hist["signal_dates"].append(latest_date_str)
                    hist["history_data"][latest_date_str] = stock_snapshot
            
            if "今日已記錄" not in signal_status:
                matched_stocks.append(
                    f"📊 **{clean_code} {name}** | {latest_date_str} [{signal_status}]\n"
                    f"收盤價: `{current_close:.2f}` | 距支撐價幅度: `{support_distance*100:+.2f}%`\n"
                    f"均量: `{int(recent_mean_vol / 1000)}` 張 | 本益比: `{pe_str}` | 每股淨值: `{nav_str}`\n"
                    f"🛡️ 防守低點(還原權息): `{base_price:.2f}`\n"
                    f"📈 季線位階: `{current_ma60:.2f}` (向上趨勢且優於大盤)\n"
                    f"🔗 <https://tw.stock.yahoo.com/quote/{clean_code}/technical-analysis>"
                )
                
        except Exception as e:
            print(f"[分析失敗] {ticker}: {type(e).__name__}: {e}")
            continue

    save_history(signal_history)

    header = f"🎯 **觀察名單：強勢股量縮整理訊號**\n" + "="*30 + "\n"
    header += "(註：報價為還原權息之技術價位，非實際下單價格。防守低點為型態參考，非絕對停損保證。)\n"
    
    if not matched_stocks:
        matched_stocks = ["今日無符合此觀察型態之新增個股。"]
        
    send_discord_message(matched_stocks, header)

if __name__ == "__main__":
    find_bottom_consolidation_stocks()
