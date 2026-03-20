import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import os
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# --- 1. 多 Bot 配置区 ---
MONITOR_LIST = [
    {
        "symbol": "XAUUSDT", 
        "name": "黄金 (Gold)", 
        "token": os.getenv('GOLD_TOKEN'),
        "chat_id": os.getenv('CHAT_ID'),
    },
    {
        "symbol": "BTCUSDT", 
        "name": "比特币 (BTC)", 
        "token": os.getenv('BTC_TOKEN'),
        "chat_id": os.getenv('CHAT_ID'),
    }
]

EXCHANGE = ccxt.binance()
BUFFER_PCT = 0.001
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-5.4-mini')
OPENAI_TIMEOUT_SECONDS = 20
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
PORT = int(os.getenv('PORT', '8080'))
WEBHOOK_TOKEN = os.getenv('WEBHOOK_TOKEN')

# --- 2. 发送函数 ---
def send_telegram_msg(text, token, chat_id):
    if not token or not chat_id:
        print(f"错误: 对应的 Token 或 ChatID 未设置 (Token存在: {token is not None})", flush=True)
        return
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # 这里修正了 Bug：直接使用传入的参数
    params = {"chat_id": chat_id, "text": text}
    
    try:
        res = requests.get(url, params=params, timeout=10)
        print(f"发送结果: {res.status_code}", flush=True)
    except Exception as e:
        print(f"发送异常: {e}", flush=True)


def is_check_command(text):
    if not text:
        return False

    first_token = text.strip().split()[0]
    return first_token.split('@')[0].lower() == '/check'

def get_data(symbol):
    # 最后一根日线通常是未收盘 K 线，用它做 4 小时观察，已收盘 K 线用于正式确认。
    bars = EXCHANGE.fetch_ohlcv(symbol, timeframe='1d', limit=180)
    df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    
    # 计算指标
    df['ema60'] = ta.ema(df['close'], length=60)
    df['ma60'] = df['close'].rolling(window=60).mean()
    df['ema_slope_pct'] = df['ema60'].pct_change()
    df['ma_slope_pct'] = df['ma60'].pct_change()
    
    return df.dropna().reset_index(drop=True)


def is_above(price, level, buffer_pct=BUFFER_PCT):
    return price > level * (1 + buffer_pct)


def is_below(price, level, buffer_pct=BUFFER_PCT):
    return price < level * (1 - buffer_pct)


def detect_daily_crosses(previous_row, current_row):
    events = []

    if previous_row['close'] <= previous_row['ema60'] and current_row['close'] > current_row['ema60']:
        events.append('bullish_ema_cross')
    if previous_row['close'] >= previous_row['ema60'] and current_row['close'] < current_row['ema60']:
        events.append('bearish_ema_cross')
    if previous_row['close'] <= previous_row['ma60'] and current_row['close'] > current_row['ma60']:
        events.append('bullish_ma_cross')
    if previous_row['close'] >= previous_row['ma60'] and current_row['close'] < current_row['ma60']:
        events.append('bearish_ma_cross')

    return events


def has_long_platform(left_row, right_row):
    return (
        is_above(left_row['close'], left_row['ema60'])
        and is_above(left_row['close'], left_row['ma60'])
        and is_above(right_row['close'], right_row['ema60'])
        and is_above(right_row['close'], right_row['ma60'])
        and right_row['ema_slope_pct'] > 0
        and right_row['ma_slope_pct'] >= 0
    )


def has_short_platform(left_row, right_row):
    return (
        is_below(left_row['close'], left_row['ema60'])
        and is_below(left_row['close'], left_row['ma60'])
        and is_below(right_row['close'], right_row['ema60'])
        and is_below(right_row['close'], right_row['ma60'])
        and right_row['ema_slope_pct'] < 0
        and right_row['ma_slope_pct'] <= 0
    )


def format_bar_time(timestamp_ms):
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d")


def build_analysis_payload(config, now, live_row, last_closed, prev_closed, prev2_closed):
    event_ids = detect_daily_crosses(prev_closed, last_closed)
    long_platform = bool(has_long_platform(prev_closed, last_closed))
    short_platform = bool(has_short_platform(prev_closed, last_closed))
    prev_long_platform = bool(has_long_platform(prev2_closed, prev_closed))
    prev_short_platform = bool(has_short_platform(prev2_closed, prev_closed))
    day_change_pct = (live_row['close'] - last_closed['close']) / last_closed['close']
    bias_to_ema_pct = (live_row['close'] - last_closed['ema60']) / last_closed['ema60']

    if long_platform and not prev_long_platform:
        event_ids.append('long_platform_confirmed')
    elif prev_long_platform and not long_platform and last_closed['close'] < last_closed['ema60']:
        event_ids.append('long_platform_broken')

    if short_platform and not prev_short_platform:
        event_ids.append('short_platform_confirmed')
    elif prev_short_platform and not short_platform and last_closed['close'] > last_closed['ema60']:
        event_ids.append('short_platform_broken')

    def row_snapshot(label, row):
        return {
            "label": label,
            "date": format_bar_time(row['timestamp']),
            "open": round(float(row['open']), 4),
            "high": round(float(row['high']), 4),
            "low": round(float(row['low']), 4),
            "close": round(float(row['close']), 4),
            "volume": round(float(row['volume']), 4),
            "ema60": round(float(row['ema60']), 4),
            "ma60": round(float(row['ma60']), 4),
            "ema_slope_pct": round(float(row['ema_slope_pct']) * 100, 4),
            "ma_slope_pct": round(float(row['ma_slope_pct']) * 100, 4),
        }

    return {
        "analysis_time": now,
        "instrument": {
            "symbol": config['symbol'],
            "name": config['name'],
        },
        "price_snapshot": {
            "current_price": round(float(live_row['close']), 4),
            "last_closed_price": round(float(last_closed['close']), 4),
            "day_change_pct": round(float(day_change_pct) * 100, 4),
            "bias_to_ema60_pct": round(float(bias_to_ema_pct) * 100, 4),
        },
        "closed_bar_context": [
            row_snapshot("prev2_closed", prev2_closed),
            row_snapshot("prev_closed", prev_closed),
            row_snapshot("last_closed", last_closed),
        ],
        "live_bar_context": row_snapshot("live_bar", live_row),
        "derived_signals": {
            "event_ids": event_ids,
            "long_platform_confirmed": long_platform,
            "short_platform_confirmed": short_platform,
            "last_closed_above_ema60": bool(is_above(last_closed['close'], last_closed['ema60'])),
            "last_closed_above_ma60": bool(is_above(last_closed['close'], last_closed['ma60'])),
            "last_closed_below_ema60": bool(is_below(last_closed['close'], last_closed['ema60'])),
            "last_closed_below_ma60": bool(is_below(last_closed['close'], last_closed['ma60'])),
            "live_price_above_ema60_ref": bool(is_above(live_row['close'], last_closed['ema60'])),
            "live_price_above_ma60_ref": bool(is_above(live_row['close'], last_closed['ma60'])),
            "live_price_below_ema60_ref": bool(is_below(live_row['close'], last_closed['ema60'])),
            "live_price_below_ma60_ref": bool(is_below(live_row['close'], last_closed['ma60'])),
            "ema60_slope_direction": "up" if last_closed['ema_slope_pct'] > 0 else ("down" if last_closed['ema_slope_pct'] < 0 else "flat"),
            "ma60_slope_direction": "up" if last_closed['ma_slope_pct'] > 0 else ("down" if last_closed['ma_slope_pct'] < 0 else "flat"),
        }
    }


def get_openai_response_format():
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "market_analysis",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "time": {"type": "string"},
                    "symbol": {"type": "string"},
                    "current_price": {"type": "number"},
                    "trend_status": {"type": "string"},
                    "signal_recognition": {"type": "string"},
                    "entry_strategy": {"type": "string"},
                    "risk_control": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["高", "中", "低"]},
                    "summary": {"type": "string"}
                },
                "required": [
                    "time",
                    "symbol",
                    "current_price",
                    "trend_status",
                    "signal_recognition",
                    "entry_strategy",
                    "risk_control",
                    "confidence",
                    "summary"
                ],
                "additionalProperties": False
            }
        }
    }


def request_llm_analysis(payload):
    if not OPENAI_API_KEY:
        raise RuntimeError("未设置 OPENAI_API_KEY")

    system_prompt = (
        "你是一名只基于输入数据做推断的市场结构分析器。"
        "你必须只使用用户提供的数值、事件和派生信号，不允许补充外部信息。"
        "输出必须是 JSON，并严格符合给定字段。"
        "禁止使用主观、情绪化或无数据支撑的词，例如：感觉、猜测、应该会、大概率、也许、看起来很强。"
        "允许表达确认条件，例如：'若下一根日线收盘仍站上 MA60，则确认站稳'。"
        "趋势状态只能描述当前数据所示状态，不允许使用未来判断。"
        "信号识别必须在以下类别中择一并说明依据：趋势形成、回踩回抽、刚好破位、等待确认。"
        "入场策略必须给出'立即'或'等待确认'，并写明触发条件。"
        "风险控制必须引用客观价位或结构失效条件，例如跌回 EMA60 下方、重新回到双均线夹层。"
        "置信度必须按数据确认程度给出：高=至少有收盘确认且结构一致，中=部分确认但仍需条件，低=信号处于夹层或仅有盘中突破。"
        "总结必须是一句话，说明当前是否适合做多/做空/观望，以及对应的数据原因。"
    )

    user_prompt = (
        "请基于以下 JSON 数据输出结构化分析，不要输出任何 JSON 之外的内容：\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )

    body = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": get_openai_response_format(),
        "temperature": 0,
    }

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    res = requests.post(
        OPENAI_API_URL,
        headers=headers,
        json=body,
        timeout=OPENAI_TIMEOUT_SECONDS,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"OpenAI 请求失败: {res.status_code} {res.text}")

    data = res.json()
    try:
        content = data['choices'][0]['message']['content']
        parsed = json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"OpenAI 返回内容无法解析: {exc}")

    required_fields = [
        "time",
        "symbol",
        "current_price",
        "trend_status",
        "signal_recognition",
        "entry_strategy",
        "risk_control",
        "confidence",
        "summary",
    ]
    missing_fields = [field for field in required_fields if field not in parsed]
    if missing_fields:
        raise RuntimeError(f"OpenAI 返回缺少字段: {', '.join(missing_fields)}")

    parsed['time'] = payload['analysis_time']
    parsed['symbol'] = payload['instrument']['symbol']
    parsed['current_price'] = payload['price_snapshot']['current_price']

    return parsed


def render_llm_message(analysis):
    return (
        f"[时间] {analysis['time']}\n"
        f"[标的] {analysis['symbol']}\n"
        f"[当前现价] {float(analysis['current_price']):.2f}\n"
        f"[趋势状态] {analysis['trend_status']}\n"
        f"[信号识别] {analysis['signal_recognition']}\n"
        f"[入场策略] {analysis['entry_strategy']}\n"
        f"[风险控制] {analysis['risk_control']}\n"
        f"[置信度] {analysis['confidence']}\n"
        f"[总结] {analysis['summary']}"
    )


def get_configs_for_symbol(symbol=None):
    if not symbol or symbol.lower() == 'all':
        return MONITOR_LIST

    normalized_symbol = symbol.upper()
    return [config for config in MONITOR_LIST if config['symbol'].upper() == normalized_symbol]


def get_config_by_token(token):
    for config in MONITOR_LIST:
        if config.get('token') == token:
            return config
    return None


def check_request_authorized(handler, query_params):
    if not WEBHOOK_TOKEN:
        return True

    header_token = handler.headers.get("X-Webhook-Token", "")
    query_token = query_params.get("token", [""])[0]
    return header_token == WEBHOOK_TOKEN or query_token == WEBHOOK_TOKEN


def handle_telegram_webhook(bot_config, update):
    message = update.get('message') or update.get('edited_message') or {}
    text = message.get('text', '').strip()
    chat_id = str(message.get('chat', {}).get('id', ''))

    if not text:
        return {"ok": True, "ignored": True, "reason": "empty_message"}

    if str(bot_config.get('chat_id')) != chat_id:
        return {"ok": False, "ignored": True, "reason": "chat_not_allowed"}

    if not is_check_command(text):
        return {"ok": True, "ignored": True, "reason": "not_check_command"}

    parts = text.split()
    requested_symbol = parts[1] if len(parts) > 1 else bot_config['symbol']

    send_telegram_msg("已收到 /check，正在执行检查。", bot_config['token'], bot_config['chat_id'])
    results = run_checks(requested_symbol)
    if not results:
        send_telegram_msg(
            f"未找到可检查的标的: {requested_symbol}",
            bot_config['token'],
            bot_config['chat_id'],
        )
        return {"ok": False, "ignored": False, "error": f"未找到标的: {requested_symbol}"}

    return {
        "ok": all(result.get("ok") for result in results),
        "ignored": False,
        "requested_symbol": requested_symbol,
        "results": results,
    }


def run_logic_for_symbol(config):
    symbol = config['symbol']
    name = config['name']
    token = config['token']
    chat_id = config['chat_id']
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        df = get_data(symbol)
    except Exception as e:
        print(f"获取 {symbol} 数据失败: {e}", flush=True)
        return {
            "symbol": symbol,
            "name": name,
            "ok": False,
            "error": f"获取数据失败: {e}",
        }

    if len(df) < 4:
        print(f"{symbol} 数据不足，无法完成日线确认与日内观察", flush=True)
        return {
            "symbol": symbol,
            "name": name,
            "ok": False,
            "error": "数据不足，无法完成分析",
        }

    live_row = df.iloc[-1]
    last_closed = df.iloc[-2]
    prev_closed = df.iloc[-3]
    prev2_closed = df.iloc[-4]

    payload = build_analysis_payload(config, now, live_row, last_closed, prev_closed, prev2_closed)

    try:
        llm_analysis = request_llm_analysis(payload)
        full_msg = render_llm_message(llm_analysis)
        mode = "llm"
    except Exception as e:
        print(f"{symbol} LLM 分析失败: {e}", flush=True)
        full_msg = (
            f"🕒 时间(Local): {now}\n"
            f"🔍 标的: {name}\n"
            f"💰 当前现价: {live_row['close']:.2f}\n"
            f"⚠️ LLM 分析不可用，未发送规则化回退内容。\n"
            f"原因: {e}"
        )
        mode = "fallback"

    send_telegram_msg(full_msg, token, chat_id)
    return {
        "symbol": symbol,
        "name": name,
        "ok": True,
        "mode": mode,
        "current_price": round(float(live_row['close']), 4),
        "analysis_time": now,
    }

def run_checks(symbol=None):
    configs = get_configs_for_symbol(symbol)
    if not configs:
        return []

    return [run_logic_for_symbol(config) for config in configs]


class BotHTTPRequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse_json_body(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}

        raw_body = self.rfile.read(content_length)
        if not raw_body:
            return {}

        try:
            return json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            raise ValueError("请求体不是合法 JSON")

    def _handle_check(self, query_params, body_params):
        if not check_request_authorized(self, query_params):
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        symbol = body_params.get("symbol") or query_params.get("symbol", ["all"])[0]
        results = run_checks(symbol)
        if not results:
            self._send_json(404, {"ok": False, "error": f"未找到标的: {symbol}"})
            return

        all_ok = all(result.get("ok") for result in results)
        self._send_json(
            200 if all_ok else 500,
            {
                "ok": all_ok,
                "triggered_symbol": symbol,
                "results": results,
            },
        )

    def _handle_telegram_webhook(self, parsed):
        token = parsed.path.rsplit("/", 1)[-1]
        bot_config = get_config_by_token(token)
        if not bot_config:
            self._send_json(404, {"ok": False, "error": "unknown_bot"})
            return

        try:
            update = self._parse_json_body()
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        result = handle_telegram_webhook(bot_config, update)
        self._send_json(200 if result.get("ok", True) else 400, result)

    def do_GET(self):
        parsed = urlparse(self.path)
        query_params = parse_qs(parsed.query)

        if parsed.path == "/health":
            self._send_json(200, {"ok": True, "service": "xauusdt-ema60-bot"})
            return

        if parsed.path == "/check":
            self._handle_check(query_params, {})
            return

        self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        query_params = parse_qs(parsed.query)

        if parsed.path.startswith("/telegram-webhook/"):
            self._handle_telegram_webhook(parsed)
            return

        if parsed.path != "/check":
            self._send_json(404, {"ok": False, "error": "not_found"})
            return

        try:
            body_params = self._parse_json_body()
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        self._handle_check(query_params, body_params)

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}", flush=True)

if __name__ == "__main__":
    print(f"🤖 Web service listening on 0.0.0.0:{PORT}", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), BotHTTPRequestHandler)
    server.serve_forever()