import os
import asyncio
import json
from telethon import TelegramClient, events
from telethon.tl.types import Channel
import anthropic
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# Environment variables
API_ID = int(os.environ.get('API_ID'))
API_HASH = os.environ.get('API_HASH')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
PERSONAL_CHAT_ID = int(os.environ.get('PERSONAL_CHAT_ID'))
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')

# Initialize Anthropic
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Initialize Google Sheets
def get_sheets():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scope = ['https://spreadsheets.google.com/feeds',
             'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)

# Check if channel is in SignalChannels sheet
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

# Add new channel to SignalChannels sheet
def add_signal_channel(username, name):
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('SignalChannels')
        ws.append_row([username, name, 'TRUE', datetime.now().strftime('%Y-%m-%d')])
        print(f"Added {username} to SignalChannels")
    except Exception as e:
        print(f"Error adding channel: {e}")

# Check channel history with Claude
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
{{"is_signal_channel": true/false, "confidence": 0-100, "reason": "brief reason"}}"""
        }]
    )
    try:
        return json.loads(response.content[0].text)
    except:
        return {"is_signal_channel": False, "confidence": 0, "reason": "Parse error"}

# Extract signal with Claude
def extract_signal(message_text, channel_name):
    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": f"""Extract trading signal from this message. 
Channel: {channel_name}

Message: {message_text}

Respond in JSON only (null for missing fields):
{{
  "is_signal": true/false,
  "asset": "XAUUSD/XAGUSD/BRENTOIL/BTCUSD/ETHUSD/etc",
  "direction": "BUY/SELL",
  "entry": number,
  "stop_loss": number,
  "tp1": number,
  "tp2": number,
  "tp3": number,
  "tp4": number,
  "tp5": number,
  "confidence": "High/Medium/Low"
}}"""
        }]
    )
    try:
        return json.loads(response.content[0].text)
    except:
        return {"is_signal": False}

# Save signal to Google Sheets
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

# Main Telethon client
async def main():
    # User client (reads channels)
    user_client = TelegramClient('user_session', API_ID, API_HASH)
    
    # Bot client (sends messages to you)
    bot_client = TelegramClient('bot_session', API_ID, API_HASH)
    await bot_client.start(bot_token=BOT_TOKEN)
    
    await user_client.start()
    print("Telethon started!")

    @user_client.on(events.NewMessage)
    async def handler(event):
        try:
            # Only process channel messages
            if not event.is_channel:
                return

            chat = await event.get_chat()
            username = f"@{chat.username}" if chat.username else str(chat.id)
            channel_name = chat.title

            # Check if this is a known signal channel
            if not is_signal_channel(username):
                # New channel — analyze history
                messages = []
                async for msg in user_client.iter_messages(chat, limit=50):
                    if msg.text:
                        messages.append(msg.text)
                
                analysis = analyze_channel_history(messages)
                
                if analysis['confidence'] >= 80:
