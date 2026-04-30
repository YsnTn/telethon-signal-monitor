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

print("Starting up...")
print("API_ID set: " + str(bool(os.environ.get('API_ID'))))
print("SESSION_STRING set: " + str(bool(os.environ.get('SESSION_STRING'))))
print("BOT_TOKEN set: " + str(bool(os.environ.get('BOT_TOKEN'))))
print("GOOGLE_CREDENTIALS set: " + str(bool(os.environ.get('GOOGLE_CREDENTIALS'))))
print("SPREADSHEET_ID set: " + str(bool(os.environ.get('SPREADSHEET_ID'))))

BAKU = timezone('Asia/Baku')

API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
SESSION_STRING = os.environ.get('SESSION_STRING', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
PERSONAL_CHAT_ID = int(os.environ.get('PERSONAL_CHAT_ID', 0))
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS', '')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID', '')
ACCOUNT_SIZE = 500
RISK_PERCENT = 0.02
pending_channels = set()

print("All env vars loaded.")

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
        return True, "Poor RR ratio (" + str(round(tp_dist/sl_dist, 2)) + ":1)"
    return False, None

def parse_claude_json(text):
    try:
        text = text.strip()
        if not text:
            return None
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                try:
                    return json.loads(part)
                except:
                    continue
        return json.loads(text)
    except Exception as e:
        print("JSON parse error: " + str(e) + " | text: " + str(text[:100]))
        return None

def validate_signal_with_ai(message_text, signal, channel_name):
    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": "Score this trading signal quality from 1-10.\nChannel: " + channel_name + "\nMessage: " + message_text + "\nExtracted: " + json.dumps(signal) + "\n\nCriteria: clear entry, reasonable SL/TP, valid asset, RR ratio >= 1:1, not vague.\nRespond in JSON only, no markdown: {\"score\": 7, \"reason\": \"brief reason\"}"
            }]
        )
        result = parse_claude_json(response.content[0].text)
        if result:
            return result
        return {"score": 5, "reason": "Parse error"}
    except Exception as e:
        print("AI validator error: " + str(e))
        return {"score": 5, "reason": "Error"}

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
        print("Error checking signal channel: " + str(e))
        return False

def add_signal_channel(username, name):
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('SignalChannels')
        ws.append_row([username, name, 'TRUE', now_baku()])
        print("Added " + username + " to SignalChannels")
    except Exception as e:
        print("Error adding channel: " + str(e))

def analyze_channel_history(messages):
    try:
        text = '\n'.join(messages[:50])
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": "Analyze these Telegram channel messages and determine if this is a trading signal channel.\nLook for: entry prices, stop loss, take profit, asset names (XAUUSD, BTC, etc), BUY/SELL directions.\n\nMessages:\n" + text + "\n\nRespond in JSON only, no markdown: {\"is_signal_channel\": true, \"confidence\": 85, \"reason\": \"brief reason\"}"
            }]
        )
        result = parse_claude_json(response.content[0].text)
        if result:
            return result
        return {"is_signal_channel": False, "confidence": 0, "reason": "Parse error"}
    except Exception as e:
        print("analyze_channel_history error: " + str(e))
        return {"is_signal_channel": False, "confidence": 0, "reason": "Error"}

def extract_signal(message_text, channel_name):
    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": "Extract trading signal from this message.\nChannel: " + channel_name + "\nMessage: " + message_text + "\n\nRespond in JSON only, no markdown (use null for missing fields):\n{\"is_signal\": true, \"asset\": \"XAUUSD\", \"direction\": \"BUY\", \"entry\": 4700, \"stop_loss\": 4650, \"tp1\": 4750, \"tp2\": 4800, \"tp3\": null, \"tp4\": null, \"tp5\": null, \"confidence\": \"High\"}"
            }]
        )
        result = parse_claude_json(response.content[0].text)
        if result:
            return result
        return {"is_signal": False}
    except Exception as e:
        print("extract_signal error: " + str(e))
        return {"is_signal": False}

def save_signal(signal, channel_name, message_text):
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('Signals')
        signal_id = signal['asset'] + '_' + datetime.now(BAKU).strftime('%Y%m%d%H%M%S')
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
        print("Error saving signal: " + str(e))
        return None

async def main():
    print("Entering main()...")

    user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    bot_client = TelegramClient('bot_session', API_ID, API_HASH)

    print("Starting bot client...")
    await bot_client.start(bot_token=BOT_TOKEN)
    print("Bot client started.")

    print("Connecting user client...")
    await user_client.connect()
    print("User client connected.")

    if not await user_client.is_user_authorized():
        print("ERROR: SESSION_STRING is invalid or expired. Re-generate it.")
        return

    print("Telethon started! User client authorized.")

    try:
        sheet = get_sheets()
        tabs = [ws.title for ws in sheet.worksheets()]
        print("Google Sheets connected. Tabs: " + str(tabs))
    except Exception as e:
        print("STARTUP Google Sheets ERROR: " + str(e))

    @user_client.on(events.NewMessage)
    async def handler(event):
        try:
            if not event.is_channel:
                return
            chat = await event.get_chat()
            username = "@" + chat.username if chat.username else str(chat.id)
            channel_name = chat.title

            if not is_signal_channel(username):
                if username in pending_channels:
                    return
                pending_channels.add(username)
                try:
                    messages = []
                    async for msg in user_client.iter_messages(chat, limit=50):
                        if msg.text:
                            messages.append(msg.text)
                    analysis = analyze_channel_history(messages)
                    if analysis['confidence'] >= 80:
                        add_signal_channel(username, channel_name)
                        await bot_client.send_message(
                            PERSONAL_CHAT_ID,
                            "New signal channel detected!\n" + username + " added automatically\nConfidence: " + str(analysis['confidence']) + "%\nReason: " + str(analysis['reason'])
                        )
                    elif analysis['confidence'] >= 40:
                        await bot_client.send_message(
                            PERSONAL_CHAT_ID,
                            "Possible signal channel: " + username + "\nConfidence: " + str(analysis['confidence']) + "%\nReason: " + str(analysis['reason'])
                        )
                finally:
                    pending_channels.discard(username)
                return

            if not event.message.text:
                return

            signal = extract_signal(event.message.text, channel_name)
            if not signal.get('is_signal'):
                return

            is_fake, fake_reason = is_fake_signal(signal, channel_name)
            if is_fake:
                print("Fake signal dropped from " + channel_name + ": " + str(fake_reason))
                return

            validation = validate_signal_with_ai(event.message.text, signal, channel_name)
            if validation['score'] < 6:
                print("Signal dropped (score " + str(validation['score']) + "): " + str(validation['reason']))
                return

            trust = get_trust_score(channel_name)
            trust_score = trust['trust_score']
            total_signals = trust['total_signals']
            if trust_score >= 70:
                trust_label = "High"
            elif trust_score >= 40:
                trust_label = "Medium"
            elif total_signals >= 5:
                trust_label = "Low"
            else:
                trust_label = "New"

            entry = signal.get('entry')
            sl = signal.get('stop_loss')
            lot_size = calculate_lot_size(signal.get('asset'), entry, sl) if entry and sl else None
            lot_text = str(lot_size) + " lots" if lot_size else "N/A"

            signal_id = save_signal(signal, channel_name, event.message.text)
            if signal_id:
                msg = (
                    "NEW SIGNAL\n\n"
                    "ID: " + signal_id + "\n"
                    "Asset: " + str(signal.get('asset')) + "\n"
                    "Direction: " + str(signal.get('direction')) + "\n"
                    "Entry: " + str(entry) + "\n"
                    "SL: " + str(sl) + "\n"
                    "TP1: " + str(signal.get('tp1')) + "\n"
                    "TP2: " + str(signal.get('tp2')) + "\n"
                    "AI Score: " + str(validation['score']) + "/10\n"
                    "Lot Size: " + lot_text + " (2% risk / $10)\n"
                    "Channel Trust: " + trust_label + " (" + str(trust_score) + "% / " + str(total_signals) + " signals)\n"
                    "Channel: " + channel_name
                )
                await bot_client.send_message(PERSONAL_CHAT_ID, msg)
        except Exception as e:
            print("Error processing message: " + str(e))

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )

if __name__ == '__main__':
    asyncio.run(main())
