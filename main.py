import os
import asyncio
import json
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import anthropic
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from pytz import timezone

BAKU = timezone('Asia/Baku')

API_ID = int(os.environ.get('API_ID'))
API_HASH = os.environ.get('API_HASH')
SESSION_STRING = os.environ.get('SESSION_STRING')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
PERSONAL_CHAT_ID = int(os.environ.get('PERSONAL_CHAT_ID'))
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
ACCOUNT_SIZE = 500
RISK_PERCENT = 0.02

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def now_baku():
    return datetime.now(BAKU).strftime('%Y-%m-%d %H:%M:%S')

def get_sheets():
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        return client.open_by_key(SPREADSHEET_ID)
    except Exception as e:
        print("get_sheets ERROR: " + str(type(e).__name__) + ": " + str(e))
        raise

def get_trust_score(channel_name):
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('ChannelScores')
        records = ws.get_all_records()
        for row in records:
            if row.get('Channel Name', '').lower() == channel_name.lower():
                return {
                    'trust_score': row.get('Trust Score', 0),
                    'total_signals': row.get('Total Signals', 0),
                    'hit_rate': row.get('Hit Rate %', 0)
                }
        return {'trust_score': 0, 'total_signals': 0, 'hit_rate': 0}
    except Exception as e:
        print("Error getting trust score: " + str(e))
        return {'trust_score': 0, 'total_signals': 0, 'hit_rate': 0}

def update_channel_score(channel_name, hit_type):
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('ChannelScores')
        records = ws.get_all_records()
        is_tp = hit_type.startswith('TP')

        for i, row in enumerate(records):
            if row.get('Channel Name', '').lower() == channel_name.lower():
                row_num = i + 2
                total = int(row.get('Total Signals', 0)) + 1
                tp_hits = int(row.get('TP Hits', 0)) + (1 if is_tp else 0)
                sl_hits = int(row.get('SL Hits', 0)) + (0 if is_tp else 1)
                hit_rate = round((tp_hits / total) * 100, 1)
                weight = min(total / 10, 1.0)
                trust_score = round(hit_rate * weight, 1)
                ws.update('B' + str(row_num) + ':G' + str(row_num), [[
                    total, tp_hits, sl_hits, hit_rate, trust_score, now_baku()
                ]])
                return trust_score

        total = 1
        tp_hits = 1 if is_tp else 0
        sl_hits = 0 if is_tp else 1
        hit_rate = 100.0 if is_tp else 0.0
        trust_score = round(hit_rate * 0.1, 1)
        ws.append_row([channel_name, total, tp_hits, sl_hits, hit_rate, trust_score, now_baku()])
        return trust_score
    except Exception as e:
        print("Error updating channel score: " + str(e))
        return None

def calculate_lot_size(asset, entry, stop_loss):
    try:
        risk_amount = ACCOUNT_SIZE * RISK_PERCENT
        asset_key = asset.upper().replace('/', '').replace(' ', '')
        sl_distance = abs(entry - stop_loss)
        if sl_distance == 0:
            return None
        if asset_key == 'XAUUSD':
            lot = round(risk_amount / (sl_distance * 100), 2)
        elif asset_key in ('BTCUSD', 'ETHUSD'):
            lot = round(risk_amount / sl_distance, 4)
        elif asset_key == 'XAGUSD':
            lot = round(risk_amount / (sl_distance * 50), 2)
        elif asset_key == 'BRENT':
            lot = round(risk_amount / (sl_distance * 100), 2)
        else:
            lot = round(risk_amount / (sl_distance * 100), 2)
        return max(lot, 0.01)
    except Exception as e:
        print("Lot size error: " + str(e))
        return None

def is_fake_signal(signal, channel_name):
    entry = signal.get('entry')
    sl = signal.get('stop_loss')
    tp1 = signal.get('tp1')
    if not entry or not sl or not tp1:
        return False, None
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp1 - entry)
    if sl_dist > 0 and tp_dist / sl_dist < 1.0:
        r
