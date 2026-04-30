import os
import asyncio
import json
from telethon import TelegramClient, events
from telethon.sessions import StringSession  # ← ADDED
import anthropic
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

API_ID = int(os.environ.get('API_ID'))
API_HASH = os.environ.get('API_HASH')
SESSION_STRING = os.environ.get('SESSION_STRING')  # ← ADDED
BOT_TOKEN = os.environ.get('BOT_TOKEN')
PERSONAL_CHAT_ID = int(os.environ.get('PERSONAL_CHAT_ID'))
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def get_sheets():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)

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
        ws.append_row([username, name, 'TRUE', datetime.now().strftime('%Y-%m-%d')])
        print(f"Added {username} to SignalChannels")
    except Exception as e:
        print(f"Error adding channel: {e}")

def analyze_channel_history(messages):
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
    try:
        return json.loads(response.content[0].text)
    except Exception:
        return {"is_signal_channel": False, "confidence": 0, "reason": "Parse error"}

def extract_signal(message_text, channel_name):
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
    try:
        return json.loads(response.content[0].text)
    except Exception:
        return {"is_signal": False}

def save_signal(signal, channel_name, message_text):
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('Signals')
        signal_id = f"{signal['asset']}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        ws.append_row([
            signal_id,
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
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
            'PENDING'
        ])
        return signal_id
    except Exception as e:
        print(f"Error saving signal: {e}")
        return None

async def main():
    # ← CHANGED: StringSession instead of file-based 'user_session'
    user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    bot_client = TelegramClient('bot_session', API_ID, API_HASH)

    await bot_client.start(bot_token=BOT_TOKEN)
    await user_client.connect()  # ← CHANGED: connect() not start() — no interactive prompt

    if not await user_client.is_user_authorized():
        print("ERROR: SESSION_STRING is invalid or expired. Re-generate it.")
        return

    print("Telethon started! User client authorized.")

    @user_client.on(events.NewMessage)
    async def handler(event):
        try:
            if not event.is_channel:
                return
            chat = await event.get_chat()
            username = f"@{chat.username}" if chat.username else str(chat.id)
            channel_name = chat.title

            if not is_signal_channel(username):
                messages = []
                async for msg in user_client.iter_messages(chat, limit=50):
                    if msg.text:
                        messages.append(msg.text)
                analysis = analyze_channel_history(messages)
                if analysis['confidence'] >= 80:
                    add_signal_channel(username, channel_name)
                    await bot_client.send_message(
                        PERSONAL_CHAT_ID,
                        f"✅ New signal channel detected!\n{username} added automatically\nConfidence: {analysis['confidence']}%\nReason: {analysis['reason']}"
                    )
                elif analysis['confidence'] >= 40:
                    await bot_client.send_message(
                        PERSONAL_CHAT_ID,
                        f"⚠️ Possible signal channel: {username}\nConfidence: {analysis['confidence']}%\nReason: {analysis['reason']}\n\nReply /add_{chat.id} to add or /skip_{chat.id} to ignore"
                    )
                    return
                else:
                    return

            if not event.message.text:
                return

            signal = extract_signal(event.message.text, channel_name)
            if not signal.get('is_signal'):
                return

            signal_id = save_signal(signal, channel_name, event.message.text)
            if signal_id:
                await bot_client.send_message(
                    PERSONAL_CHAT_ID,
                    f"📡 NEW SIGNAL\n\n"
                    f"ID: {signal_id}\n"
                    f"Asset: {signal.get('asset')}\n"
                    f"Direction: {signal.get('direction')}\n"
                    f"Entry: {signal.get('entry')}\n"
                    f"SL: {signal.get('stop_loss')}\n"
                    f"TP1: {signal.get('tp1')}\n"
                    f"TP2: {signal.get('tp2')}\n"
                    f"Confidence: {signal.get('confidence')}\n"
                    f"Channel: {channel_name}\n\n"
                    f"Track this signal?\n"
                    f"/yes_{signal_id} or /no_{signal_id}"
                )
        except Exception as e:
            print(f"Error processing message: {e}")

    @bot_client.on(events.NewMessage(pattern=r'/yes_(.+)'))
    async def yes_handler(event):
        signal_id = event.pattern_match.group(1)
        try:
            sheet = get_sheets()
            ws = sheet.worksheet('Signals')
            cell = ws.find(signal_id)
            if cell:
                ws.update_cell(cell.row, 15, 'OPEN')
                await event.reply(f"✅ Signal {signal_id} is now being tracked!")
        except Exception as e:
            await event.reply(f"Error: {e}")

    @bot_client.on(events.NewMessage(pattern=r'/no_(.+)'))
    async def no_handler(event):
        signal_id = event.pattern_match.group(1)
        try:
            sheet = get_sheets()
            ws = sheet.worksheet('Signals')
            cell = ws.find(signal_id)
            if cell:
                ws.update_cell(cell.row, 15, 'IGNORED')
                await event.reply(f"❌ Signal {signal_id} ignored!")
        except Exception as e:
            await event.reply(f"Error: {e}")

    @bot_client.on(events.NewMessage(pattern=r'/add_(.+)'))
    async def add_handler(event):
        chat_id = event.pattern_match.group(1)
        try:
            chat = await user_client.get_entity(int(chat_id))
            username = f"@{chat.username}" if chat.username else str(chat.id)
            add_signal_channel(username, chat.title)
            await event.reply(f"✅ {username} added as signal channel!")
        except Exception as e:
            await event.reply(f"Error: {e}")

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )

if __name__ == '__main__':
    asyncio.run(main())
