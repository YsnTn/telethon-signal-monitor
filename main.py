import os
import asyncio
import json
import time
import signal as signal_module
from aiohttp import web
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.custom import Button
import anthropic
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from pytz import timezone as pytz_timezone

print("Starting up...")

BAKU = pytz_timezone('Asia/Baku')

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
CACHE_TTL = 900
TRIAL_DAYS = 7
TRIAL_MIN_SIGNALS = 5
TRIAL_MIN_WIN_RATE = 50

MAX_SCANS_PER_HOUR = 5
channel_scan_cooldown = {}
scan_count_this_hour = 0
scan_hour_reset = time.time()
pending_channels = set()

signal_channels_cache = {}
signal_channels_cache_time = 0

print("All env vars loaded.")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
bot_client_ref = None

def now_baku():
    return datetime.now(BAKU).strftime('%Y-%m-%d %H:%M:%S')

def can_scan_channel(username):
    global scan_count_this_hour, scan_hour_reset
    now = time.time()
    if now - scan_hour_reset > 3600:
        scan_count_this_hour = 0
        scan_hour_reset = now
    if scan_count_this_hour >= MAX_SCANS_PER_HOUR:
        print("Rate limit: max scans per hour reached")
        return False
    last_scan = channel_scan_cooldown.get(username, 0)
    if now - last_scan < 86400:
        return False
    return True

def mark_channel_scanned(username):
    global scan_count_this_hour
    channel_scan_cooldown[username] = time.time()
    scan_count_this_hour += 1

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

def get_signal_channels():
    global signal_channels_cache, signal_channels_cache_time
    now = time.time()
    if now - signal_channels_cache_time < CACHE_TTL and signal_channels_cache:
        return signal_channels_cache
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('SignalChannels')
        records = ws.get_all_records()
        new_cache = {}
        for row in records:
            username = str(row.get('Channel Username', '')).lower().strip()
            if username:
                new_cache[username] = {
                    'active': str(row.get('Active', '')) == 'TRUE',
                    'status': str(row.get('Status', 'PENDING')).upper().strip()
                }
        signal_channels_cache = new_cache
        signal_channels_cache_time = now
        print("SignalChannels cache refreshed. Count: " + str(len(signal_channels_cache)))
        return signal_channels_cache
    except Exception as e:
        print("Error refreshing signal channels cache: " + str(e))
        signal_channels_cache_time = now + 60
        return signal_channels_cache

def is_signal_channel(username):
    channels = get_signal_channels()
    entry = channels.get(str(username).lower().strip())
    if not entry:
        return False
    return entry['active'] and entry['status'] == 'ACTIVE'

def is_known_channel(username):
    channels = get_signal_channels()
    return str(username).lower().strip() in channels

def get_channel_status(username):
    channels = get_signal_channels()
    entry = channels.get(str(username).lower().strip())
    if not entry:
        return None
    return entry['status']

def invalidate_channel_cache():
    global signal_channels_cache_time
    signal_channels_cache_time = 0

def add_pending_channel(username, name):
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('SignalChannels')
        ws.append_row([
            str(username), str(name), 'FALSE',
            now_baku(), 'PENDING', now_baku(), 0, 0, 0
        ])
        invalidate_channel_cache()
        print("Added " + str(username) + " as PENDING")
    except Exception as e:
        print("Error adding pending channel: " + str(e))

def update_signal_status(signal_id, status):
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('Signals')
        records = ws.get_all_records()
        for i, row in enumerate(records):
            if str(row.get('Signal ID', '')) == str(signal_id):
                ws.update('O' + str(i + 2), [[status]])
                return True
        return False
    except Exception as e:
        print("Error updating signal status: " + str(e))
        return False

def get_signal_info(signal_id):
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('Signals')
        records = ws.get_all_records()
        for row in records:
            if str(row.get('Signal ID', '')) == str(signal_id):
                return row
        return None
    except Exception as e:
        print("Error getting signal info: " + str(e))
        return None

def update_trial_stats(channel_name, hit_type):
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('SignalChannels')
        records = ws.get_all_records()
        is_tp = hit_type.startswith('TP')
        for i, row in enumerate(records):
            row_channel = str(row.get('Channel Name', '')).lower().strip()
            row_username = str(row.get('Channel Username', '')).lower().strip()
            if row_channel == str(channel_name).lower().strip() or row_username == str(channel_name).lower().strip():
                row_num = i + 2
                status = str(row.get('Status', '')).upper().strip()
                if status != 'PENDING':
                    return None
                trial_signals = int(row.get('Trial Signals', 0)) + 1
                trial_tp = int(row.get('Trial TP Hits', 0)) + (1 if is_tp else 0)
                trial_sl = int(row.get('Trial SL Hits', 0)) + (0 if is_tp else 1)
                trial_start = str(row.get('Trial Start', now_baku()))
                try:
                    start_dt = datetime.strptime(trial_start[:10], '%Y-%m-%d')
                    days_elapsed = (datetime.now() - start_dt).days
                except:
                    days_elapsed = 0
                win_rate = round((trial_tp / trial_signals) * 100, 1) if trial_signals > 0 else 0
                ws.update('G' + str(row_num) + ':I' + str(row_num), [[trial_signals, trial_tp, trial_sl]])
                if trial_signals >= TRIAL_MIN_SIGNALS and days_elapsed >= TRIAL_DAYS:
                    approve = win_rate >= TRIAL_MIN_WIN_RATE
                    ws.update('C' + str(row_num), [['TRUE' if approve else 'FALSE']])
                    ws.update('E' + str(row_num), [['ACTIVE' if approve else 'BLOCKED']])
                    invalidate_channel_cache()
                    return {
                        'graduated': True,
                        'approved': approve,
                        'win_rate': win_rate,
                        'trial_signals': trial_signals,
                        'channel_name': str(row.get('Channel Name', channel_name)),
                        'username': str(row.get('Channel Username', channel_name))
                    }
                return {'graduated': False, 'win_rate': win_rate, 'trial_signals': trial_signals, 'days_elapsed': days_elapsed}
        return None
    except Exception as e:
        print("Error updating trial stats: " + str(e))
        return None

def get_trust_score(channel_name):
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('ChannelScores')
        records = ws.get_all_records()
        for row in records:
            if str(row.get('Channel Name', '')).lower() == str(channel_name).lower():
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
            if str(row.get('Channel Name', '')).lower() == str(channel_name).lower():
                row_num = i + 2
                total = int(row.get('Total Signals', 0)) + 1
                tp_hits = int(row.get('TP Hits', 0)) + (1 if is_tp else 0)
                sl_hits = int(row.get('SL Hits', 0)) + (0 if is_tp else 1)
                hit_rate = round((tp_hits / total) * 100, 1)
                weight = min(total / 10, 1.0)
                trust_score = round(hit_rate * weight, 1)
                ws.update('B' + str(row_num) + ':G' + str(row_num), [[total, tp_hits, sl_hits, hit_rate, trust_score, now_baku()]])
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
            messages=[{"role": "user", "content": "Score this trading signal quality from 1-10.\nChannel: " + str(channel_name) + "\nMessage: " + str(message_text) + "\nExtracted: " + json.dumps(signal) + "\n\nCriteria: clear entry, reasonable SL/TP, valid asset, RR ratio >= 1:1, not vague.\nRespond in JSON only, no markdown: {\"score\": 7, \"reason\": \"brief reason\"}"}]
        )
        result = parse_claude_json(response.content[0].text)
        return result if result else {"score": 5, "reason": "Parse error"}
    except Exception as e:
        print("AI validator error: " + str(e))
        return {"score": 5, "reason": "Error"}

def analyze_channel_history(messages):
    try:
        if len(messages) < 3:
            return {"is_signal_channel": False, "confidence": 0, "reason": "Too few messages"}
        text = '\n'.join(messages[:50])
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": "Analyze these Telegram channel messages. Is this a TRADING SIGNAL channel?\n\nA trading signal channel MUST have ALL of these:\n- Specific BUY or SELL direction\n- Specific entry price\n- Specific Stop Loss price\n- At least one Take Profit price\n- At least 3 such signals visible in the messages\n\nNEWS channels, price update channels, analysis channels, and educational channels are NOT signal channels even if they mention prices or assets.\n\nMessages:\n" + text + "\n\nRespond in JSON only, no markdown: {\"is_signal_channel\": true, \"confidence\": 85, \"reason\": \"brief reason\"}\n\nBe strict. If fewer than 3 complete signals with entry+SL+TP are visible, confidence must be below 40."}]
        )
        result = parse_claude_json(response.content[0].text)
        return result if result else {"is_signal_channel": False, "confidence": 0, "reason": "Parse error"}
    except Exception as e:
        print("analyze_channel_history error: " + str(e))
        return {"is_signal_channel": False, "confidence": 0, "reason": "Error"}

def extract_signal(message_text, channel_name):
    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": "Extract trading signal from this message.\nChannel: " + str(channel_name) + "\nMessage: " + str(message_text) + "\n\nRespond in JSON only, no markdown (use null for missing fields):\n{\"is_signal\": true, \"asset\": \"XAUUSD\", \"direction\": \"BUY\", \"entry\": 4700, \"stop_loss\": 4650, \"tp1\": 4750, \"tp2\": 4800, \"tp3\": null, \"tp4\": null, \"tp5\": null, \"confidence\": \"High\"}"}]
        )
        result = parse_claude_json(response.content[0].text)
        return result if result else {"is_signal": False}
    except Exception as e:
        print("extract_signal error: " + str(e))
        return {"is_signal": False}

def save_signal(signal, channel_name, message_text, status='OPEN'):
    try:
        sheet = get_sheets()
        ws = sheet.worksheet('Signals')
        signal_id = str(signal['asset']) + '_' + datetime.now(BAKU).strftime('%Y%m%d%H%M%S')
        ws.append_row([signal_id, now_baku(), signal.get('asset', ''), channel_name, signal.get('direction', ''), signal.get('entry', ''), signal.get('stop_loss', ''), signal.get('tp1', ''), signal.get('tp2', ''), signal.get('tp3', ''), signal.get('tp4', ''), signal.get('tp5', ''), signal.get('confidence', ''), str(message_text)[:200], status])
        return signal_id
    except Exception as e:
        print("Error saving signal: " + str(e))
        return None

async def handle_trial_update(request):
    try:
        data = await request.json()
        channel = data.get('channel', '')
        hit_type = data.get('hitType', '')
        if not channel or not hit_type:
            return web.json_response({'error': 'missing channel or hitType'}, status=400)
        result = update_trial_stats(channel, hit_type)
        if result and result.get('graduated'):
            approved = result.get('approved')
            win_rate = result.get('win_rate')
            trial_signals = result.get('trial_signals')
            channel_name = result.get('channel_name', channel)
            username = result.get('username', channel)
            if approved:
                msg = "CHANNEL APPROVED\n\nChannel: " + str(username) + "\nName: " + str(channel_name) + "\nWin Rate: " + str(win_rate) + "%\nTrial Signals: " + str(trial_signals) + "\n\nNow receiving live signal alerts."
            else:
                msg = "CHANNEL BLOCKED\n\nChannel: " + str(username) + "\nWin Rate: " + str(win_rate) + "% (below 50%)\nTrial Signals: " + str(trial_signals)
            if bot_client_ref:
                asyncio.create_task(bot_client_ref.send_message(PERSONAL_CHAT_ID, msg))
        return web.json_response({'ok': True, 'result': result})
    except Exception as e:
        print("Webhook error: " + str(e))
        return web.json_response({'error': str(e)}, status=500)

async def handle_score_update(request):
    try:
        data = await request.json()
        channel = data.get('channel', '')
        hit_type = data.get('hitType', '')
        if not channel or not hit_type:
            return web.json_response({'error': 'missing channel or hitType'}, status=400)
        trust_score = update_channel_score(channel, hit_type)
        return web.json_response({'ok': True, 'trust_score': trust_score})
    except Exception as e:
        print("Score update webhook error: " + str(e))
        return web.json_response({'error': str(e)}, status=500)

async def handle_health(request):
    return web.json_response({'status': 'ok', 'time': now_baku()})

async def start_webhook_server():
    app = web.Application()
    app.router.add_post('/trial-update', handle_trial_update)
    app.router.add_post('/score-update', handle_score_update)
    app.router.add_get('/health', handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    print("Webhook server started on port 8080")

async def main():
    global bot_client_ref
    print("Entering main()...")

    user_client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH,
        device_model="iPhone 15",
        system_version="17.0",
        app_version="10.3.2",
        lang_code="en",
        system_lang_code="en",
        connection_retries=1,
        retry_delay=5,
        auto_reconnect=False
    )

    bot_client = TelegramClient('bot_session', API_ID, API_HASH)

    def graceful_shutdown(signum, frame):
        print("Received shutdown signal, disconnecting...")
        asyncio.create_task(user_client.disconnect())
        asyncio.create_task(bot_client.disconnect())

    signal_module.signal(signal_module.SIGTERM, graceful_shutdown)
    signal_module.signal(signal_module.SIGINT, graceful_shutdown)

    print("Starting bot client...")
    await bot_client.start(bot_token=BOT_TOKEN)
    bot_client_ref = bot_client
    print("Bot client started.")

    print("Connecting user client...")
    await user_client.connect()
    print("User client connected.")

    if not await user_client.is_user_authorized():
        print("ERROR: SESSION_STRING is invalid or expired.")
        return

    print("Telethon started! User client authorized.")

    try:
        sheet = get_sheets()
        tabs = [ws.title for ws in sheet.worksheets()]
        print("Google Sheets connected. Tabs: " + str(tabs))
        get_signal_channels()
    except Exception as e:
        print("STARTUP Google Sheets ERROR: " + str(e))

    await start_webhook_server()

    @bot_client.on(events.CallbackQuery)
    async def callback_handler(event):
        try:
            data = event.data.decode('utf-8')
            if data.startswith('track_'):
                signal_id = data[6:]
                success = update_signal_status(signal_id, 'OPEN_TRACKED')
                if success:
                    signal = get_signal_info(signal_id)
                    asset = signal.get('Asset', '') if signal else ''
                    direction = signal.get('Direction', '') if signal else ''
                    entry = signal.get('Entry', '') if signal else ''
                    await event.answer("Tracking signal!")
                    await event.edit(
                        "TRACKING\n\nID: " + str(signal_id) + "\nAsset: " + str(asset) + "\nDirection: " + str(direction) + "\nEntry: " + str(entry) + "\n\nYou will be notified when TP or SL is hit.",
                        buttons=[[Button.inline("🛑 Stop Tracking", data="stoptrack_" + signal_id)]]
                    )
                else:
                    await event.answer("Error tracking signal.")
            elif data.startswith('skip_'):
                signal_id = data[5:]
                update_signal_status(signal_id, 'SKIPPED')
                await event.answer("Signal skipped.")
                await event.edit("SKIPPED: " + signal_id)
            elif data.startswith('stoptrack_'):
                signal_id = data[10:]
                update_signal_status(signal_id, 'CLOSED')
                await event.answer("Stopped tracking.")
                await event.edit("CLOSED: " + signal_id + "\nTracking stopped manually.")
            elif data.startswith('addtrial_'):
                username_clean = data[9:]
                username = '@' + username_clean
                add_pending_channel(username, username_clean)
                await event.answer("Channel added to trial!")
                await event.edit("TRIAL STARTED\n\nChannel: " + username + "\nMonitoring for " + str(TRIAL_DAYS) + " days / " + str(TRIAL_MIN_SIGNALS) + "+ signals.")
            elif data.startswith('skipchannel_'):
                username = '@' + data[12:]
                await event.answer("Channel skipped.")
                await event.edit("SKIPPED channel: " + username)
        except Exception as e:
            print("Callback error: " + str(e))

    @user_client.on(events.NewMessage)
    async def handler(event):
        try:
            if not event.is_channel:
                return
            chat = await event.get_chat()
            username = "@" + chat.username if chat.username else str(chat.id)
            channel_name = chat.title

            if not is_known_channel(username):
                if not can_scan_channel(username):
                    return
                if username in pending_channels:
                    return
                pending_channels.add(username)
                try:
                    mark_channel_scanned(username)
                    await asyncio.sleep(2)
                    messages = []
                    async for msg in user_client.iter_messages(chat, limit=50):
                        if msg.text:
                            messages.append(msg.text)
                        await asyncio.sleep(0.1)
                    analysis = analyze_channel_history(messages)
                    if analysis['confidence'] >= 70:
                        add_pending_channel(username, channel_name)
                        await bot_client.send_message(PERSONAL_CHAT_ID, "TRIAL STARTED\n\nChannel: " + str(username) + "\nName: " + str(channel_name) + "\nConfidence: " + str(analysis['confidence']) + "%\nReason: " + str(analysis['reason']) + "\n\nMonitoring for " + str(TRIAL_DAYS) + " days / " + str(TRIAL_MIN_SIGNALS) + "+ signals.")
                    elif analysis['confidence'] >= 40:
                        msg = "Possible signal channel: " + str(username) + "\nName: " + str(channel_name) + "\nConfidence: " + str(analysis['confidence']) + "%\nReason: " + str(analysis['reason']) + "\n\nDo you want to add this channel to trial?"
                        buttons = [[Button.inline("✅ Add to Trial", data="addtrial_" + str(username).replace('@', '')), Button.inline("❌ Skip", data="skipchannel_" + str(username).replace('@', ''))]]
                        await bot_client.send_message(PERSONAL_CHAT_ID, msg, buttons=buttons)
                finally:
                    pending_channels.discard(username)
                return

            channel_status = get_channel_status(username)

            if channel_status == 'PENDING':
                if not event.message.text:
                    return
                signal = extract_signal(event.message.text, channel_name)
                if not signal.get('is_signal'):
                    return
                is_fake, _ = is_fake_signal(signal, channel_name)
                if is_fake:
                    return
                validation = validate_signal_with_ai(event.message.text, signal, channel_name)
                if validation['score'] < 6:
                    return
                save_signal(signal, channel_name, event.message.text, status='TRIAL')
                print("Trial signal saved from " + str(channel_name))
                return

            if channel_status == 'BLOCKED':
                return

            if not is_signal_channel(username):
                return

            if not event.message.text:
                return

            signal = extract_signal(event.message.text, channel_name)
            if not signal.get('is_signal'):
                return

            is_fake, fake_reason = is_fake_signal(signal, channel_name)
            if is_fake:
                print("Fake signal dropped from " + str(channel_name) + ": " + str(fake_reason))
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

            signal_id = save_signal(signal, channel_name, event.message.text, status='OPEN')
            if signal_id:
                msg = ("NEW SIGNAL\n\nID: " + str(signal_id) + "\nAsset: " + str(signal.get('asset')) + "\nDirection: " + str(signal.get('direction')) + "\nEntry: " + str(entry) + "\nSL: " + str(sl) + "\nTP1: " + str(signal.get('tp1')) + "\nTP2: " + str(signal.get('tp2')) + "\nAI Score: " + str(validation['score']) + "/10\nLot Size: " + lot_text + " (2% risk / $10)\nChannel Trust: " + trust_label + " (" + str(trust_score) + "% / " + str(total_signals) + " signals)\nChannel: " + str(channel_name) + "\n\nDo you want to track this signal?")
                buttons = [[Button.inline("✅ Track", data="track_" + signal_id), Button.inline("❌ Skip", data="skip_" + signal_id)]]
                await bot_client.send_message(PERSONAL_CHAT_ID, msg, buttons=buttons)
        except Exception as e:
            print("Error processing message: " + str(e))

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )

if __name__ == '__main__':
    asyncio.run(main())
