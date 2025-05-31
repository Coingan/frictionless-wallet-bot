import time
import json
from web3 import Web3
from telegram import Bot
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import os

# ---------------- CONFIG ---------------- #
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_IDS = os.getenv('TELEGRAM_CHAT_ID', '').split(',')
ETHEREUM_RPC_URL = os.getenv('ETHEREUM_RPC_URL')

WALLETS_TO_TRACK = {
    '0xd9aD5Acc883D8a67ab612B70C11abF33dD450A45': 'Switch FRIC/ETH',
    '0xda1916b0d6B209A143009214Cac95e771c4aa277': 'Switch FRIC/ETH'
}

GLOBAL_LABEL = "Frictionless Whales POTC"

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
            f"🔔 *New Offer Created on the Frictionless Platform* ({wallet_label}, {GLOBAL_LABEL})\n"
            f"Token: `{token_symbol}`\n"
            f"Amount: `{value:.4f}`\n"
            f"🔗 [View Transaction](https://etherscan.io/tx/{tx_hash})"
        )
    elif tx_type == "outgoing":
        return (
            f"🤝 *Contribution on offer wall* ({wallet_label}, {GLOBAL_LABEL})\n"
            f"Token: `{token_symbol}`\n"
            f"Amount: `{value:.4f}`\n"
            f"🔗 [View Transaction](https://etherscan.io/tx/{tx_hash})"
        )
    return None

def notify(message, tx_type=None):
    video_path = 'Friccy_whale.gif'
    if tx_type == "incoming":
        keyboard = [[InlineKeyboardButton("💰 Contribute Now", url="https://app.frictionless.network/")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
    elif tx_type == "outgoing":
        keyboard = [[InlineKeyboardButton("💰 Create an OTC offer", url="https://app.frictionless.network/")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
    else:
        keyboard = []
        reply_markup = None

    for chat_id in TELEGRAM_CHAT_IDS:
        bot.send_animation(chat_id=chat_id, animation=open(video_path, 'rb'))
        bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown', reply_markup=reply_markup)

# ---------------- MAIN LOGIC ---------------- #
def check_blocks():
    global last_checked
    latest = w3.eth.block_number
    print(f"Checking blocks {last_checked + 1} to {latest}", flush=True)
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
                from_addr = tx['from']
                to_addr = tx['to']
                value = tx['value']
                if from_addr in WALLETS_TO_TRACK or to_addr in WALLETS_TO_TRACK:
                    if value == 0:
                        continue  # Skip zero-value ETH transfers
                    tx_type = "incoming" if to_addr in WALLETS_TO_TRACK else "outgoing"
                    tracked_addr = to_addr if tx_type == "incoming" else from_addr
                    value_eth = w3.from_wei(value, 'ether')
                    message = build_frictionless_message(tx_type, 'ETH', value_eth, tx.hash.hex(), tracked_addr)
                    if message:
                        print(f"Sending message: {message[:100]}...", flush=True)
                        notify(message, tx_type)

                try:
                    receipt = w3.eth.get_transaction_receipt(tx.hash)
                except Exception as e:
                    if '429' in str(e):
                        print("Rate limited by RPC provider. Cooling down for 120 seconds.", flush=True)
                        time.sleep(120)
                        continue
                    else:
                        raise
                for log in receipt.logs:
                    if len(log['topics']) != 3:
                        continue  # Skip non-ERC20 Transfer events
                    if log['topics'][0].hex() == transfer_event_sig:
                        try:
                            contract = w3.eth.contract(address=log['address'], abi=ERC20_ABI)
                            from web3._utils.events import get_event_data
                            transfer_event_abi = [abi for abi in ERC20_ABI if abi.get("type") == "event" and abi.get("name") == "Transfer"][0]
                            decoded_log = get_event_data(w3.codec, transfer_event_abi, log)

                            from_addr = decoded_log['args']['from']
                            to_addr = decoded_log['args']['to']
                            value = decoded_log['args']['value']

                            if to_addr in WALLETS_TO_TRACK:
                                tx_type = "incoming"
                                tracked_addr = to_addr
                            elif from_addr in WALLETS_TO_TRACK:
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
                                print(f"Sending ERC20 message: {message[:100]}...", flush=True)
                                notify(message, tx_type)
                        except Exception as e:
                            print("Decode error:", e)
            except Exception as e:
                print("Receipt error:", e)

    last_checked = latest

# ---------------- RUN LOOP ---------------- #
if __name__ == '__main__':
    while True:
        try:
            check_blocks()
            time.sleep(60)
        except Exception as e:
            print("Main loop error:", e)
            time.sleep(30)
