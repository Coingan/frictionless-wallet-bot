import time
import json
from web3 import Web3
from telegram import Bot
from web3._utils.events import get_event_data
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ---------------- CONFIG ---------------- #
TELEGRAM_BOT_TOKEN = '7675655366:AAGuj0XOv4uFMF3GPWxY9NZrdkX0PY9Er3Y'
TELEGRAM_CHAT_ID = '1836773368'
ETHEREUM_RPC_URL = 'https://mainnet.infura.io/v3/bc0f03db32124262bf703df3bf68db85'

WALLETS_TO_TRACK = {
    '0xd9aD5Acc883D8a67ab612B70C11abF33dD450A45': 'Frictionless Switch FRIC/ETH',
    '0xda1916b0d6B209A143009214Cac95e771c4aa277': 'Frictionless Switch FRIC/ETH'
}

GLOBAL_LABEL = "Frictionless Whales"

ERC20_ABI = json.loads('[{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"from","type":"address"},{"indexed":true,"internalType":"address","name":"to","type":"address"},{"indexed":false,"internalType":"uint256","name":"value","type":"uint256"}],"name":"Transfer","type":"event"}]')

# ---------------- SETUP ---------------- #
w3 = Web3(Web3.HTTPProvider(ETHEREUM_RPC_URL))
bot = Bot(token=TELEGRAM_BOT_TOKEN)

transfer_event_sig = w3.keccak(text="Transfer(address,address,uint256)").hex()

# ---------------- UTILS ---------------- #
def build_frictionless_message(tx_type, token_symbol, value, tx_hash, address):
    wallet_label = WALLETS_TO_TRACK.get(address, None)
    if not wallet_label:
        return None  # Skip unknown addresses entirely
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
    else:
        return f"🔄 {value:.4f} {token_symbol} transfer detected."

def notify(message, tx_type=None):
    video_path = 'Friccy Whale.gif'
    if tx_type == "incoming":
        keyboard = [[InlineKeyboardButton("💰 Contribute Now", url="https://app.frictionless.network/")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        bot.send_animation(chat_id=TELEGRAM_CHAT_ID, animation=open(video_path, 'rb'))
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown', reply_markup=reply_markup)
    elif tx_type == "outgoing":
        keyboard = [[InlineKeyboardButton("💰 Create an OTC offer", url="https://app.frictionless.network/")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        bot.send_animation(chat_id=TELEGRAM_CHAT_ID, animation=open(video_path, 'rb'))
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown', reply_markup=reply_markup)
    else:
        bot.send_animation(chat_id=TELEGRAM_CHAT_ID, animation=open(video_path, 'rb'))
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown')

# ---------------- MAIN LOGIC ---------------- #
def check_blocks():
    latest = w3.eth.block_number
    print(f"Checking block {latest}")
    block = w3.eth.get_block(latest, full_transactions=True)

    for tx in block.transactions:
        try:
            # Native ETH Transfers
            from_addr = tx['from']
            to_addr = tx['to']
            value = tx['value']
            if value > 0 and (from_addr in WALLETS_TO_TRACK or to_addr in WALLETS_TO_TRACK):
                tx_type = "incoming" if to_addr in WALLETS_TO_TRACK else "outgoing"
                tracked_addr = to_addr if tx_type == "incoming" else from_addr
                value_eth = w3.from_wei(value, 'ether')
                message = build_frictionless_message(tx_type, 'ETH', value_eth, tx.hash.hex(), tracked_addr)
                if message:
                    notify(message, tx_type)

            # ERC20 Transfers
            receipt = w3.eth.get_transaction_receipt(tx.hash)
            for log in receipt.logs:
                if log['topics'][0].hex() == transfer_event_sig:
                    try:
                        decoded = get_event_data(w3.codec, ERC20_ABI[0], log)
                        from_addr = decoded['args']['from']
                        to_addr = decoded['args']['to']
                        value = decoded['args']['value']
                        contract = w3.eth.contract(address=log['address'], abi=ERC20_ABI)
                        token_symbol = contract.functions.symbol().call()

                        if to_addr in WALLETS_TO_TRACK:
                            tx_type = "incoming"
                            tracked_addr = to_addr
                        elif from_addr in WALLETS_TO_TRACK:
                            tx_type = "outgoing"
                            tracked_addr = from_addr
                        else:
                            continue

                        value_human = value / (10 ** contract.functions.decimals().call())
                        message = build_frictionless_message(tx_type, token_symbol, value_human, tx.hash.hex(), tracked_addr)
                        if message:
                            notify(message, tx_type)

                    except Exception as e:
                        print("Decode error:", e)
        except Exception as e:
            print("Receipt error:", e)

# ---------------- RUN LOOP ---------------- #
if __name__ == '__main__':
    while True:
        try:
            check_blocks()
            time.sleep(15)
        except Exception as e:
            print("Main loop error:", e)
            time.sleep(30)
