import logging
import os
import re
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger("bot")

TWELVE_API_KEY = (os.getenv("TWELVE_API_KEY") or "").strip()

DEFAULT_INTERVAL = (os.getenv("DEFAULT_INTERVAL") or "5min").strip()
DEFAULT_SYMBOLS = (os.getenv("DEFAULT_SYMBOLS") or "XAUUSD").strip()  # ex: "XAUUSD,EURUSD,BTCUSD"
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS") or "60")  # 60s
AUTO_TFS = (os.getenv("AUTO_TFS") or "1min,5min,15min").strip()

VI_LENGTH = int(os.getenv("VI_LENGTH") or "14")
ATR_LENGTH = int(os.getenv("ATR_LENGTH") or "14")
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT") or "1.5")
ATR_TP1_MULT = float(os.getenv("ATR_TP1_MULT") or "1.0")
ATR_TP2_MULT = float(os.getenv("ATR_TP2_MULT") or "2.0")
ATR_TP3_MULT = float(os.getenv("ATR_TP3_MULT") or "3.0")

# Anti-spam: guarda último sinal por (chat,symbol,tf)
LAST_SIGNAL: Dict[Tuple[int, str, str], str] = {}
AUTO_ENABLED: Dict[int, bool] = {}          # chat_id -> on/off
AUTO_TFS_BY_CHAT: Dict[int, List[str]] = {} # chat_id -> tfs list
AUTO_SYMBOLS_BY_CHAT: Dict[int, List[str]] = {} # chat_id -> symbols list


@dataclass
class Candle:
    t: str
    o: float
    h: float
    l: float
    c: float


def _normalize_symbol(raw: str) -> str:
    s = raw.strip().upper().replace("#", "").replace("$", "")
    if "/" in s:
        return s
    if re.match(r"^[A-Z]{6}$", s):
        return f"{s[:3]}/{s[3:]}"
    return s


def _extract_symbol_and_interval(text: str) -> Tuple[Optional[str], Optional[str]]:
    if not text:
        return None, None
    parts = text.strip().split()
    if not parts:
        return None, None
    first = parts[0].strip()
    if first.startswith("/"):
        return None, None

    sym = _normalize_symbol(first)
    if not re.match(r"^[A-Z0-9._-]{3,15}(\/[A-Z0-9._-]{3,15})?$", sym):
        return None, None

    interval = None
    if len(parts) >= 2:
        raw = parts[1].strip().upper()
        m_map = {"M1": "1min", "M5": "5min", "M15": "15min", "M30": "30min", "H1": "1h", "H4": "4h", "D1": "1day"}
        interval = m_map.get(raw, parts[1].strip().lower())

    return sym, interval


async def fetch_candles_twelve(symbol: str, interval: str, outputsize: int = 220) -> List[Candle]:
    if not TWELVE_API_KEY:
        raise RuntimeError("TWELVE_API_KEY não configurada no Railway.")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": str(outputsize),
        "apikey": TWELVE_API_KEY,
        "format": "JSON",
    }
    timeout = httpx.Timeout(12.0, connect=6.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()

    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(f"TwelveData error: {data.get('message', 'unknown error')}")

    values = data.get("values") if isinstance(data, dict) else None
    if not values:
        raise RuntimeError("TwelveData não retornou candles (values vazio).")

    candles: List[Candle] = []
    for row in reversed(values):
        candles.append(Candle(
            t=row["datetime"],
            o=float(row["open"]),
            h=float(row["high"]),
            l=float(row["low"]),
            c=float(row["close"]),
        ))
    return candles


def _true_range(curr: Candle, prev_close: float) -> float:
    return max(curr.h - curr.l, abs(curr.h - prev_close), abs(curr.l - prev_close))


def atr(candles: List[Candle], length: int) -> float:
    if len(candles) < length + 1:
        raise RuntimeError("Poucos candles para ATR.")
    trs = []
    for i in range(1, len(candles)):
        trs.append(_true_range(candles[i], candles[i - 1].c))
    window = trs[-length:]
    return sum(window) / len(window)


def vortex(candles: List[Candle], length: int) -> Tuple[float, float]:
    if len(candles) < length + 1:
        raise RuntimeError("Poucos candles para Vortex.")
    vm_plus, vm_minus, tr = [], [], []
    for i in range(1, len(candles)):
        c = candles[i]
        p = candles[i - 1]
        vm_plus.append(abs(c.h - p.l))
        vm_minus.append(abs(c.l - p.h))
        tr.append(_true_range(c, p.c))

    vm_plus_w = vm_plus[-length:]
    vm_minus_w = vm_minus[-length:]
    tr_w = tr[-length:]
    sum_tr = sum(tr_w) if sum(tr_w) != 0 else 1e-9
    return (sum(vm_plus_w) / sum_tr, sum(vm_minus_w) / sum_tr)


def build_trade_plan(last_price: float, direction: str, atr_val: float) -> Dict[str, float]:
    entry = last_price
    if direction == "BUY":
        sl = entry - atr_val * ATR_SL_MULT
        tp1 = entry + atr_val * ATR_TP1_MULT
        tp2 = entry + atr_val * ATR_TP2_MULT
        tp3 = entry + atr_val * ATR_TP3_MULT
    else:
        sl = entry + atr_val * ATR_SL_MULT
        tp1 = entry - atr_val * ATR_TP1_MULT
        tp2 = entry - atr_val * ATR_TP2_MULT
        tp3 = entry - atr_val * ATR_TP3_MULT
    return {"entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3}


def fmt_price(x: float) -> str:
    return f"{x:.2f}" if x >= 100 else f"{x:.5f}"


def decide_signal(vi_p: float, vi_m: float, min_strength: float = 0.12) -> str:
    """
    Retorna BUY/SELL/WAIT.
    min_strength evita setups fracos.
    """
    strength = abs(vi_p - vi_m)
    if strength < min_strength:
        return "WAIT"
    return "BUY" if vi_p > vi_m else "SELL"


def format_alert(symbol: str, interval: str, vi_p: float, vi_m: float, atr_val: float, plan: Dict[str, float], signal: str) -> str:
    strength = abs(vi_p - vi_m)
    return (
        f"🚨 *ALERTA VORTEX*\n"
        f"• Ativo: *{symbol}*\n"
        f"• TF: *{interval}*\n\n"
        f"✅ *Sinal:* *{signal}*\n"
        f"🎯 Entrada: `{fmt_price(plan['entry'])}`\n"
        f"🛡 Stop: `{fmt_price(plan['sl'])}`\n"
        f"🏁 TP1: `{fmt_price(plan['tp1'])}` | TP2: `{fmt_price(plan['tp2'])}` | TP3: `{fmt_price(plan['tp3'])}`\n\n"
        f"📌 VI+ `{vi_p:.3f}` vs VI- `{vi_m:.3f}` | Força `{strength:.3f}` | ATR `{atr_val:.3f}`"
    )


async def analyze_once(symbol: str, interval: str) -> Tuple[str, Dict[str, float], float, float, float]:
    candles = await fetch_candles_twelve(symbol, interval, outputsize=220)
    vi_p, vi_m = vortex(candles, VI_LENGTH)
    atr_val = atr(candles, ATR_LENGTH)
    last_price = candles[-1].c
    signal = decide_signal(vi_p, vi_m, min_strength=0.12)
    direction = "BUY" if vi_p > vi_m else "SELL"
    plan = build_trade_plan(last_price, direction, atr_val)
    return signal, plan, vi_p, vi_m, atr_val


# ---------------- Commands ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    AUTO_ENABLED.setdefault(chat_id, False)
    AUTO_TFS_BY_CHAT.setdefault(chat_id, [t.strip() for t in AUTO_TFS.split(",") if t.strip()])
    AUTO_SYMBOLS_BY_CHAT.setdefault(chat_id, [s.strip() for s in DEFAULT_SYMBOLS.split(",") if s.strip()])

    await update.message.reply_text(
        "👋 *Xauron Vortex*\n\n"
        "• Digite um ativo: `XAUUSD` ou `XAU/USD`\n"
        "• Auto-scan:\n"
        "  - `/autoscan on`\n"
        "  - `/autoscan off`\n"
        "  - `/settf 1min,5min,15min,1h`\n"
        "  - `/setsymbols XAUUSD,EURUSD,BTCUSD`\n",
        parse_mode=ParseMode.MARKDOWN,
    )


async def autoscan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        await update.message.reply_text("Use: `/autoscan on` ou `/autoscan off`", parse_mode=ParseMode.MARKDOWN)
        return
    AUTO_ENABLED[chat_id] = (arg == "on")
    await update.message.reply_text(f"Auto-scan: *{arg.upper()}*", parse_mode=ParseMode.MARKDOWN)


async def settf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    raw = " ".join(context.args).strip()
    if not raw:
        await update.message.reply_text("Use: `/settf 1min,5min,15min,1h`", parse_mode=ParseMode.MARKDOWN)
        return
    tfs = [t.strip() for t in raw.split(",") if t.strip()]
    AUTO_TFS_BY_CHAT[chat_id] = tfs
    await update.message.reply_text(f"Timeframes do auto-scan: `{', '.join(tfs)}`", parse_mode=ParseMode.MARKDOWN)


async def setsymbols(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    raw = " ".join(context.args).strip()
    if not raw:
        await update.message.reply_text("Use: `/setsymbols XAUUSD,EURUSD,BTCUSD`", parse_mode=ParseMode.MARKDOWN)
        return
    syms = [s.strip() for s in raw.split(",") if s.strip()]
    AUTO_SYMBOLS_BY_CHAT[chat_id] = syms
    await update.message.reply_text(f"Ativos do auto-scan: `{', '.join(syms)}`", parse_mode=ParseMode.MARKDOWN)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "✅ Comandos:\n"
        "• `XAUUSD` (ou `XAU/USD`) → análise na hora\n"
        "• `/autoscan on` → começa a te avisar sozinho\n"
        "• `/autoscan off` → para\n"
        "• `/settf 1min,5min,15min,1h` → timeframes\n"
        "• `/setsymbols XAUUSD,EURUSD,BTCUSD` → ativos\n",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    symbol, interval = _extract_symbol_and_interval(text)
    if not symbol:
        await update.message.reply_text("Manda só o ativo (ex: `XAUUSD`).", parse_mode=ParseMode.MARKDOWN)
        return

    interval = interval or DEFAULT_INTERVAL

    try:
        await update.message.reply_text("⏳ Pegando candles + calculando Vortex…", parse_mode=ParseMode.MARKDOWN)
        signal, plan, vi_p, vi_m, atr_val = await analyze_once(symbol, interval)

        if signal == "WAIT":
            await update.message.reply_text(
                f"⏳ Sem setup forte agora em *{symbol}* no *{interval}* (WAIT).",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        msg = format_alert(symbol, interval, vi_p, vi_m, atr_val, plan, signal)
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        log.exception("Erro: %s", e)
        await update.message.reply_text(f"Erro: `{str(e)}`", parse_mode=ParseMode.MARKDOWN)


# ---------------- Background Job ----------------

async def autoscan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    for chat_id, enabled in list(AUTO_ENABLED.items()):
        if not enabled:
            continue

        tfs = AUTO_TFS_BY_CHAT.get(chat_id, [t.strip() for t in AUTO_TFS.split(",") if t.strip()])
        syms = AUTO_SYMBOLS_BY_CHAT.get(chat_id, [s.strip() for s in DEFAULT_SYMBOLS.split(",") if s.strip()])

        for raw_sym in syms:
            symbol = _normalize_symbol(raw_sym)
            for tf in tfs:
                try:
                    signal, plan, vi_p, vi_m, atr_val = await analyze_once(symbol, tf)

                    key = (chat_id, symbol, tf)
                    prev = LAST_SIGNAL.get(key)

                    # Só manda quando muda para BUY/SELL e diferente do anterior
                    if signal in ("BUY", "SELL") and signal != prev:
                        LAST_SIGNAL[key] = signal
                        msg = format_alert(symbol, tf, vi_p, vi_m, atr_val, plan, signal)
                        await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

                    # Se virou WAIT, atualiza estado mas não spamma (opcional)
                    if signal == "WAIT":
                        LAST_SIGNAL[key] = "WAIT"

                except Exception as e:
                    # não travar o loop inteiro por um símbolo
                    log.warning("Autoscan erro %s %s: %s", raw_sym, tf, e)


def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("autoscan", autoscan))
    app.add_handler(CommandHandler("settf", settf))
    app.add_handler(CommandHandler("setsymbols", setsymbols))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # roda a cada SCAN_INTERVAL_SECONDS
    app.job_queue.run_repeating(autoscan_job, interval=SCAN_INTERVAL_SECONDS, first=10)

    return app
