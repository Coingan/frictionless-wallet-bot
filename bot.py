import time
import json
from web3 import Web3
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Dispatcher, CommandHandler, CallbackContext
import os
import logging
import threading
import requests
import matplotlib.pyplot as plt
from flask import Flask, request

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ---------------- CONFIG ---------------- #
CAMPAIGN_ADDRESS = os.getenv('CAMPAIGN_ADDRESS')
CAMPAIGN_TARGET_USD = float(os.getenv('CAMPAIGN_TARGET_USD', '50000'))
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_IDS = [chat_id.strip() for chat_id in os.getenv('TELEGRAM_CHAT_ID', '').split(',') if chat_id.strip()]
ETHEREUM_RPC_URL = os.getenv('ETHEREUM_RPC_URL')

WALLETS_TO_TRACK = {
    '0xd9aD5Acc883D8a67ab612B70C11abF33dD450A45': 'FRIC/ETH',
    '0xda1916b0d6B209A143009214Cac95e771c4aa277': 'FRIC/ETH'
}

GLOBAL_LABEL = "Frictionless Whales"
EXCLUDED_TO_ADDRESS = "0x4ca9798a36b287f6675429884fab36563f82552d"

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
app = Flask(__name__)

# Validate required environment variables
required_vars = ['TELEGRAM_BOT_TOKEN', 'ETHEREUM_RPC_URL', 'CAMPAIGN_ADDRESS']
missing_vars = [var for var in required_vars if not os.getenv(var)]
if missing_vars:
    raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

if not TELEGRAM_CHAT_IDS:
    raise ValueError("No valid Telegram chat IDs provided")

w3 = Web3(Web3.HTTPProvider(ETHEREUM_RPC_URL))
if not w3.is_connected():
    raise ConnectionError("Failed to connect to Ethereum RPC")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
last_checked = w3.eth.block_number
transfer_event_sig = w3.keccak(text="Transfer(address,address,uint256)").hex()
start_time = time.time()

# ---------------- UTILS ---------------- #
def build_frictionless_message(tx_type, token_symbol, value, tx_hash, address):
    """Build formatted message for Frictionless platform notifications"""
    wallet_label = WALLETS_TO_TRACK.get(address)
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
    """Send notification to all configured Telegram chats"""
    video_path = 'Friccy_whale.gif'
    
    # Create appropriate keyboard based on transaction type
    if tx_type == "incoming":
        keyboard = [[InlineKeyboardButton("💰 Contribute Now", url="https://app.frictionless.network/contribute")]]
    elif tx_type == "outgoing":
        keyboard = [[InlineKeyboardButton("💰 Create an OTC offer", url="https://app.frictionless.network/create")]]
    else:
        keyboard = []
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    for chat_id in TELEGRAM_CHAT_IDS:
        # Send animation with retries
        for attempt in range(3):
            try:
                if os.path.exists(video_path):
                    with open(video_path, 'rb') as gif_file:
                        bot.send_animation(chat_id=chat_id, animation=gif_file, timeout=10)
                break
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} failed to send animation to {chat_id}: {e}")
                if attempt < 2:
                    time.sleep(2)
        
        # Send message with retries
        for attempt in range(3):
            try:
                bot.send_message(
                    chat_id=chat_id, 
                    text=message, 
                    parse_mode='Markdown', 
                    reply_markup=reply_markup, 
                    timeout=10
                )
                break
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} failed to send message to {chat_id}: {e}")
                if attempt < 2:
                    time.sleep(2)
        else:
            logger.error(f"❌ All retries failed for message to chat_id {chat_id}")

def process_erc20_transfer(log, tx_hash):
    """Process ERC20 transfer events from transaction logs"""
    try:
        contract = w3.eth.contract(address=log['address'], abi=ERC20_ABI)
        from web3._utils.events import get_event_data
        
        transfer_event_abi = next(
            abi for abi in ERC20_ABI 
            if abi.get("type") == "event" and abi.get("name") == "Transfer"
        )
        decoded_log = get_event_data(w3.codec, transfer_event_abi, log)

        from_addr = w3.to_checksum_address(decoded_log['args']['from'])
        to_addr = w3.to_checksum_address(decoded_log['args']['to'])
        value = decoded_log['args']['value']

        # Determine transaction type and tracked address
        if to_addr in WALLETS_TO_TRACK:
            tx_type = "incoming"
            tracked_addr = to_addr
        elif from_addr in WALLETS_TO_TRACK:
            if to_addr.lower() == EXCLUDED_TO_ADDRESS.lower():
                return False  # Skip excluded address
            tx_type = "outgoing"
            tracked_addr = from_addr
        else:
            return False

        # Get token info
        try:
            token_symbol = contract.functions.symbol().call()
        except Exception:
            token_symbol = "UNKNOWN"

        try:
            decimals = contract.functions.decimals().call()
        except Exception:
            decimals = 18

        value_human = value / (10 ** decimals)
        if value_human == 0:
            return False

        message = build_frictionless_message(tx_type, token_symbol, value_human, tx_hash, tracked_addr)
        if message:
            logger.info(f"Sending ERC20 message: {message[:100]}...")
            notify(message, tx_type)
            return True
            
    except Exception as e:
        logger.warning(f"ERC20 decode error: {e}")
    
    return False

def process_eth_transfer(tx):
    """Process native ETH transfers"""
    from_addr = w3.to_checksum_address(tx['from']) if tx['from'] else None
    to_addr = w3.to_checksum_address(tx['to']) if tx['to'] else None
    value = tx['value']

    if not from_addr or not to_addr or value == 0:
        return False

    # Check if transaction involves tracked wallets
    if to_addr in WALLETS_TO_TRACK:
        tx_type = "incoming"
        tracked_addr = to_addr
    elif from_addr in WALLETS_TO_TRACK:
        if to_addr.lower() == EXCLUDED_TO_ADDRESS.lower():
            return False  # Skip excluded address
        tx_type = "outgoing"
        tracked_addr = from_addr
    else:
        return False

    value_eth = w3.from_wei(value, 'ether')
    message = build_frictionless_message(tx_type, 'ETH', value_eth, tx['hash'].hex(), tracked_addr)
    if message:
        logger.info(f"Sending ETH message: {message[:100]}...")
        notify(message, tx_type)
        return True
    
    return False

# ---------------- MAIN LOGIC ---------------- #
def check_blocks():
    """Main function to check new blocks for relevant transactions"""
    global last_checked
    latest = w3.eth.block_number
    
    if latest <= last_checked:
        return
        
    logger.info(f"Checking blocks {last_checked + 1} to {latest}")

    for block_number in range(last_checked + 1, latest + 1):
        try:
            block = w3.eth.get_block(block_number, full_transactions=True)
            
            for tx in block.transactions:
                if not tx['to'] and not tx['from']:
                    continue

                to_address = w3.to_checksum_address(tx['to']) if tx['to'] else None
                from_address = w3.to_checksum_address(tx['from']) if tx['from'] else None

                # Skip if transaction doesn't involve tracked wallets
                if (to_address not in WALLETS_TO_TRACK and 
                    from_address not in WALLETS_TO_TRACK):
                    continue

                try:
                    receipt = w3.eth.get_transaction_receipt(tx.hash)
                    found_token_transfer = False

                    # Check for ERC20 transfers in transaction logs
                    for log in receipt.logs:
                        if (len(log['topics']) == 3 and 
                            log['topics'][0].hex() == transfer_event_sig):
                            if process_erc20_transfer(log, tx.hash.hex()):
                                found_token_transfer = True

                    # If no ERC20 transfers found, check for ETH transfer
                    if not found_token_transfer:
                        process_eth_transfer(tx)

                except Exception as e:
                    if '429' in str(e):
                        logger.warning("Rate limited by RPC provider. Cooling down for 120 seconds.")
                        time.sleep(120)
                    else:
                        logger.error(f"Transaction processing error: {e}")

        except Exception as e:
            logger.error(f"Block processing error for block {block_number}: {e}")

    last_checked = latest

# ---------------- CAMPAIGN SUMMARY ---------------- #
def send_campaign_summary():
    """Send periodic fundraising campaign updates"""
    try:
        # Validate campaign address
        if not CAMPAIGN_ADDRESS or not w3.is_address(CAMPAIGN_ADDRESS):
            logger.error("Invalid campaign address")
            return

        bal_wei = w3.eth.get_balance(CAMPAIGN_ADDRESS)
        bal_eth = w3.from_wei(bal_wei, 'ether')
        
        # Get ETH price
        response = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ethereum", "vs_currencies": "usd"}, 
            timeout=10
        )
        response.raise_for_status()
        
        price_data = response.json()
        price_usd = price_data.get('ethereum', {}).get('usd', 0)
        
        if price_usd == 0:
            logger.warning("Could not fetch ETH price")
            return
            
        current_usd = float(bal_eth) * price_usd
        percent = min(100, (current_usd / CAMPAIGN_TARGET_USD) * 100)

        # Build summary message
        msg = (
            "*Fundraising Update*\n\n"
            f"Balance: `{bal_eth:.4f} ETH`\n"
            f"USD Value: `${current_usd:,.2f}` of `${CAMPAIGN_TARGET_USD:,.2f}`\n"
            f"Progress: `{percent:.1f}%`"
        )

        # Generate progress bar chart
        fig, ax = plt.subplots(figsize=(6, 1))
        ax.barh(0, percent, color='green', height=0.5)
        ax.barh(0, 100 - percent, left=percent, color='lightgray', height=0.5)
        ax.set_xlim(0, 100)
        ax.set_ylim(-0.5, 0.5)
        ax.axis('off')
        
        img_path = '/tmp/progress.png'
        fig.savefig(img_path, bbox_inches='tight', dpi=100)
        plt.close(fig)

        # Send to all Telegram chats
        for chat_id in TELEGRAM_CHAT_IDS:
            try:
                with open(img_path, 'rb') as img_file:
                    bot.send_photo(chat_id=chat_id, photo=img_file, timeout=10)
                bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown', timeout=10)
            except Exception as e:
                logger.error(f"Failed to send campaign update to {chat_id}: {e}")
                
        # Clean up temp file
        try:
            os.remove(img_path)
        except Exception:
            pass
            
    except Exception as e:
        logger.error(f"Error in send_campaign_summary: {e}")

# ---------------- BACKGROUND THREADS ---------------- #
def run_scanner():
    """Background thread for blockchain scanning"""
    logger.info("✅ Scanner thread started")
    while True:
        try:
            check_blocks()
            time.sleep(60)  # Check every minute
        except Exception as e:
            logger.error(f"🔥 Scanner loop error: {repr(e)}")
            time.sleep(30)  # Wait before retrying

def run_summary():
    """Background thread for periodic fundraising summaries"""
    logger.info("✅ Summary thread started")
    while True:
        try:
            send_campaign_summary()
            time.sleep(1740)  # 29 minutes between summaries
        except Exception as e:
            logger.error(f"Summary thread error: {e}")
            time.sleep(300)  # Wait 5 minutes before retrying

# ---------------- TELEGRAM COMMANDS ---------------- #
def start_command(update: Update, context: CallbackContext):
    """Handle /start command"""
    update.message.reply_text("🚀 Frictionless bot is live and tracking blocks.")

def status_command(update: Update, context: CallbackContext):
    """Handle /status command"""
    try:
        block = w3.eth.block_number
        connection_status = "✅ Connected" if w3.is_connected() else "❌ Disconnected"
        update.message.reply_text(
            f"📡 Bot Status: {connection_status}\n"
            f"Current block: {block:,}\n"
            f"Last checked: {last_checked:,}"
        )
    except Exception as e:
        update.message.reply_text(f"❌ Error checking status: {str(e)}")

def switches_command(update: Update, context: CallbackContext):
    """Handle /switches command"""
    switches = '\n'.join([
        f"{label}: `{addr}`" 
        for addr, label in WALLETS_TO_TRACK.items()
    ])
    update.message.reply_text(
        f"🔀 *Tracked Switches:*\n{switches}", 
        parse_mode='Markdown'
    )

def uptime_command(update: Update, context: CallbackContext):
    """Handle /uptime command"""
    uptime_seconds = int(time.time() - start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    update.message.reply_text(f"⏱ Bot uptime: {hours}h {minutes}m {seconds}s")

def commands_command(update: Update, context: CallbackContext):
    """Handle /commands command"""
    commands_text = (
        "*Available Commands:*\n\n"
        "`/start` - Show startup confirmation\n"
        "`/status` - Show current block height and connection status\n"
        "`/switches` - List all tracked wallet addresses\n"
        "`/uptime` - Show bot uptime\n"
        "`/help` - Link to Frictionless Platform User Guide\n"
        "`/commands` - List all available commands"
    )
    update.message.reply_text(commands_text, parse_mode='Markdown')

def help_command(update: Update, context: CallbackContext):
    """Handle /help command"""
    help_text = (
        "📚 *Frictionless Platform Help*\n\n"
        "🔗 [User Guide](https://frictionless-2.gitbook.io/http-www.frictionless.help)\n"
        "💬 For support, contact the development team."
    )
    update.message.reply_text(help_text, parse_mode='Markdown')

# ---------------- FLASK ROUTES ---------------- #
@app.route('/', methods=['GET'])
def home():
    """Health check endpoint"""
    return {
        'status': 'running',
        'uptime_seconds': int(time.time() - start_time),
        'last_checked_block': last_checked,
        'current_block': w3.eth.block_number if w3.is_connected() else 'disconnected'
    }

@app.route('/webhook', methods=['POST'])
def webhook():
    """Telegram webhook endpoint"""
    try:
        if request.method == "POST":
            update = Update.de_json(request.get_json(force=True), bot)
            dispatcher.process_update(update)
        return "ok"
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "error", 500

# ---------------- INITIALIZATION ---------------- #
# Setup Telegram dispatcher
dispatcher = Dispatcher(bot, None, workers=1, use_context=True)

# Register command handlers
dispatcher.add_handler(CommandHandler("start", start_command))
dispatcher.add_handler(CommandHandler("status", status_command))
dispatcher.add_handler(CommandHandler("switches", switches_command))
dispatcher.add_handler(CommandHandler("uptime", uptime_command))
dispatcher.add_handler(CommandHandler("commands", commands_command))
dispatcher.add_handler(CommandHandler("help", help_command))

# Start background threads
scanner_thread = threading.Thread(target=run_scanner, daemon=True)
scanner_thread.start()

summary_thread = threading.Thread(target=run_summary, daemon=True)
summary_thread.start()

# Setup webhook
webhook_url = os.environ.get('WEBHOOK_URL')
if webhook_url:
    try:
        bot.set_webhook(url=f"{webhook_url}/webhook")
        logger.info(f"Webhook set to {webhook_url}/webhook")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")

logger.info("🚀 Frictionless Telegram Bot started successfully")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))