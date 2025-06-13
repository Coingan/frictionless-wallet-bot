import time
import json
from web3 import Web3
from telegram import Bot
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import os
import logging
import threading
import requests
import matplotlib.pyplot as plt

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ---------------- CONFIG ---------------- #
CAMPAIGN_ADDRESS = os.getenv('CAMPAIGN_ADDRESS')
CAMPAIGN_TARGET_USD = float(os.getenv('CAMPAIGN_TARGET_USD', '50000'))
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_IDS = os.getenv('TELEGRAM_CHAT_ID', '').split(',')
ETHEREUM_RPC_URL = os.getenv('ETHEREUM_RPC_URL')

WALLETS_TO_TRACK = {
    '0xd9aD5Acc883D8a67ab612B70C11abF33dD450A45': 'FRIC/ETH',
    '0xda1916b0d6B209A143009214Cac95e771c4aa277': 'FRIC/ETH'
}

GLOBAL_LABEL = "Frictionless Whales"

ERC20_ABI = json.loads('''
[
  {
    "type": "function",
    "name": "symbol",
    "inputs": [],
    "outputs": [{"name": "", "type": "string"}],
    "stateMutability": "view"
  },
  {
    "type": "function",
    "name": "decimals",
    "inputs": [],
    "outputs": [{"name": "", "type": "uint8"}],
    "stateMutability": "view"
  },
  {
    "anonymous": false,
    "inputs": [
      {"indexed": true, "name": "from", "type": "address"},
      {"indexed": true, "name": "to", "type": "address"},
      {"indexed": false, "name": "value", "type": "uint256"}
    ],
    "name": "Transfer",
    "type": "event"
  }
]''')

# ---------------- SETUP ---------------- #
from flask import Flask, request
from telegram import Update
from telegram.ext import Dispatcher, CommandHandler, CallbackContext
w3 = Web3(Web3.HTTPProvider(ETHEREUM_RPC_URL))
last_checked = w3.eth.block_number
bot = Bot(token=TELEGRAM_BOT_TOKEN)
transfer_event_sig = w3.keccak(text="Transfer(address,address,uint256)").hex()

# ---------------- UTILS ---------------- #
def build_frictionless_message(tx_type, token_symbol, value, tx_hash, address):
    wallet_label = WALLETS_TO_TRACK.get(address, None)
    if not wallet_label:
        return None
    if tx_type == "incoming":
        return (
            f"🔔 *New Offer Created on the Frictionless Platform*\n\n"
            f"Token Offered: `{token_symbol}`\n"
            f"Amount: `{value:.4f}`\n"
            f"Switch: _{wallet_label}_\n"
            f"Channel: _{GLOBAL_LABEL}_\n"
            f"🔗 [View Transaction](https://etherscan.io/tx/{tx_hash})"
        )
    elif tx_type == "outgoing":
        return (
            f"🤝 *Contribution on Offer Wall*\n\n"
            f"Token Received: `{token_symbol}`\n"
            f"Amount: `{value:.4f}`\n"
            f"Switch: _{wallet_label}_\n"
            f"Channel: _{GLOBAL_LABEL}_\n"
            f"🔗 [View Transaction](https://etherscan.io/tx/{tx_hash})"
        )
    return None

def notify(message, tx_type=None):
    video_path = 'Friccy_whale.gif'
    if tx_type == "incoming":
        keyboard = [[InlineKeyboardButton("💰 Contribute Now", url="https://app.frictionless.network/contribute")]]
    elif tx_type == "outgoing":
        keyboard = [[InlineKeyboardButton("💰 Create an OTC offer", url="https://app.frictionless.network/create")]]
    else:
        keyboard = []
    reply_markup = InlineKeyboardMarkup(keyboard)

    for chat_id in TELEGRAM_CHAT_IDS:
        for attempt in range(3):
            try:
                bot.send_animation(chat_id=chat_id, animation=open(video_path, 'rb'), timeout=10)
                bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown', reply_markup=reply_markup, timeout=10)
                break  # success
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} failed to send Telegram message: {e}")
                time.sleep(2)
        else:
            logger.error(f"❌ All retries failed for message to chat_id {chat_id}")

# ---------------- MAIN LOGIC ---------------- #
def check_blocks():
    global last_checked
    latest = w3.eth.block_number
    logger.info(f"Checking blocks {last_checked + 1} to {latest}")
    seen_messages = set()

    for block_number in range(last_checked + 1, latest + 1):
        block = w3.eth.get_block(block_number, full_transactions=True)
        for tx in block.transactions:
            if tx['to'] is None and tx['from'] is None:
                continue

            to_address = w3.to_checksum_address(tx['to']) if tx['to'] else None
            from_address = w3.to_checksum_address(tx['from']) if tx['from'] else None

            if to_address not in WALLETS_TO_TRACK and from_address not in WALLETS_TO_TRACK:
                continue

            try:
                receipt = w3.eth.get_transaction_receipt(tx.hash)
                found_token_log = False

                for log in receipt.logs:
                    if len(log['topics']) != 3:
                        continue
                    if log['topics'][0].hex() == transfer_event_sig:
                        try:
                            contract = w3.eth.contract(address=log['address'], abi=ERC20_ABI)
                            from web3._utils.events import get_event_data
                            transfer_event_abi = [abi for abi in ERC20_ABI if abi.get("type") == "event" and abi.get("name") == "Transfer"][0]
                            decoded_log = get_event_data(w3.codec, transfer_event_abi, log)

                            from_addr = w3.to_checksum_address(decoded_log['args']['from'])
                            to_addr = w3.to_checksum_address(decoded_log['args']['to'])
                            value = decoded_log['args']['value']

                            if to_addr in WALLETS_TO_TRACK:
                                tx_type = "incoming"
                                tracked_addr = to_addr
                            elif from_addr in WALLETS_TO_TRACK:
                                if to_addr.lower() == "0x4ca9798a36b287f6675429884fab36563f82552d".lower():
                                    continue  # omit this specific outgoing address

                                tx_type = "outgoing"
                                tracked_addr = from_addr
                            else:
                                continue

                            try:
                                token_symbol = contract.functions.symbol().call()
                            except:
                                token_symbol = "UNKNOWN"

                            try:
                                decimals = contract.functions.decimals().call()
                            except:
                                decimals = 18

                            value_human = value / (10 ** decimals)
                            if value_human == 0:
                                continue

                            message = build_frictionless_message(tx_type, token_symbol, value_human, tx.hash.hex(), tracked_addr)
                            if message:
                                logger.info(f"Sending ERC20 message: {message[:100]}...")
                                notify(message, tx_type)
                                found_token_log = True
                        except Exception as e:
                            logger.warning(f"Decode error: {e}")

                if not found_token_log:
                    from_addr = tx['from']
                    to_addr = tx['to']
                    value = tx['value']

                    if from_addr in WALLETS_TO_TRACK or to_addr in WALLETS_TO_TRACK:
                        if tx_type == "outgoing" and to_addr.lower() == "0x4ca9798a36b287f6675429884fab36563f82552d".lower():
                            continue  # omit this specific outgoing address
                        if value == 0:
                            continue
                        tx_type = "incoming" if to_addr in WALLETS_TO_TRACK else "outgoing"
                        tracked_addr = to_addr if tx_type == "incoming" else from_addr
                        value_eth = w3.from_wei(value, 'ether')
                        message = build_frictionless_message(tx_type, 'ETH', value_eth, tx.hash.hex(), tracked_addr)
                        if message:
                            logger.info(f"Sending message: {message[:100]}...")
                            notify(message, tx_type)

            except Exception as e:
                if '429' in str(e):
                    logger.warning("Rate limited by RPC provider. Cooling down for 120 seconds.")
                    time.sleep(120)
                else:
                    logger.error(f"Receipt error: {e}")

    last_checked = latest

# ---------------- TELEGRAM COMMANDS ---------------- #
from telegram.ext import Dispatcher, CallbackContext
from telegram import Update

app = Flask(__name__)

# ✅ Launch scanner thread and webhook registration on app load
def run_scanner():
    logger.info("✅ Scanner thread started")
    while True:
        try:
            check_blocks()
            time.sleep(60)
        except Exception as e:
            logger.error(f"🔥 Main loop error: {repr(e)}")
            time.sleep(30)

scanner_thread = threading.Thread(target=run_scanner)
scanner_thread.daemon = True
scanner_thread.start()

webhook_url = os.environ.get('WEBHOOK_URL')
if webhook_url:
    bot.set_webhook(url=f"{webhook_url}/webhook")
    logger.info(f"Webhook set to {webhook_url}/webhook")

# ---------------- CAMPAIGN SUMMARY THREAD ---------------- #
def send_campaign_summary():
    try:
        bal_wei = w3.eth.get_balance(CAMPAIGN_ADDRESS)
        bal_eth = w3.from_wei(bal_wei, 'ether')
        res = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ethereum", "vs_currencies": "usd"}, timeout=10
        ).json()
        price_usd = res.get('ethereum', {}).get('usd', 0)
        current_usd = float(bal_eth) * price_usd
        percent = min(100, (current_usd / CAMPAIGN_TARGET_USD) * 100)

        # Build textual summary
        msg = (
            "*Fundraising Update*"
            f"Balance: `{bal_eth:.4f} ETH`"
            f"USD Value: `${current_usd:,.2f}` of `${CAMPAIGN_TARGET_USD:,.2f}`"
            f"Progress: `{percent:.1f}%`"
        )

        # Generate simple progress bar image
        fig, ax = plt.subplots(figsize=(6, 1))
        ax.barh(0, percent, color='green')
        ax.barh(0, 100 - percent, left=percent, color='lightgray')
        ax.set_xlim(0, 100)
        ax.axis('off')
        img_path = '/tmp/progress.png'
        fig.savefig(img_path, bbox_inches='tight')
        plt.close(fig)

        # Send to Telegram
        for chat_id in TELEGRAM_CHAT_IDS:
            try:
                bot.send_photo(chat_id=chat_id, photo=open(img_path, 'rb'), timeout=10)
            except Exception as e:
                logger.error(f"Failed to send campaign image: {e}")
            try:
                bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown', timeout=10)
            except Exception as e:
                logger.error(f"Failed to send campaign summary: {e}")
    except Exception as e:
        logger.error(f"Error in send_campaign_summary: {e}")

# ---------------- START SUMMARY THREAD ----------------
summary_thread = threading.Thread(target=run_summary)
summary_thread.daemon = True
summary_thread.start()(target=run_summary)
summary_thread.daemon = True
summary_thread.start()

@app.route('/', methods=['GET'])
def home():
    return 'Frictionless Wallet Bot is running.'

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), bot)
        dispatcher.process_update(update)
    return "ok"
start_time = time.time()

def start_command(update, context):
    update.message.reply_text("🚀 Frictionless bot is live and tracking blocks.")

def status_command(update, context):
    block = w3.eth.block_number
    update.message.reply_text(f"📡 Bot is synced. Current block: {block}")

def switches_command(update, context):
    switches = '\n'.join([f"{label}: `{addr}`" for addr, label in WALLETS_TO_TRACK.items()])
    update.message.reply_text(f"🔀 *Tracked Switches:*\n{switches}", parse_mode='Markdown')

def uptime_command(update, context):
    uptime_seconds = int(time.time() - start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    update.message.reply_text(f"⏱ Bot uptime: {hours}h {minutes}m {seconds}s")

def commands_command(update, context):
    commands_text = (
        "*Available Commands:*\n\n"
        "`/start` - Show startup confirmation\n"
        "`/status` - Show current block height\n"
        "`/switches` - List all contract addresses for tracked switches\n"
        "`/uptime` - Show how long the bot has been running\n"
        "`/help` - Link to Frictionless Platform User Guide\n"
        "`/commands` - List all available commands"
    )
    update.message.reply_text(commands_text, parse_mode='Markdown')

def help_command(update, context):
    help_text = (
        "/help - https://frictionless-2.gitbook.io/http-www.frictionless.help"
    )
    update.message.reply_text(help_text)

dispatcher = Dispatcher(bot, None, workers=1, use_context=True)

dispatcher.add_handler(CommandHandler("uptime", uptime_command))
dispatcher.add_handler(CommandHandler("start", start_command))
dispatcher.add_handler(CommandHandler("status", status_command))
dispatcher.add_handler(CommandHandler("switches", switches_command))
dispatcher.add_handler(CommandHandler("help", help_command))
dispatcher.add_handler(CommandHandler("commands", commands_command))


  


  



  


  


  






