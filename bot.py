import time
import json
from web3 import Web3
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Dispatcher, CommandHandler, CallbackContext
from telegram.utils.request import Request
import os
import logging
import threading
from telegram.error import RetryAfter
import requests
import matplotlib.pyplot as plt
import matplotlib.patheffects as path_effects
from flask import Flask, request
import ssl
import urllib3

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

class Config:
    BLOCK_CHECK_INTERVAL = 60  # seconds
    SCANNER_ERROR_SLEEP = 30  # seconds
    SCANNER_MAX_CONSECUTIVE_ERRORS = 5  # max consecutive errors before extended sleep
    SCANNER_EXTENDED_SLEEP = 300  # 5 minutes extended sleep on repeated failures
    RATE_LIMIT_COOLDOWN = 120  # seconds
    MAX_RETRIES = 3
    PRICE_CACHE_DURATION = 300  # seconds (5 minutes)
    TELEGRAM_TIMEOUT = 15  # seconds (increased from 10)
    TELEGRAM_RETRY_DELAY = 2  # seconds
    SUMMARY_RETRY_SLEEP = 600  # 10 minutes retry for summary (increased from 60)
    SUMMARY_MAX_CONSECUTIVE_ERRORS = 3  # max consecutive summary errors
    SUMMARY_EXTENDED_SLEEP = 1800  # 30 minutes extended sleep for summary failures
    IMAGE_DPI = 200  # Summary Image resolution
    WEB3_RETRY_DELAY = 5  # seconds
    WEB3_MAX_RETRIES = 3  # max retries for web3 calls

# Global variables
w3_lock = threading.Lock()
eth_price_cache = {'price': 0, 'timestamp': 0}

# New optimization globals
TOKEN_CACHE = {}
rpc_calls_today = {'count': 0, 'date': time.strftime('%Y-%m-%d')}
blocks_processed_count = 0
    
# ---------------- CONFIG ---------------- #
CAMPAIGN_ADDRESS = os.getenv('CAMPAIGN_ADDRESS')
CAMPAIGN_TARGET_USD = float(os.getenv('CAMPAIGN_TARGET_USD', '50000'))
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_IDS = [chat_id.strip() for chat_id in os.getenv('TELEGRAM_CHAT_ID', '').split(',') if chat_id.strip()]
ETHEREUM_RPC_URL = os.getenv('ETHEREUM_RPC_URL')

# Campaign summary configuration
ENABLE_CAMPAIGN_SUMMARY = os.getenv('ENABLE_CAMPAIGN_SUMMARY', 'true').lower() in ('true', '1', 'yes', 'on')
SUMMARY_INTERVAL_MINUTES = int(os.getenv('SUMMARY_INTERVAL_MINUTES', '120'))  # Default 2 hours
STATIC_ETH_PRICE = os.getenv('STATIC_ETH_PRICE')  # Optional static price for testing

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

# Create Telegram bot with improved connection handling
try:
    # Create request object with increased timeouts and retries
    telegram_request = Request(
        connect_timeout=30,
        read_timeout=30,
        con_pool_size=8
    )
    
    bot = Bot(token=TELEGRAM_BOT_TOKEN, request=telegram_request)
    
    # Test the bot connection with retry logic for SSL issues
    logger.info("Testing Telegram bot connection...")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            bot_info = bot.get_me()
            logger.info(f"✅ Bot connected successfully: @{bot_info.username}")
            break
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(f"Connection attempt {attempt + 1} failed: {e}")
                time.sleep(2)  # Wait before retry
            else:
                logger.error(f"❌ All connection attempts failed: {e}")
                raise
    
except Exception as e:
    logger.error(f"❌ Failed to initialize Telegram bot with custom request: {e}")
    logger.info("Falling back to basic bot initialization...")
    try:
        # Fallback to basic bot
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        
        # Test fallback connection with retries
        for attempt in range(3):
            try:
                bot_info = bot.get_me()
                logger.info(f"✅ Bot connected successfully with fallback: @{bot_info.username}")
                break
            except Exception as e:
                if attempt < 2:
                    logger.warning(f"Fallback attempt {attempt + 1} failed: {e}")
                    time.sleep(2)
                else:
                    raise
    except Exception as fallback_error:
        logger.error(f"❌ Even fallback bot initialization failed: {fallback_error}")
        raise

last_checked = w3.eth.block_number
transfer_event_sig = w3.keccak(text="Transfer(address,address,uint256)").hex()
start_time = time.time()

# ---------------- IMPROVED WEB3 WRAPPER ---------------- #
def safe_web3_call(func, *args, max_retries=None, **kwargs):
    """Wrapper for Web3 calls with proper error handling, retries, and RPC monitoring"""
    global rpc_calls_today
    
    if max_retries is None:
        max_retries = Config.WEB3_MAX_RETRIES
    
    # Track RPC usage
    current_date = time.strftime('%Y-%m-%d')
    if current_date != rpc_calls_today['date']:
        rpc_calls_today['count'] = 0
        rpc_calls_today['date'] = current_date
        logger.info("🔄 Daily RPC call counter reset")
    
    rpc_calls_today['count'] += 1
    
    # Log usage at intervals
    if rpc_calls_today['count'] % 1000 == 0:
        logger.info(f"📊 RPC calls today: {rpc_calls_today['count']}")
        
    for attempt in range(max_retries):
        try:
            with w3_lock:
                return func(*args, **kwargs)
        except Exception as e:
            error_str = str(e).lower()
            # Enhanced rate limit detection for Infura
            if any(term in error_str for term in [
                '429', 'rate limit', 'too many requests', 'quota exceeded',
                'request limit', 'throttled', 'rate exceeded',
                'daily request count exceeded', 'project id request limit'  # Infura specific
            ]):
                # Exponential backoff for rate limits
                wait_time = min(600, Config.RATE_LIMIT_COOLDOWN * (2 ** attempt))
                logger.warning(f"🚫 Infura rate limit hit, waiting {wait_time}s...")
                time.sleep(wait_time)
            elif any(term in error_str for term in [
                'connection', 'timeout', 'network', 'unreachable'
            ]):
                logger.warning(f"Network error (attempt {attempt + 1}): {e}")
                time.sleep(Config.WEB3_RETRY_DELAY * (attempt + 1))
            elif attempt == max_retries - 1:
                logger.error(f"Web3 call failed after {max_retries} attempts: {e}")
                raise
            else:
                logger.warning(f"Web3 call failed (attempt {attempt + 1}): {e}")
                time.sleep(Config.WEB3_RETRY_DELAY)

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
    """Send notification to all configured Telegram chats with improved error handling"""
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
        # Send animation with improved error handling
        _send_animation_with_retry(chat_id, video_path)
        
        # Send message with improved error handling
        _send_message_with_retry(chat_id, message, reply_markup)

def _send_animation_with_retry(chat_id, video_path):
    """Helper function to send animation with proper error handling"""
    if not os.path.exists(video_path):
        logger.warning(f"Animation file not found: {video_path}")
        return
        
    for attempt in range(Config.MAX_RETRIES):
        try:
            with open(video_path, 'rb') as gif_file:
                bot.send_animation(
                    chat_id=chat_id, 
                    animation=gif_file, 
                    timeout=Config.TELEGRAM_TIMEOUT
                )
            logger.debug(f"Animation sent successfully to {chat_id}")
            return
            
        except RetryAfter as e:
            logger.warning(f"Telegram rate limit (animation), retrying in {e.retry_after}s...")
            time.sleep(e.retry_after)
        except Exception as e:
            error_str = str(e).lower()
            if 'ssl' in error_str or 'decryption failed' in error_str:
                logger.warning(f"SSL error on attempt {attempt+1}, retrying: {e}")
                time.sleep(Config.TELEGRAM_RETRY_DELAY * (attempt + 1))  # Exponential backoff
            else:
                logger.warning(f"Attempt {attempt+1} failed to send animation to {chat_id}: {e}")
                if attempt < Config.MAX_RETRIES - 1:
                    time.sleep(Config.TELEGRAM_RETRY_DELAY)
    
    logger.error(f"❌ Failed to send animation to chat_id {chat_id} after {Config.MAX_RETRIES} attempts")

def _send_message_with_retry(chat_id, message, reply_markup):
    """Helper function to send message with proper error handling"""
    for attempt in range(Config.MAX_RETRIES):
        try:
            bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode='Markdown',
                reply_markup=reply_markup,
                timeout=Config.TELEGRAM_TIMEOUT
            )
            logger.debug(f"Message sent successfully to {chat_id}")
            return
            
        except RetryAfter as e:
            logger.warning(f"Telegram rate limit (message), retrying in {e.retry_after}s...")
            time.sleep(e.retry_after)
        except Exception as e:
            error_str = str(e).lower()
            if 'ssl' in error_str or 'decryption failed' in error_str:
                logger.warning(f"SSL error on attempt {attempt+1}, retrying: {e}")
                time.sleep(Config.TELEGRAM_RETRY_DELAY * (attempt + 1))  # Exponential backoff
            else:
                logger.warning(f"Attempt {attempt+1} failed to send message to {chat_id}: {e}")
                if attempt < Config.MAX_RETRIES - 1:
                    time.sleep(Config.TELEGRAM_RETRY_DELAY)
    
    logger.error(f"❌ Failed to send message to chat_id {chat_id} after {Config.MAX_RETRIES} attempts")

# ---------------- TOKEN INFO CACHING SYSTEM ---------------- #
def get_cached_token_info(contract_address):
    """Get cached token info to avoid repeat RPC calls"""
    global TOKEN_CACHE
    
    if contract_address in TOKEN_CACHE:
        return TOKEN_CACHE[contract_address]
    
    try:
        contract = w3.eth.contract(address=contract_address, abi=ERC20_ABI)
        symbol = safe_web3_call(contract.functions.symbol().call)
        decimals = safe_web3_call(contract.functions.decimals().call)
        
        # Cache the result
        TOKEN_CACHE[contract_address] = {'symbol': symbol, 'decimals': decimals}
        logger.debug(f"Cached token info for {contract_address}: {symbol}")
        return TOKEN_CACHE[contract_address]
    except Exception:
        # Cache the failure too to avoid repeat attempts
        TOKEN_CACHE[contract_address] = {'symbol': 'UNKNOWN', 'decimals': 18}
        logger.debug(f"Cached failed token lookup for {contract_address}")
        return TOKEN_CACHE[contract_address]

def get_cached_token_symbol(contract_address):
    """Get token symbol with aggressive caching"""
    return get_cached_token_info(contract_address)['symbol']

def get_cached_token_decimals(contract_address):
    """Get token decimals with aggressive caching"""
    return get_cached_token_info(contract_address)['decimals']

def process_erc20_transfer(log, tx_hash):
    """Process ERC20 transfer events from transaction logs"""
    try:
        # Use safe_web3_call for contract interactions
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

        # Get token info using safe wrapper
        try:
            token_symbol = safe_web3_call(contract.functions.symbol().call)
        except Exception:
            token_symbol = "UNKNOWN"

        try:
            decimals = safe_web3_call(contract.functions.decimals().call)
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

# ---------------- IMPROVED MAIN LOGIC ---------------- #
def check_blocks():
    """Main function to check new blocks for relevant transactions"""
    global last_checked
    
    try:
        latest = safe_web3_call(lambda: w3.eth.block_number)
    except Exception as e:
        logger.error(f"Failed to get latest block number: {e}")
        return
    
    if latest <= last_checked:
        return
        
    logger.info(f"Checking blocks {last_checked + 1} to {latest}")

    for block_number in range(last_checked + 1, latest + 1):
        try:
            process_block(block_number)
        except Exception as e:
            logger.error(f"Block processing error for block {block_number}: {e}")
            # Continue processing other blocks even if one fails

    last_checked = latest

def process_block(block_number):
    """Process a single block for relevant transactions"""
    try:
        block = safe_web3_call(lambda: w3.eth.get_block(block_number, full_transactions=True))
        
        for tx in block.transactions:
            if not tx['to'] and not tx['from']:
                continue

            to_address = w3.to_checksum_address(tx['to']) if tx['to'] else None
            from_address = w3.to_checksum_address(tx['from']) if tx['from'] else None

            # Skip if transaction doesn't involve tracked wallets
            if (to_address not in WALLETS_TO_TRACK and 
                from_address not in WALLETS_TO_TRACK):
                continue

            process_transaction(tx)
                
    except Exception as e:
        logger.error(f"Error processing block {block_number}: {e}")
        raise

def process_transaction(tx):
    """Process a single transaction for token transfers and ETH transfers"""
    try:
        receipt = safe_web3_call(lambda: w3.eth.get_transaction_receipt(tx.hash))
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
        logger.error(f"Transaction processing error for {tx.hash.hex()}: {e}")

# ---------------- IMPROVED CAMPAIGN SUMMARY ---------------- #
def get_eth_price():
    """Get ETH price with caching and multiple fallbacks"""
    global eth_price_cache
    
    # Use static price if configured
    if STATIC_ETH_PRICE:
        try:
            static_price = float(STATIC_ETH_PRICE)
            logger.info(f"Using static ETH price: ${static_price}")
            return static_price
        except ValueError:
            logger.warning(f"Invalid STATIC_ETH_PRICE value: {STATIC_ETH_PRICE}")
    
    # Return cached price if still valid
    if (time.time() - eth_price_cache['timestamp']) < Config.PRICE_CACHE_DURATION:
        logger.debug(f"Using cached ETH price: ${eth_price_cache['price']}")
        return eth_price_cache['price']
    
    # Try to get fresh price
    price = fetch_eth_price_from_apis()
    
    if price > 0:
        # Update cache
        eth_price_cache = {
            'price': price,
            'timestamp': time.time()
        }
        return price
    
    # Return cached price even if expired, or 0 if no cache
    if eth_price_cache['price'] > 0:
        logger.warning("Using expired cached ETH price")
        return eth_price_cache['price']
    
    logger.error("Could not fetch ETH price from any source")
    return 0

def fetch_eth_price_from_apis():
    """Fetch ETH price from multiple API sources"""
    price_sources = [
        {
            'name': 'CoinGecko',
            'url': 'https://api.coingecko.com/api/v3/simple/price',
            'params': {'ids': 'ethereum', 'vs_currencies': 'usd'},
            'parser': lambda data: data.get('ethereum', {}).get('usd', 0)
        },
        {
            'name': 'CryptoCompare',
            'url': 'https://min-api.cryptocompare.com/data/price',
            'params': {'fsym': 'ETH', 'tsyms': 'USD'},
            'parser': lambda data: data.get('USD', 0)
        },
        {
            'name': 'Binance',
            'url': 'https://api.binance.com/api/v3/ticker/price',
            'params': {'symbol': 'ETHUSDT'},
            'parser': lambda data: float(data.get('price', 0))
        }
    ]
    
    for source in price_sources:
        try:
            response = requests.get(
                source['url'],
                params=source['params'],
                timeout=10,
                headers={'User-Agent': 'Frictionless-Bot/1.0'}
            )
            
            if response.status_code == 200:
                price_data = response.json()
                price = source['parser'](price_data)
                
                if price > 0:
                    logger.info(f"ETH price updated from {source['name']}: ${price}")
                    return price
                    
        except Exception as e:
            logger.warning(f"Failed to get price from {source['name']}: {e}")
    
    return 0

def create_enhanced_progress_chart(bal_eth, current_usd, percent):
    """Create progress chart with optional background image"""
    import numpy as np
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    import matplotlib.image as mpimg
    from matplotlib import patheffects as path_effects
    from PIL import Image, ImageFilter, ImageEnhance
    import os
    
    # Create figure
    fig, ax = plt.subplots(figsize=(12, 4), facecolor='#1a1a1a')
    ax.set_facecolor('#1a1a1a')
    
    # METHOD 1: Use a local background image file
    background_image_path = 'background.jpg'  # Your background image
    
    if os.path.exists(background_image_path):
        try:
            # Load and process background image
            bg_img = Image.open(background_image_path)
            
            # Resize to fit chart dimensions
            chart_width, chart_height = fig.get_size_inches() * fig.dpi
            bg_img = bg_img.resize((int(chart_width), int(chart_height)), Image.Resampling.LANCZOS)
            
            # Apply effects to make text readable
            enhancer = ImageEnhance.Brightness(bg_img)
            bg_img = enhancer.enhance(1.0)  # Darken (0.3 = 30% brightness) changed from .9 to 1.0 to make it brighter
            
            # Convert to array and display
            bg_array = np.array(bg_img)
            ax.imshow(bg_array, extent=[-2, 102, -1, 1.5], aspect='auto', alpha=0.6)
            
        except Exception as e:
            logger.warning(f"Could not load background image: {e}")
    
    # METHOD 2: Create a gradient background programmatically
    else:
        # Create gradient background
        gradient = np.linspace(0, 1, 256).reshape(1, -1)
        gradient = np.vstack((gradient, gradient))
        
        # Define gradient colors based on progress
        if percent >= 75:
            colors = ['#1a0d00', '#331a00', '#4d2600']  # Dark orange gradient
        elif percent >= 50:
            colors = ['#001a1a', '#003333', '#004d4d']  # Dark teal gradient
        elif percent >= 25:
            colors = ['#0d001a', '#1a0033', '#26004d']  # Dark purple gradient
        else:
            colors = ['#000d1a', '#001a33', '#00264d']  # Dark blue gradient
        
        # Create custom colormap for background
        bg_cmap = LinearSegmentedColormap.from_list('bg_gradient', colors, N=256)
        ax.imshow(gradient, extent=[-2, 102, -1, 1.5], aspect='auto', 
                 cmap=bg_cmap, alpha=0.4, zorder=0)
    
    # Define progress bar colors
    if percent >= 75:
        colors = ['#ff6b35', '#f7931e', '#ffcd3c']  # Orange to yellow
    elif percent >= 50:
        colors = ['#4ecdc4', '#44a08d', '#093637']  # Teal gradient
    elif percent >= 25:
        colors = ['#667eea', '#764ba2', '#f093fb']  # Purple gradient
    else:
        colors = ['#2196f3', '#21cbf3', '#2196f3']  # Blue gradient
    
    # Create custom colormap for progress bar
    n_bins = 100
    cmap = LinearSegmentedColormap.from_list('progress', colors, N=n_bins)
    
    # Create the main progress bar - MOVED DOWN to avoid text overlap
    bar_height = 0.6
    bar_y = -0.7  # Moved up from -0.9
    
    # Background bar (unfilled portion) with subtle glow - MORE TRANSPARENT
    ax.barh(bar_y, 100, height=bar_height, color='#333333', alpha=0.2, 
            edgecolor='#555555', linewidth=2, zorder=2)
    
    # Add subtle glow behind the background bar - REDUCED OPACITY
    for i in range(3):
        ax.barh(bar_y, 100, height=bar_height + 0.1 * (3-i), 
               color='#333333', alpha=0.02 * (i+1), 
               edgecolor='none', zorder=1)
    
    # Progress bar with gradient and glow effect - MORE TRANSPARENT
    if percent > 0:
        # Main progress bar - REDUCED OPACITY
        x_vals = np.linspace(0, percent, max(int(percent), 1) + 1)
        for i, x in enumerate(x_vals[:-1]):
            color_intensity = i / len(x_vals) if len(x_vals) > 1 else 0.5
            ax.barh(bar_y, 1, left=x, height=bar_height, 
                   color=cmap(color_intensity), alpha=0.5, zorder=3)  # Reduced from 0.6 to 0.5
        
        # Add glow effect around progress bar - REDUCED OPACITY
        for i in range(4):
            glow_alpha = 0.04 * (4-i) / 4  # Reduced from 0.08
            glow_height = bar_height + 0.04 * (4-i)
            ax.barh(bar_y, percent, height=glow_height, 
                   color=colors[0], alpha=glow_alpha, 
                   edgecolor='none', zorder=1)
    
    # Outlined Text Function
    def add_outlined_text_v2(x, y, text, fontsize, color='white', outline_color='black', outline_width=3):
        """Add text with outline using path effects (more efficient)"""
        text_obj = ax.text(x, y, text, ha='center', va='center', fontsize=fontsize, 
                          color=color, fontweight='bold', zorder=6)
        
        # Add stroke/outline effect
        text_obj.set_path_effects([
            path_effects.Stroke(linewidth=outline_width, foreground=outline_color),
            path_effects.Normal()
        ])
        
        return text_obj

    # For other text elements:
    if percent > 10:
        add_outlined_text_v2(percent/2, bar_y, f'{percent:.1f}%', 16, outline_width=3)
    else:
        add_outlined_text_v2(percent + 8, bar_y, f'{percent:.1f}%', 16, outline_width=3)
    
    # Add value labels with outline - ADJUSTED POSITIONS
    add_outlined_text_v2(10, bar_y - .4, "Raised = "f'${current_usd:,.0f}', 14, 
                        color='#cccccc', outline_color='black', outline_width=4) #moved down from -.4
    add_outlined_text_v2(90, bar_y - .4, "Goal = "f'${CAMPAIGN_TARGET_USD:,.0f}', 14, 
                        color='#cccccc', outline_color='black', outline_width=4) #moved down from -.4
    
    # Removed corners
    
    # Customize the chart
    ax.set_xlim(-2, 102)
    ax.set_ylim(-1, 1.5)
    ax.axis('off')
    
    return fig

def send_campaign_summary():
    """Send periodic fundraising campaign updates with enhanced visuals and proper cleanup"""
    img_path = None
    fig = None
    
    try:
        # Validation
        if not CAMPAIGN_ADDRESS or not w3.is_address(CAMPAIGN_ADDRESS):
            logger.error("Invalid campaign address")
            return

        # Get balance using safe wrapper
        bal_wei = safe_web3_call(lambda: w3.eth.get_balance(CAMPAIGN_ADDRESS))
        bal_eth = w3.from_wei(bal_wei, 'ether')
        
        price_usd = get_eth_price()
        if price_usd == 0:
            logger.warning("Could not fetch ETH price - skipping summary")
            return
            
        current_usd = float(bal_eth) * price_usd
        percent = min(100, (current_usd / CAMPAIGN_TARGET_USD) * 100)

        # Status message logic
        status_emoji, status_text = get_status_emoji_and_text(percent)

        msg = (
            f"{status_emoji} *{status_text}*\n\n"
            f"💰 **Balance:** `{bal_eth:.4f} ETH`\n"
            f"💵 **Value:** `${current_usd:,.2f}` / `${CAMPAIGN_TARGET_USD:,.2f}`\n"
            f"📊 **Progress:** `{percent:.1f}%`"
        )

        keyboard = [[InlineKeyboardButton("💰 Contribute Now", url="https://app.frictionless.network/contribute")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Create chart
        fig = create_enhanced_progress_chart(bal_eth, current_usd, percent)
        
        # Save with high quality
        img_path = '/tmp/progress_enhanced.png'
        fig.savefig(img_path, bbox_inches='tight', dpi=Config.IMAGE_DPI, 
                   facecolor='#0173CC', edgecolor='none', #changed face color from 1a1a1a
                   transparent=True, pad_inches=0) #Reduced padding from .15

        # Send to all Telegram chats
        send_campaign_to_chats(img_path, msg, reply_markup)
                
    except Exception as e:
        logger.error(f"Error in send_campaign_summary: {e}")
    finally:
        # Cleanup resources
        if fig:
            plt.close(fig)
        if img_path and os.path.exists(img_path):
            try:
                os.remove(img_path)
            except Exception as e:
                logger.warning(f"Failed to cleanup image file: {e}")

def get_status_emoji_and_text(percent):
    """Get appropriate emoji and status text based on progress percentage"""
    if percent >= 100:
        return "🎉", "GOAL ACHIEVED!"
    elif percent >= 75:
        return "🔥", "Almost There!"
    elif percent >= 50:
        return "📈", "Halfway Mark!"
    elif percent >= 25:
        return "💪", "Building Momentum"
    else:
        return "🚀", "Getting Started"

def send_campaign_to_chats(img_path, msg, reply_markup):
    """Send campaign update to all configured chats with better error handling"""
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            # Send image
            with open(img_path, 'rb') as img_file:
                bot.send_photo(
                    chat_id=chat_id, 
                    photo=img_file, 
                    timeout=Config.TELEGRAM_TIMEOUT
                )
            
            # Send message
            bot.send_message(
                chat_id=chat_id, 
                text=msg, 
                parse_mode='Markdown', 
                reply_markup=reply_markup, 
                timeout=Config.TELEGRAM_TIMEOUT
            )
            
        except Exception as e:
            logger.error(f"Failed to send campaign update to {chat_id}: {e}")

# ---------------- IMPROVED BACKGROUND THREADS ---------------- #
def run_scanner():
    """Background thread for blockchain scanning with better error handling"""
    logger.info("✅ Scanner thread started")
    consecutive_errors = 0
    
    while True:
        try:
            check_blocks()
            consecutive_errors = 0  # Reset error counter on success
            time.sleep(Config.BLOCK_CHECK_INTERVAL)
            
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"🔥 Scanner loop error ({consecutive_errors}/{Config.SCANNER_MAX_CONSECUTIVE_ERRORS}): {repr(e)}")
            
            if consecutive_errors >= Config.SCANNER_MAX_CONSECUTIVE_ERRORS:
                logger.critical("Too many consecutive scanner errors. Extending sleep time.")
                time.sleep(Config.SCANNER_EXTENDED_SLEEP)
                consecutive_errors = 0  # Reset counter
            else:
                time.sleep(Config.SCANNER_ERROR_SLEEP)

def run_summary():
    """Background thread for periodic fundraising summaries with better error handling"""
    if not ENABLE_CAMPAIGN_SUMMARY:
        logger.info("📊 Campaign summary disabled via ENABLE_CAMPAIGN_SUMMARY")
        return
        
    logger.info(f"✅ Summary thread started - interval: {SUMMARY_INTERVAL_MINUTES} minutes")
    interval_seconds = SUMMARY_INTERVAL_MINUTES * 60
    consecutive_errors = 0
    
    while True:
        try:
            send_campaign_summary()
            consecutive_errors = 0  # Reset error counter on success
            logger.info(f"Next campaign summary in {SUMMARY_INTERVAL_MINUTES} minutes")
            time.sleep(interval_seconds)
            
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"Summary thread error ({consecutive_errors}/{Config.SUMMARY_MAX_CONSECUTIVE_ERRORS}): {e}")
            
            if consecutive_errors >= Config.SUMMARY_MAX_CONSECUTIVE_ERRORS:
                logger.warning("Too many consecutive summary errors. Extending sleep time.")
                time.sleep(Config.SUMMARY_EXTENDED_SLEEP)
                consecutive_errors = 0  # Reset counter
            else:
                time.sleep(Config.SUMMARY_RETRY_SLEEP)

# ---------------- TELEGRAM COMMANDS ---------------- #
def start_command(update: Update, context: CallbackContext):
    """Handle /start command"""
    update.message.reply_text("🚀 Frictionless bot is live and tracking blocks.")

def status_command(update: Update, context: CallbackContext):
    """Handle /status command with performance metrics"""
    try:
        block = safe_web3_call(lambda: w3.eth.block_number)
        connection_status = "✅ Connected" if w3.is_connected() else "❌ Disconnected"
        blocks_behind = block - last_checked
        
        status_text = (
            f"📡 **Bot Status:** {connection_status}\n"
            f"🔗 **Current block:** `{block:,}`\n"
            f"🔍 **Last checked:** `{last_checked:,}`\n"
            f"📊 **Blocks behind:** `{blocks_behind}`\n\n"
            f"📈 **Performance:**\n"
            f"• Daily RPC calls: `{rpc_calls_today['count']:,}`\n"
            f"• Cached tokens: `{len(TOKEN_CACHE)}`\n"
            f"• Blocks processed: `{blocks_processed_count:,}`"
        )
        
        update.message.reply_text(status_text, parse_mode='Markdown')
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

def config_command(update: Update, context: CallbackContext):
    """Handle /config command - show current configuration"""
    try:
        # Use safe_web3_call for getting block number
        current_block = safe_web3_call(lambda: w3.eth.block_number) if w3.is_connected() else 'disconnected'
        
        # Determine price mode text
        if STATIC_ETH_PRICE:
            try:
                static_price = float(STATIC_ETH_PRICE)
                price_mode = f"Static (${static_price})`"
            except ValueError:
                price_mode = f"Invalid static price: {STATIC_ETH_PRICE}`"
        else:
            price_mode = "Dynamic pricing`"
        
        # Safely display chat count without exposing IDs
        chat_count = len(TELEGRAM_CHAT_IDS)
        chat_status = f"{chat_count} configured"
        
        config_text = (
            "*Bot Configuration:*\n\n"
            f"🔗 **Blockchain:**\n"
            f"• Network: Ethereum\n"
            f"• Current Block: `{current_block}`\n"
            f"• Last Checked: `{last_checked}`\n\n"
            f"👥 **Telegram:**\n"
            f"• Chat IDs: `{chat_status}`\n\n"
            f"💰 **Campaign:**\n"
            f"• Address: `{CAMPAIGN_ADDRESS[:10]}...{CAMPAIGN_ADDRESS[-8:] if CAMPAIGN_ADDRESS else 'Not set'}`\n"
            f"• Target: `${CAMPAIGN_TARGET_USD:,.2f}`\n"
            f"• Summary Enabled: `{ENABLE_CAMPAIGN_SUMMARY}`\n"
            f"• Summary Interval: `{SUMMARY_INTERVAL_MINUTES} minutes`\n\n"
            f"🔍 **Tracking:**\n"
            f"• Wallets: `{len(WALLETS_TO_TRACK)} addresses`\n"
            f"• Price Mode: `{price_mode}"
        )
        update.message.reply_text(config_text, parse_mode='Markdown')
    except Exception as e:
        update.message.reply_text(f"❌ Error getting configuration: {str(e)}")

def commands_command(update: Update, context: CallbackContext):
    """Handle /commands command"""
    commands_text = (
        "*Available Commands:*\n\n"
        "`/start` - Show startup confirmation\n"
        "`/status` - Show current block height and connection status\n"
        "`/config` - Show bot configuration\n"
        "`/campaign` - Show current campaign status\n"
        "`/switches` - List all tracked wallet addresses\n"
        "`/uptime` - Show bot uptime\n"
        "`/help` - Link to Frictionless Platform User Guide\n"
        "`/commands` - List all available commands"
    )
    update.message.reply_text(commands_text, parse_mode='Markdown')

def campaign_command(update: Update, context: CallbackContext):
    """Handle /campaign command - show current campaign status with visual progress"""
    try:
        # Check if campaign summary is enabled
        if not ENABLE_CAMPAIGN_SUMMARY:
            update.message.reply_text("No active campaigns currently")
            return
        
        # Validate campaign address
        if not CAMPAIGN_ADDRESS or not w3.is_address(CAMPAIGN_ADDRESS):
            update.message.reply_text("❌ Invalid campaign address configured")
            return

        # Get campaign balance using safe wrapper
        bal_wei = safe_web3_call(lambda: w3.eth.get_balance(CAMPAIGN_ADDRESS))
        bal_eth = w3.from_wei(bal_wei, 'ether')
        
        # Get ETH price
        price_usd = get_eth_price()
        
        if price_usd == 0:
            update.message.reply_text("❌ Could not fetch ETH price for campaign status")
            return
            
        current_usd = float(bal_eth) * price_usd
        percent = min(100, (current_usd / CAMPAIGN_TARGET_USD) * 100)

        # Build visual progress bar
        progress_blocks = "█" * int(percent // 4) + "░" * (25 - int(percent // 4))
        
        # Choose emoji and status text based on progress
        status_emoji, status_text = get_status_emoji_and_text(percent)

        status_msg = (
            f"{status_emoji} *{status_text}*\n\n"
            f"💰 **Balance:** `{bal_eth:.4f} ETH`\n"
            f"💵 **Value:** `${current_usd:,.2f}` / `${CAMPAIGN_TARGET_USD:,.2f}`\n"
            f"📊 **Progress:** `{percent:.1f}%`\n\n"
            f"```\n{progress_blocks}\n```\n"
            f"`{percent:.1f}%` Complete"
        )
        
        # Create proper inline keyboard
        keyboard = [[InlineKeyboardButton("💰 Contribute Now", url="https://app.frictionless.network/contribute")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Use reply_markup parameter
        update.message.reply_text(status_msg, parse_mode='Markdown', reply_markup=reply_markup)
        
    except Exception as e:
        logger.error(f"Error in campaign_command: {e}")
        update.message.reply_text(f"❌ Error fetching campaign status: {str(e)}")

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
    try:
        current_block = safe_web3_call(lambda: w3.eth.block_number) if w3.is_connected() else 'disconnected'
        return {
            'status': 'running',
            'uptime_seconds': int(time.time() - start_time),
            'last_checked_block': last_checked,
            'current_block': current_block
        }
    except Exception as e:
        return {
            'status': 'error',
            'error': str(e),
            'uptime_seconds': int(time.time() - start_time),
            'last_checked_block': last_checked
        }

@app.route('/webhook', methods=['POST'])
def webhook():
    """Telegram webhook endpoint"""
    try:
        if request.method == "POST":
            json_data = request.get_json(force=True)
            if json_data:
                update = Update.de_json(json_data, bot)
                dispatcher.process_update(update)
                return "ok", 200
            else:
                logger.warning("Received webhook with no JSON data")
                return "no data", 400
        return "method not allowed", 405
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "error", 500

# ---------------- INITIALIZATION ---------------- #
# Setup Telegram dispatcher
dispatcher = Dispatcher(bot, None, workers=1, use_context=True)

# Register command handlers
dispatcher.add_handler(CommandHandler("start", start_command))
dispatcher.add_handler(CommandHandler("status", status_command))
dispatcher.add_handler(CommandHandler("config", config_command))
dispatcher.add_handler(CommandHandler("campaign", campaign_command))
dispatcher.add_handler(CommandHandler("switches", switches_command))
dispatcher.add_handler(CommandHandler("uptime", uptime_command))
dispatcher.add_handler(CommandHandler("commands", commands_command))
dispatcher.add_handler(CommandHandler("help", help_command))

# Start background threads
scanner_thread = threading.Thread(target=run_scanner, daemon=True)
scanner_thread.start()

# Only start summary thread if enabled
if ENABLE_CAMPAIGN_SUMMARY:
    summary_thread = threading.Thread(target=run_summary, daemon=True)
    summary_thread.start()
else:
    logger.info("📊 Campaign summary thread disabled")

# Setup webhook
webhook_url = os.environ.get('WEBHOOK_URL')
if webhook_url:
    try:
        # Clear any existing webhook first
        bot.delete_webhook()
        time.sleep(1)
        
        # Set the new webhook
        result = bot.set_webhook(url=f"{webhook_url}/webhook")
        if result:
            logger.info(f"✅ Webhook set successfully to {webhook_url}/webhook")
        else:
            logger.error("❌ Failed to set webhook")
    except Exception as e:
        logger.error(f"❌ Failed to set webhook: {e}")
else:
    logger.warning("⚠️ No WEBHOOK_URL configured")

logger.info("🚀 Frictionless Telegram Bot started successfully")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))