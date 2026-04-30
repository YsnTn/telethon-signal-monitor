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
        print(f"get_sheets ERROR: {type(e).__name__}: {e}")
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
        print(f"Error getting trust score: {e}")
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
                ws.update(f'B{row_num}:G{row_num}', [[
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
        print(f"Error updating channel score: {e}")
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
        print(f"Lot size error: {e}")
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
        return True, f"Poor RR ratio ({round(tp_dist/sl_dist, 2)}:1)"
    return False, None

def validate_signal_with_ai(message_text, signal, channel_name):
    try:
        response = claude.messages.create(
            model="claude-opus-4-5",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": f"""Score this trading signal quality from 1-10.
Channel: {channel_name}
Message: {message_text}
Extracted: {json.dumps(signal)}

Criteria: clear entry, reasonable SL/TP, valid asset, RR ratio >= 1:1, not vague.
Respond in JSON only: {{"score": 7, "reason": "brief reason"}}"""
            }]
        )
        return json.loads(response.content[0].text)
    except Exception as e:
        print(f"AI validator error: {e}")
        return {"score": 5, "reason": "Parse error"}

def is_signal_channel(username):
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('SignalChannels')
        records = ws.get_all_records()
        for row in records:
            if row.get('Channel Username', '').lower() == username.lower():
                return row.get('Active', '') == 'TRUE'
        return False
    except Exception as e:
        print(f"Error checking signal channel: {e}")
        return False

def add_signal_channel(username, name):
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('SignalChannels')
        ws.append_row([username, name, 'TRUE', now_baku()])
        print(f"Added {username} to SignalChannels")
    except Exception as e:
        print(f"Error adding channel: {e}")

def analyze_channel_history(messages):
    try:
        text = '\n'.join(messages[:50])
        response = claude.messages.create(
            model="claude-opus-4-5",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": f"""Analyze these Telegram channel messages and determine if this is a trading signal channel.
Look for: entry prices, stop loss, take profit, asset names (XAUUSD, BTC, etc), BUY/SELL directions.

Messages:
{text}

Respond in JSON only:
{{"is_signal_channel": true, "confidence": 85, "reason": "brief reason"}}"""
            }]
        )
        return json.loads(response.content[0].text)
    except Exception as e:
        print(f"analyze_channel_history error: {e}")
        return {"is_signal_channel": False, "confidence": 0, "reason": "Parse error"}

def extract_signal(message_text, channel_name):
    try:
        response = claude.messages.create(
            model="claude-opus-4-5",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": f"""Extract trading signal from this message.
Channel: {channel_name}
Message: {message_text}

Respond in JSON only (use null for missing fields):
{{"is_signal": true, "asset": "XAUUSD", "direction": "BUY", "entry": 4700, "stop_loss": 4650, "tp1": 4750, "tp2": 4800, "tp3": null, "tp4": null, "tp5": null, "confidence": "High"}}"""
            }]
        )
        return json.loads(response.content[0].text)
    except Exception as e:
        print(f"extract_signal error: {e}")
        return {"is_signal": False}

def save_signal(signal, channel_name, message_text):
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('Signals')
        signal_id = f"{signal['asset']}_{datetime.now(BAKU).strftime('%Y%m%d%H%M%S')}"
        ws.append_row([
            signal_id,
            now_baku(),
            signal.get('asset', ''),
            channel_name,
            signal.get('direction', ''),
            signal.get('entry', ''),
            signal.get('stop_loss', ''),
            signal.get('tp1', ''),
            signal.get('tp2', ''),
            signal.get('tp3', ''),
            signal.get('tp4', ''),
            signal.get('tp5', ''),
            signal.get('confidence', ''),
            message_text[:200],
            'OPEN'
        ])
        return signal_id
    except Exception as e:
        print(f"Error saving signal: {e}")
        return None

async def main():
    user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    bot_client = TelegramClient('bot_session', API_ID, API_HASH)

    await bot_client.start(bot_token=BOT_TOKEN)
    await user_client.connect()

    if not await user_client.is_user_authorized():
        print("ERROR: SESSION_STRING is invalid or expired. Re-generate it.")
        return

    print("Telethon started! User client authorized.")

    # Test sheets connection on startup
    try:
        sheet = get_sheets()
        tabs = [ws.title for ws in sheet.worksheets()]
        print(f"Google Sh
