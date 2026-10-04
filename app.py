
# ============================================================================
#  UPSTOX AI OPTION BOT  (Windows GUI)  --  NIFTY / BANKNIFTY / SENSEX
#  Paste Access Token -> Connect -> Start.  Paper mode by default.
#  Strategy (based on the Options Market-Making flow):
#    Live data -> IV / fair value (Black-Scholes) -> mispricing + momentum
#    -> Buy underpriced CE/PE -> book profit at target / cut at stop loss.
#
#  IMPORTANT: No bot can guarantee profit. Test in PAPER mode first.
# ============================================================================

import os, sys, json, time, math, threading, queue, traceback, webbrowser
from urllib.parse import quote, urlparse, parse_qs
from datetime import datetime, date
from collections import deque


import tkinter as tk
from tkinter import ttk, messagebox

try:
    import requests
except ImportError:
    print("requests not installed. Run:  pip install requests")
    sys.exit(1)

BASE = "https://api.upstox.com/v2"
BASE_HFT = "https://api-hft.upstox.com/v3"   # order placement host

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_settings_upstox.json")

INDEXES = {
    "NIFTY":     {"key": "NSE_INDEX|Nifty 50",   "order_seg": "NSE_FO", "lot": 75, "step": 50},
    "BANKNIFTY": {"key": "NSE_INDEX|Nifty Bank", "order_seg": "NSE_FO", "lot": 30, "step": 100},
    "SENSEX":    {"key": "BSE_INDEX|SENSEX",     "order_seg": "BSE_FO", "lot": 20, "step": 100},
}

RISK_FREE = 0.065  # 6.5% like the infographic

# ----------------------------- Black-Scholes --------------------------------
def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def bs_price(S, K, T, r, sigma, is_call=True):
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)

# ----------------------------- Upstox REST client ---------------------------
class UpstoxAPI:
    def __init__(self):
        self.token = ""
        self.session = requests.Session()
        self.ok = False
        self.lot_sizes = {}   # filled from Upstox option contracts (real lot sizes)

    def _set_token(self, token):
        self.token = token.strip()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    @staticmethod
    def _json(r):
        try:
            return r.json()
        except Exception:
            return {}

    @staticmethod
    def _friendly_error(code, data):
        code_txt, msg = "", ""
        try:
            errs = data.get("errors") or []
            if errs:
                code_txt = str(errs[0].get("errorCode", ""))
                msg = str(errs[0].get("message", ""))
            if not msg:
                msg = str(data)[:200]
        except Exception:
            msg = str(data)[:200]
        if code == 401 or code_txt == "UDAPI100050":
            return ("TOKEN INVALID / EXPIRED. Upstox Access Tokens expire every day "
                    "(around 3:30 AM). Click GET TOKEN (or generate one in your Upstox "
                    f"developer app) and paste a fresh token. ({code_txt} {msg})")
        if code == 429:
            return "RATE LIMIT hit. Increase Scan Interval (seconds) and retry. " + msg
        if code >= 500:
            return "Upstox server error. Wait a minute and retry. " + msg
        return f"Upstox error {code} {code_txt}: {msg}"

    def connect(self, token):
        self._set_token(token)
        r = self.session.get(f"{BASE}/user/profile", timeout=15)
        d = self._json(r)
        if r.status_code == 200 and d.get("status") == "success":
            self.ok = True
            info = d.get("data") or {}
            bal = "?"
            try:  # balance is best-effort (funds service has a nightly maintenance window)
                fr = self.session.get(f"{BASE}/user/get-funds-and-margin",
                                      params={"segment": "SEC"}, timeout=15)
                fd = self._json(fr)
                if fr.status_code == 200 and fd.get("status") == "success":
                    bal = fd["data"]["equity"]["available_margin"]
            except Exception:
                pass
            return True, {"availableBalance": bal, "user_name": info.get("user_name", "")}
        self.ok = False
        return False, self._friendly_error(r.status_code, d)

    @staticmethod
    def _norm(s):
        return str(s).replace(":", "|").replace(" ", "").lower()

    def ltp_indices(self):
        keys = ",".join(i["key"] for i in INDEXES.values())
        r = self.session.get(f"{BASE}/market-quote/ltp", params={"instrument_key": keys}, timeout=15)
        d = self._json(r)
        if r.status_code != 200 or d.get("status") != "success":
            raise RuntimeError(self._friendly_error(r.status_code, d))
        by_key = {}
        for k, cell in (d.get("data") or {}).items():
            tok = cell.get("instrument_token") or k
            by_key[self._norm(tok)] = cell.get("last_price")
            by_key.setdefault(self._norm(k), cell.get("last_price"))
        out = {}
        for name, info in INDEXES.items():
            lp = by_key.get(self._norm(info["key"]))
            if lp is not None:
                out[name] = float(lp)
        if not out:
            raise RuntimeError(f"Upstox returned no index prices: {str(d)[:200]}")
        return out

    def expiry_list(self, name):
        info = INDEXES[name]
        r = self.session.get(f"{BASE}/option/contract",
                             params={"instrument_key": info["key"]}, timeout=30)
        d = self._json(r)
        if r.status_code != 200 or d.get("status") != "success":
            raise RuntimeError(self._friendly_error(r.status_code, d))
        today = date.today().isoformat()
        dates = set()
        for c in d.get("data") or []:
            e = c.get("expiry")
            if isinstance(e, str) and e >= today:
                dates.add(e)
            try:
                if c.get("lot_size"):
                    self.lot_sizes[name] = int(c["lot_size"])
            except Exception:
                pass
        return sorted(dates)

    def option_chain(self, name, expiry):
        info = INDEXES[name]
        r = self.session.get(f"{BASE}/option/chain",
                             params={"instrument_key": info["key"], "expiry_date": expiry},
                             timeout=20)
        d = self._json(r)
        if r.status_code != 200 or d.get("status") != "success":
            raise RuntimeError(self._friendly_error(r.status_code, d))
        rows, spot = {}, 0.0
        for item in d.get("data") or []:
            try:
                k = float(item.get("strike_price"))
            except Exception:
                continue
            if not spot:
                spot = float(item.get("underlying_spot_price") or 0)
            rows[k] = {}
            for side, key in (("ce", "call_options"), ("pe", "put_options")):
                leg = item.get(key) or {}
                md = leg.get("market_data") or {}
                gk = leg.get("option_greeks") or {}
                rows[k][side] = {
                    "sec_id": str(leg.get("instrument_key", "")),   # e.g. NSE_FO|51059
                    "ltp": md.get("ltp"),
                    "iv": gk.get("iv"),
                }
        return spot, rows

    def place_order(self, instrument_token, exchange_segment, txn, qty):
        body = {
            "quantity": int(qty),
            "product": "I",            # intraday
            "validity": "DAY",
            "price": 0,
            "tag": "aibot",
            "instrument_token": instrument_token,
            "order_type": "MARKET",
            "transaction_type": txn,   # BUY / SELL
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
            "slice": False,
        }
        r = self.session.post(f"{BASE_HFT}/order/place", json=body, timeout=15)
        d = self._json(r)
        if r.status_code in (200, 201) and d.get("status") == "success":
            data = d.get("data") or {}
            oid = data.get("order_id") or (data.get("order_ids") or [""])[0]
            return True, {"orderId": oid}
        msg = self._friendly_error(r.status_code, d)
        if r.status_code == 401:
            msg += (" | If prices/balance work but ONLY orders fail, your Upstox app may not be "
                    "allowed to place orders yet (order permission / static-IP rules) - check "
                    "your app settings in the Upstox developer console.")
        return False, msg

# ----------------------------- Strategy engine ------------------------------
class Strategy:
    def __init__(self):
        self.history = {n: deque(maxlen=120) for n in INDEXES}  # (ts, spot)

    def momentum_signal(self, name, spot):
        h = self.history[name]
        h.append((time.time(), spot))
        if len(h) < 6:
            return "NEUTRAL", 0.0
        prices = [p for _, p in h]
        fast = sum(prices[-3:]) / 3.0
        slow = sum(prices[-10:]) / min(10, len(prices))
        drift = (prices[-1] - prices[0]) / max(1, len(prices) - 1)
        if fast > slow and drift >= 0:
            return "BULLISH", drift
        if fast < slow and drift <= 0:
            return "BEARISH", drift
        return "NEUTRAL", drift

    @staticmethod
    def analyse_chain(spot, rows, expiry_str, step):
        """Return ATM strike info + fair value mispricing per the infographic."""
        if not rows or spot <= 0:
            return None
        atm = min(rows.keys(), key=lambda k: abs(k - spot))
        row = rows[atm]
        try:
            exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        except Exception:
            exp = date.today()
        T = max((exp - date.today()).days, 0) / 365.0
        if T <= 0:
            T = 1 / 365.0
        ce = row.get("ce") or {}
        pe = row.get("pe") or {}
        iv = ce.get("iv") or pe.get("iv") or 13.5
        try:
            iv = float(iv)
        except Exception:
            iv = 13.5
        if iv > 3: iv /= 100.0
        fv_ce = bs_price(spot, atm, T, RISK_FREE, iv, True)
        fv_pe = bs_price(spot, atm, T, RISK_FREE, iv, False)
        def leg(legd, fv):
            ltp = legd.get("ltp")
            try: ltp = float(ltp)
            except Exception: ltp = None
            diff = (ltp - fv) if ltp is not None else None
            return {"ltp": ltp, "fv": fv, "diff": diff,
                    "signal": ("Underpriced (Buy)" if (diff is not None and diff < 0)
                               else "Overpriced (Sell)" if diff is not None else "-")}
        return {"atm": atm, "iv": iv * 100, "ce": leg(ce, fv_ce), "pe": leg(pe, fv_pe)}

# ----------------------------- The App --------------------------------------
class BotApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Upstox AI Option Bot - NIFTY / BANKNIFTY / SENSEX")
        self.geometry("1420x780")
        self.minsize(1180, 680)
        self.configure(bg="#0e1526")

        self.api = UpstoxAPI()
        self.strat = Strategy()
        self.log_q = queue.Queue()
        self.ui_q = queue.Queue()
        self.running = False
        self.worker = None
        self.position = None  # dict
        self.state = {n: {} for n in INDEXES}  # live snapshot per index
        self.expiries = {}
        self.settings = self.load_settings()

        self._build_style()
        self._build_ui()
        self.after(100, self._drain_queues)

    # ---------- settings ----------
    def load_settings(self):
        s = {"remember": False, "token": "", "lots": 1, "interval": 5,
             "target": 1000, "stoploss": 500, "trail_on": True, "trail_start": 300, "trail_gap": 150,
             "exit_on_reversal": True, "min_hold_sec": 60,
             "lot_sizes": {k: v["lot"] for k, v in INDEXES.items()}}
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                s.update(json.load(f))
        except Exception:
            pass
        return s

    def save_settings(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
        except Exception:
            pass

    def _build_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        bg, fg, acc = "#0e1526", "#e8eefc", "#1f6feb"
        st.configure(".", background=bg, foreground=fg, fieldbackground="#16213b",
                     font=("Segoe UI", 10))
        st.configure("TNotebook", background=bg)
        st.configure("TNotebook.Tab", padding=(14, 6), font=("Segoe UI", 10, "bold"))
        st.configure("TButton", padding=(12, 6), font=("Segoe UI", 10, "bold"))
        st.configure("Go.TButton", foreground="white", background="#1a7f37")
        st.configure("Stop.TButton", foreground="white", background="#b62324")
        st.configure("Danger.TButton", foreground="white", background="#6e2c2c")
        st.configure("TLabel", background=bg, foreground=fg)
        st.configure("Card.TLabelframe", background="#131c33", foreground="#9fd0ff")
        st.configure("Card.TLabelframe.Label", background="#131c33", foreground="#9fd0ff",
                     font=("Segoe UI", 10, "bold"))
        st.configure("Big.TLabel", font=("Segoe UI", 20, "bold"))
        st.configure("Pnl.TLabel", font=("Segoe UI", 22, "bold"))

    # ---------- UI ----------
    def _build_ui(self):
        # ---- top login bar ----
        top = tk.Frame(self, bg="#0b1220")
        top.pack(fill="x", padx=10, pady=(10, 4))
        tk.Label(top, text="UPSTOX", bg="#0b1220", fg="#7cc4ff",
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(8, 2))
        tk.Label(top, text="Access Token", bg="#0b1220", fg="#9fb3d9").pack(side="left", padx=(12, 2))
        self.ent_token = tk.Entry(top, width=38, show="*", font=("Consolas", 9))
        if self.settings.get("remember"):
            self.ent_token.insert(0, self.settings.get("token", ""))
        self.ent_token.pack(side="left", padx=4, fill="x", expand=True)
        self.remember_var = tk.BooleanVar(value=self.settings.get("remember", False))
        tk.Checkbutton(top, text="Remember on this PC", variable=self.remember_var,
                       bg="#0b1220", fg="#9fb3d9", selectcolor="#16213b",
                       activebackground="#0b1220").pack(side="left", padx=6)
        self.btn_connect = ttk.Button(top, text="CONNECT", command=self.on_connect)
        self.btn_connect.pack(side="left", padx=6)
        ttk.Button(top, text="GET TOKEN", command=self.on_get_token).pack(side="left", padx=2)
        self.lbl_status = tk.Label(top, text="Not connected", bg="#0b1220", fg="#ff7b72",
                                   font=("Segoe UI", 10, "bold"))
        self.lbl_status.pack(side="left", padx=8)

        # ---- control bar ----
        ctrl = ttk.LabelFrame(self, text=" TRADE CONTROLS ", style="Card.TLabelframe")
        ctrl.pack(fill="x", padx=10, pady=6)

        def lab(t): return tk.Label(ctrl, text=t, bg="#131c33", fg="#9fb3d9")
        lab("Index").grid(row=0, column=0, padx=(10, 2), pady=8, sticky="e")
        self.cmb_index = ttk.Combobox(ctrl, values=list(INDEXES), width=10, state="readonly")
        self.cmb_index.current(0)
        self.cmb_index.grid(row=0, column=1, padx=4)
        lab("Lots").grid(row=0, column=2, padx=(12, 2))
        self.spn_lots = tk.Spinbox(ctrl, from_=1, to=20, width=4, font=("Segoe UI", 10))
        self.spn_lots.delete(0, "end"); self.spn_lots.insert(0, str(self.settings.get("lots", 1)))
        self.spn_lots.grid(row=0, column=3, padx=4)
        lab("Lot size").grid(row=0, column=4, padx=(12, 2))
        self.spn_lotsize = tk.Spinbox(ctrl, from_=1, to=1000, width=6, font=("Segoe UI", 10))
        self.spn_lotsize.delete(0, "end")
        self.spn_lotsize.insert(0, str(self.settings["lot_sizes"].get("NIFTY", 75)))
        self.spn_lotsize.grid(row=0, column=5, padx=4)
        lab("Target profit (INR)").grid(row=0, column=6, padx=(12, 2))
        self.ent_target = tk.Entry(ctrl, width=7, font=("Segoe UI", 10))
        self.ent_target.insert(0, str(self.settings.get("target", 1000)))
        self.ent_target.grid(row=0, column=7, padx=4)
        lab("Max loss (INR)").grid(row=0, column=8, padx=(12, 2))
        self.ent_stop = tk.Entry(ctrl, width=7, font=("Segoe UI", 10))
        self.ent_stop.insert(0, str(self.settings.get("stoploss", 500)))
        self.ent_stop.grid(row=0, column=9, padx=4)
        lab("Scan (sec)").grid(row=0, column=10, padx=(12, 2))
        self.ent_int = tk.Entry(ctrl, width=4, font=("Segoe UI", 10))
        self.ent_int.insert(0, str(self.settings.get("interval", 5)))
        self.ent_int.grid(row=0, column=11, padx=4)

        # ---- trailing stop row ----
        self.trail_var = tk.BooleanVar(value=bool(self.settings.get("trail_on", True)))
        tk.Checkbutton(ctrl, text="TRAILING STOP", variable=self.trail_var,
                       bg="#131c33", fg="#7cc4ff", selectcolor="#16213b",
                       activebackground="#131c33", font=("Segoe UI", 10, "bold")
                       ).grid(row=1, column=0, columnspan=2, padx=(10, 4), pady=(0, 8), sticky="w")
        lab("Trail starts at profit (INR)").grid(row=1, column=2, columnspan=3, padx=(12, 2), pady=(0, 8), sticky="e")
        self.ent_trail_start = tk.Entry(ctrl, width=7, font=("Segoe UI", 10))
        self.ent_trail_start.insert(0, str(self.settings.get("trail_start", 300)))
        self.ent_trail_start.grid(row=1, column=5, padx=4, pady=(0, 8))
        lab("Trail gap from peak (INR)").grid(row=1, column=6, columnspan=2, padx=(12, 2), pady=(0, 8), sticky="e")
        self.ent_trail_gap = tk.Entry(ctrl, width=7, font=("Segoe UI", 10))
        self.ent_trail_gap.insert(0, str(self.settings.get("trail_gap", 150)))
        self.ent_trail_gap.grid(row=1, column=8, padx=4, pady=(0, 8))
        tk.Label(ctrl, text="(Target profit always closes the trade; trailing protects profit below it)",
                 bg="#131c33", fg="#6e7fa3", font=("Segoe UI", 8)
                 ).grid(row=1, column=9, columnspan=6, padx=6, pady=(0, 8), sticky="w")

        self.stop_after_var = tk.BooleanVar(value=bool(self.settings.get("stop_after_profit", False)))
        tk.Checkbutton(ctrl, text="Stop bot after a profit is booked", variable=self.stop_after_var,
                       bg="#131c33", fg="#9fb3d9", selectcolor="#16213b",
                       activebackground="#131c33").grid(row=2, column=0, columnspan=6, padx=(10, 4),
                                                        pady=(0, 8), sticky="w")

        self.reversal_var = tk.BooleanVar(value=bool(self.settings.get("exit_on_reversal", True)))
        tk.Checkbutton(ctrl, text="Exit on signal reversal", variable=self.reversal_var,
                       bg="#131c33", fg="#9fb3d9", selectcolor="#16213b",
                       activebackground="#131c33").grid(row=2, column=6, columnspan=4, padx=(12, 4),
                                                        pady=(0, 8), sticky="w")
        tk.Label(ctrl, text="min hold before reversal exit (sec)", bg="#131c33", fg="#9fb3d9"
                 ).grid(row=2, column=10, padx=(12, 2), pady=(0, 8), sticky="e")
        self.ent_min_hold = tk.Entry(ctrl, width=5, font=("Segoe UI", 10))
        self.ent_min_hold.insert(0, str(self.settings.get("min_hold_sec", 60)))
        self.ent_min_hold.grid(row=2, column=11, padx=4, pady=(0, 8))

        # ---- actions row: LIVE toggle + START/STOP/EXIT, on their own row so they
        #      always stay on-screen regardless of window width ----
        actions = tk.Frame(ctrl, bg="#131c33")
        actions.grid(row=3, column=0, columnspan=17, sticky="we", padx=10, pady=(4, 10))

        self.live_var = tk.BooleanVar(value=False)
        self.chk_live = tk.Checkbutton(actions, text="LIVE REAL MONEY", variable=self.live_var,
                                       bg="#131c33", fg="#ffb02e", selectcolor="#16213b",
                                       activebackground="#131c33", font=("Segoe UI", 10, "bold"),
                                       command=self.on_live_toggle)
        self.chk_live.pack(side="left", padx=(0, 6))
        self.mode_lbl = tk.Label(actions, text="PAPER (safe)", bg="#131c33", fg="#3fb950",
                                 font=("Segoe UI", 11, "bold"))
        self.mode_lbl.pack(side="left", padx=(0, 20))

        self.btn_start = ttk.Button(actions, text="START", style="Go.TButton",
                                    command=self.on_start, state="disabled")
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(actions, text="STOP", style="Stop.TButton",
                                   command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.btn_exit = ttk.Button(actions, text="EXIT POSITION", style="Danger.TButton",
                                   command=self.on_exit_position, state="disabled")
        self.btn_exit.pack(side="left", padx=4)

        # ---- P&L strip ----
        strip = tk.Frame(self, bg="#0b1220")
        strip.pack(fill="x", padx=10, pady=(0, 4))
        tk.Label(strip, text="POSITION:", bg="#0b1220", fg="#9fb3d9",
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(8, 4))
        self.lbl_pos = tk.Label(strip, text="FLAT (no open trade)", bg="#0b1220", fg="#e8eefc",
                                font=("Segoe UI", 10))
        self.lbl_pos.pack(side="left")
        tk.Label(strip, text="   LIVE P&L:", bg="#0b1220", fg="#9fb3d9",
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self.lbl_pnl = tk.Label(strip, text="INR 0.00", bg="#0b1220", fg="#9fb3d9")
        self.lbl_pnl.pack_configure()
        self.lbl_pnl.configure(font=("Segoe UI", 16, "bold"))
        self.lbl_pnl.pack(side="left", padx=6)
        tk.Label(strip, text="   SESSION:", bg="#0b1220", fg="#9fb3d9",
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self.lbl_sess = tk.Label(strip, text="INR 0.00", bg="#0b1220", fg="#3fb950",
                                 font=("Segoe UI", 12, "bold"))
        self.lbl_sess.pack(side="left", padx=6)

        # ---- notebook with 3 index tabs ----
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=10, pady=4)
        self.tabs = {}
        for name in INDEXES:
            self.tabs[name] = self._make_tab(name)

        # ---- log ----
        logf = ttk.LabelFrame(self, text=" ACTIVITY LOG ", style="Card.TLabelframe")
        logf.pack(fill="both", padx=10, pady=(2, 8))
        self.txt_log = tk.Text(logf, height=8, bg="#0b1220", fg="#c9d7f2",
                               font=("Consolas", 9), state="disabled", wrap="word")
        self.txt_log.pack(fill="both", expand=True, padx=6, pady=6)

        tk.Label(self, text="Educational tool. Markets carry risk of loss. No profit is guaranteed. "
                            "Upstox Access Token expires daily (~3:30 AM) - generate a fresh one each trading day.",
                 bg="#0e1526", fg="#6e7fa3", font=("Segoe UI", 8)).pack(pady=(0, 6))

        self.cmb_index.bind("<<ComboboxSelected>>", lambda e: self.spn_lotsize.delete(0, "end") or
                            self.spn_lotsize.insert(0, str(self.settings["lot_sizes"].get(
                                self.cmb_index.get(), INDEXES[self.cmb_index.get()]["lot"]))))
        self.on_live_toggle()

    def _make_tab(self, name):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=f"  {name}  ")
        w = {}
        card = ttk.LabelFrame(f, text=f" {name} LIVE ", style="Card.TLabelframe")
        card.pack(fill="x", padx=8, pady=8)
        w["spot"] = tk.Label(card, text="--", bg="#131c33", fg="#e8eefc", font=("Segoe UI", 26, "bold"))
        w["spot"].pack(side="left", padx=14, pady=10)
        w["sig"] = tk.Label(card, text="Signal: -", bg="#131c33", fg="#9fb3d9",
                            font=("Segoe UI", 13, "bold"))
        w["sig"].pack(side="left", padx=16)
        w["atm"] = tk.Label(card, text="ATM: -   IV: -", bg="#131c33", fg="#9fd0ff",
                            font=("Segoe UI", 12))
        w["atm"].pack(side="left", padx=16)

        grid = ttk.LabelFrame(f, text=" OPTION CHAIN + FAIR VALUE (Black-Scholes) ",
                              style="Card.TLabelframe")
        grid.pack(fill="x", padx=8, pady=4)
        heads = ["Strike", "CE LTP", "CE Fair", "CE Diff", "CE Signal",
                 "PE LTP", "PE Fair", "PE Diff", "PE Signal"]
        for c, h in enumerate(heads):
            tk.Label(grid, text=h, bg="#131c33", fg="#9fd0ff",
                     font=("Segoe UI", 9, "bold"), width=13).grid(row=0, column=c, padx=2, pady=4)
        w["rows"] = []
        for r in range(3):
            rowcells = []
            for c in range(9):
                lb = tk.Label(grid, text="-", bg="#131c33", fg="#e8eefc",
                              font=("Consolas", 9), width=13)
                lb.grid(row=r + 1, column=c, padx=2, pady=2)
                rowcells.append(lb)
            w["rows"].append(rowcells)

        logrow = tk.Frame(f, bg="#0e1526")
        logrow.pack(fill="x", padx=8, pady=4)

        posf = ttk.LabelFrame(logrow, text=" POSITION HISTORY (BUY / SELL / CLOSE) ",
                              style="Card.TLabelframe")
        posf.pack(side="left", fill="both", expand=True)
        posbody = tk.Frame(posf, bg="#0b1220")
        posbody.pack(fill="both", expand=True, padx=6, pady=4)
        posbody.grid_rowconfigure(0, weight=1)
        posbody.grid_columnconfigure(0, weight=1)
        w["poslist"] = tk.Text(posbody, width=55, height=11, bg="#0b1220", fg="#c9d7f2",
                               font=("Consolas", 8), state="disabled", wrap="none",
                               selectbackground="#1f6feb", selectforeground="#ffffff")
        vsb = tk.Scrollbar(posbody, orient="vertical", command=w["poslist"].yview)
        hsb = tk.Scrollbar(posbody, orient="horizontal", command=w["poslist"].xview)
        w["poslist"].configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        w["poslist"].grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        w["poslist"].tag_configure("buy", foreground="#3fb950")
        w["poslist"].tag_configure("win", foreground="#3fb950")
        w["poslist"].tag_configure("loss", foreground="#ff7b72")

        note = tk.Label(f, text="Buy logic: momentum (fast vs slow average of live spot) picks side "
                                "(CE for up / PE for down). Fair value from ATM IV flags cheap options. "
                                "Exit: trailing stop / target profit, max loss, signal reversal, or EXIT button.",
                        bg="#0e1526", fg="#6e7fa3", font=("Segoe UI", 9), wraplength=900,
                        justify="left")
        note.pack(anchor="w", padx=10, pady=6)
        return w

    # ---------- helpers ----------
    def log(self, msg):
        self.log_q.put(msg)

    def _drain_queues(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self.txt_log.configure(state="normal")
                self.txt_log.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
                self.txt_log.see("end")
                self.txt_log.configure(state="disabled")
        except queue.Empty:
            pass
        try:
            while True:
                fn = self.ui_q.get_nowait()
                try: fn()
                except Exception: pass
        except queue.Empty:
            pass
        self.after(100, self._drain_queues)

    def set_status(self, text, color):
        def f():
            self.lbl_status.configure(text=text, fg=color)
        self.ui_q.put(f)

    def on_live_toggle(self):
        if self.live_var.get():
            self.mode_lbl.configure(text="LIVE (real money!)", fg="#ff7b72")
        else:
            self.mode_lbl.configure(text="PAPER (safe)", fg="#3fb950")

    # ---------- connect ----------
    def on_connect(self):
        tok = self.ent_token.get().strip()
        if not tok:
            messagebox.showwarning("Missing", "Paste today's Upstox Access Token (or click GET TOKEN).")
            return
        self.btn_connect.configure(state="disabled")
        self.set_status("Connecting...", "#ffb02e")
        self.log("Connecting to Upstox...")

        def work():
            data_msg = ""
            try:
                ok, data = self.api.connect(tok)
            except Exception as e:
                ok, data = False, f"Network error: {e}\n(Check internet / antivirus / VPN.)"
            if ok:
                # login worked; test market data separately since the bot needs it
                try:
                    self.api.ltp_indices()
                except Exception as e:
                    data_msg = str(e)
            def done():
                self.btn_connect.configure(state="normal")
                if ok:
                    avail = data.get("availableBalance", "?")
                    try:
                        avail = f"{float(avail):,.2f}"
                    except Exception:
                        pass
                    who = data.get("user_name") or ""
                    if data_msg:
                        self.set_status("LOGIN OK but DATA API FAILED - bot cannot run", "#ff7b72")
                        self.log(f"Connected {who}. Balance: INR {avail}")
                        self.log(f"DATA API TEST FAILED -> {data_msg}")
                        messagebox.showwarning("Data API problem", data_msg)
                    else:
                        self.set_status(f"CONNECTED - Balance: INR {avail} | Data OK", "#3fb950")
                        self.log(f"Connected {who}. Available balance: INR {avail} | Data API OK")
                    self.btn_start.configure(state="normal")
                    self.settings["remember"] = self.remember_var.get()
                    self.settings["token"] = tok if self.remember_var.get() else ""
                    self.save_settings()
                else:
                    self.set_status("CONNECTION FAILED", "#ff7b72")
                    self.log(f"CONNECT ERROR -> {data}")
                    messagebox.showerror("Connection error", str(data))
            self.ui_q.put(done)
        threading.Thread(target=work, daemon=True).start()

    # ---------- token helper (OAuth login -> Access Token) ----------
    def on_get_token(self):
        win = tk.Toplevel(self)
        win.title("Get Upstox Access Token")
        win.configure(bg="#0e1526")
        win.geometry("700x360")
        win.transient(self)
        lbl = dict(bg="#0e1526", fg="#9fb3d9")
        ents = {}
        fields = [("API Key", False, self.settings.get("api_key", "")),
                  ("API Secret", True, ""),
                  ("Redirect URL", False, self.settings.get("redirect_url", ""))]
        for i, (t, secret, initial) in enumerate(fields):
            tk.Label(win, text=t, **lbl).grid(row=i, column=0, sticky="e", padx=10, pady=8)
            e = tk.Entry(win, width=62, show="*" if secret else "", font=("Consolas", 9))
            e.insert(0, initial)
            e.grid(row=i, column=1, padx=6, sticky="w")
            ents[t] = e
        tk.Label(win, text="(Use the API Key / Secret / Redirect URL from your app at "
                           "account.upstox.com/developer/apps)", **lbl).grid(
            row=3, column=0, columnspan=2, padx=10, sticky="w")

        def open_login():
            key, redir = ents["API Key"].get().strip(), ents["Redirect URL"].get().strip()
            if not key or not redir:
                messagebox.showwarning("Missing", "Enter API Key and Redirect URL first.", parent=win)
                return
            self.settings["api_key"], self.settings["redirect_url"] = key, redir
            self.save_settings()
            url = ("https://api.upstox.com/v2/login/authorization/dialog?response_type=code"
                   f"&client_id={quote(key, safe='')}&redirect_uri={quote(redir, safe='')}")
            webbrowser.open(url)

        ttk.Button(win, text="1) Open Upstox login", command=open_login).grid(
            row=4, column=1, sticky="w", padx=6, pady=10)
        tk.Label(win, text="2) After login, copy the WHOLE address from the browser bar\n"
                           "(it contains ?code=...) and paste it here:", justify="left",
                 **lbl).grid(row=5, column=0, columnspan=2, padx=10, sticky="w")
        ent_code = tk.Entry(win, width=78, font=("Consolas", 9))
        ent_code.grid(row=6, column=0, columnspan=2, padx=10, pady=6, sticky="w")
        lbl_msg = tk.Label(win, text="", **lbl)
        lbl_msg.grid(row=8, column=0, columnspan=2, padx=10, sticky="w")

        def exchange():
            key, sec, redir = (ents["API Key"].get().strip(), ents["API Secret"].get().strip(),
                               ents["Redirect URL"].get().strip())
            raw = ent_code.get().strip()
            code = raw
            if "code=" in raw:
                code = (parse_qs(urlparse(raw).query).get("code") or [""])[0]
            if not (key and sec and redir and code):
                messagebox.showwarning("Missing", "Fill API Key, Secret, Redirect URL and paste the address/code.", parent=win)
                return
            lbl_msg.configure(text="Getting token...")
            def work():
                try:
                    r = requests.post("https://api.upstox.com/v2/login/authorization/token",
                                      headers={"accept": "application/json",
                                               "Content-Type": "application/x-www-form-urlencoded"},
                                      data={"code": code, "client_id": key, "client_secret": sec,
                                            "redirect_uri": redir, "grant_type": "authorization_code"},
                                      timeout=20)
                    d = r.json()
                    tokn = d.get("access_token")
                    err = None if tokn else f"{r.status_code}: {str(d)[:250]}"
                except Exception as e:
                    tokn, err = None, f"Network error: {e}"
                def done():
                    if tokn:
                        self.ent_token.delete(0, "end")
                        self.ent_token.insert(0, tokn)
                        self.log("Access Token received. Click CONNECT.")
                        win.destroy()
                    else:
                        lbl_msg.configure(text="Failed - see message")
                        messagebox.showerror("Token error",
                            (err or "unknown") + "\n\nCommon causes: Redirect URL not EXACTLY the same as "
                            "in your Upstox app, wrong API Secret, or the code was already used "
                            "(codes work once - click step 1 again).", parent=win)
                self.ui_q.put(done)
            threading.Thread(target=work, daemon=True).start()

        ttk.Button(win, text="3) Get Access Token", command=exchange).grid(
            row=7, column=1, sticky="w", padx=6, pady=6)

    # ---------- start / stop ----------
    def on_start(self):
        try:
            lots = max(1, int(self.spn_lots.get()))
            target = float(self.ent_target.get())
            stop = float(self.ent_stop.get())
            interval = max(3, int(self.ent_int.get()))
            trail_start = float(self.ent_trail_start.get())
            trail_gap = float(self.ent_trail_gap.get())
            min_hold = max(0.0, float(self.ent_min_hold.get()))
        except ValueError:
            messagebox.showwarning("Check inputs", "Lots / Target / Max loss / Trail / Min hold / Scan must be numbers.")
            return
        if self.trail_var.get() and (trail_start <= 0 or trail_gap <= 0):
            messagebox.showwarning("Check inputs", "Trail start and Trail gap must be greater than 0.")
            return
        if self.live_var.get():
            if not messagebox.askyesno("LIVE TRADING",
                "You are about to trade with REAL MONEY.\n\nNo bot guarantees profit. "
                "Are you sure you want LIVE mode?"):
                self.live_var.set(False); self.on_live_toggle(); return
        self.settings.update({"lots": lots, "target": target, "stoploss": stop, "interval": interval,
                              "stop_after_profit": self.stop_after_var.get(),
                              "trail_on": self.trail_var.get(), "trail_start": trail_start,
                              "trail_gap": trail_gap, "exit_on_reversal": self.reversal_var.get(),
                              "min_hold_sec": min_hold})
        self.settings["lot_sizes"][self.cmb_index.get()] = int(float(self.spn_lotsize.get() or 1))
        self.save_settings()
        self.running = True
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_exit.configure(state="normal")
        self.log(f"STARTED | mode={'LIVE' if self.live_var.get() else 'PAPER'} | "
                 f"lots={lots} | target=INR {target} | maxloss=INR {stop} | "
                 + (f"trailing ON (starts +{trail_start:.0f}, gap {trail_gap:.0f})"
                    if self.trail_var.get() else "trailing OFF") + " | "
                 + (f"exit-on-reversal ON (min hold {min_hold:.0f}s)"
                    if self.reversal_var.get() else "exit-on-reversal OFF"))
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    def on_stop(self):
        self.running = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.log("STOPPED (open position, if any, is NOT auto-closed - use EXIT POSITION).")

    def on_exit_position(self):
        if not self.position:
            self.log("No open position to exit.")
            return
        threading.Thread(target=self._close_position, args=("Manual exit",), daemon=True).start()

    # ---------- worker loop ----------
    def _loop(self):
        interval = self.settings.get("interval", 5)
        last_chain = 0.0
        while self.running:
            try:
                spots = self.api.ltp_indices()
                now = time.time()
                for name, spot in spots.items():
                    self.state[name]["spot"] = spot
                sel = self.cmb_index.get()
                self._update_tab_labels()

                if now - last_chain >= max(3, interval):
                    last_chain = now
                    self._refresh_chain(sel)

                self._trade_logic(sel)
            except Exception as e:
                self.log(f"ERROR: {e}")
                self.set_status("DATA ERROR (see log) - retrying", "#ffb02e")
                time.sleep(min(interval, 10))
            for _ in range(interval * 10):
                if not self.running: break
                time.sleep(0.1)

    def _refresh_chain(self, name):
        try:
            if name not in self.expiries:
                exps = self.api.expiry_list(name)
                if not exps:
                    self.log(f"{name}: no expiries found.")
                    return
                self.expiries[name] = exps[0]
            exp = self.expiries[name]
            spot, rows = self.api.option_chain(name, exp)
            st = self.state[name]
            st["rows"], st["expiry"] = rows, exp
            if spot > 0:
                st["spot"] = spot
            atm_info = Strategy.analyse_chain(st.get("spot", 0), rows, exp, INDEXES[name]["step"])
            st["atm_info"] = atm_info
        except Exception as e:
            self.log(f"Chain error ({name}): {e}")

    def _update_tab_labels(self):
        for name, w in self.tabs.items():
            spot = self.state[name].get("spot")
            sig, _ = self.strat.momentum_signal(name, spot) if spot else ("NEUTRAL", 0)
            col = {"BULLISH": "#3fb950", "BEARISH": "#ff7b72"}.get(sig, "#9fb3d9")
            def make(nm=name, sp=spot, sg=sig, cl=col, ww=w):
                if sp:
                    ww["spot"].configure(text=f"{sp:,.2f}")
                ww["sig"].configure(text=f"Signal: {sg}", fg=cl)
                ai = self.state[nm].get("atm_info")
                if ai:
                    ww["atm"].configure(text=f"ATM: {ai['atm']:,.0f}   IV: {ai['iv']:.1f}%   "
                                             f"Expiry: {self.state[nm].get('expiry','')}")
                rows = self.state[nm].get("rows") or {}
                if rows and sp:
                    atm = min(rows.keys(), key=lambda k: abs(k - sp))
                    strikes = sorted(rows.keys())
                    i0 = strikes.index(atm)
                    picks = strikes[max(0, i0 - 1): i0 + 2]
                    for r, K in enumerate(picks):
                        a = Strategy.analyse_chain(sp, {K: rows[K]}, self.state[nm].get("expiry", str(date.today())), 50)
                        cells = ww["rows"][r]
                        if not a:
                            continue
                        ce, pe = a["ce"], a["pe"]
                        cells[0].configure(text=f"{K:,.0f}")
                        for off, leg in ((1, ce), (5, pe)):
                            ltp = f"{leg['ltp']:.1f}" if leg["ltp"] is not None else "-"
                            diff = f"{leg['diff']:+.1f}" if leg["diff"] is not None else "-"
                            cells[off].configure(text=ltp)
                            cells[off + 1].configure(text=f"{leg['fv']:.1f}")
                            cells[off + 2].configure(text=diff,
                                fg="#3fb950" if (leg["diff"] or 0) < 0 else "#ff7b72" if (leg["diff"] or 0) > 0 else "#e8eefc")
                            cells[off + 3].configure(text=leg["signal"])
            self.ui_q.put(make)

    # ---------- trading ----------
    def _trade_logic(self, name):
        st = self.state[name]
        spot = st.get("spot")
        if not spot:
            return
        sig, _ = self.strat.momentum_signal(name, spot)
        pnl = self._current_pnl(name)

        if self.position:
            target = self.settings["target"]; stop = self.settings["stoploss"]
            reason = None
            trail_on = bool(self.settings.get("trail_on"))
            t_start = float(self.settings.get("trail_start", 300))
            t_gap = float(self.settings.get("trail_gap", 150))
            peak = self.position.get("peak", 0.0)
            if pnl is not None and self.position["name"] == name:
                peak = max(peak, pnl)
                self.position["peak"] = peak
                if trail_on and peak >= t_start and not self.position.get("trail_active"):
                    self.position["trail_active"] = True
                    self.log(f"TRAILING ACTIVE - locking in profit. Exit if P&L falls to "
                             f"INR {peak - t_gap:+.0f} (peak INR {peak:+.0f}, gap {t_gap:.0f})")
            held_now = (datetime.now() - self.position["time"]).total_seconds()
            if pnl is not None and pnl >= target:
                reason = f"Target profit hit (+INR {pnl:.0f}, held {held_now:.0f}s)"
            elif pnl is not None and pnl <= -stop:
                reason = f"Max loss hit (-INR {abs(pnl):.0f}, held {held_now:.0f}s)"
            elif (trail_on and pnl is not None and self.position.get("trail_active")
                  and pnl <= peak - t_gap):
                reason = (f"Trailing stop hit (peak +INR {peak:.0f}, exit INR {pnl:+.0f}, "
                          f"gave back {peak - pnl:.0f}, held {held_now:.0f}s)")
            elif self.position["name"] == name and self.settings.get("exit_on_reversal", True):
                held = (datetime.now() - self.position["time"]).total_seconds()
                min_hold = float(self.settings.get("min_hold_sec", 60))
                want = "CE" if sig == "BULLISH" else "PE" if sig == "BEARISH" else None
                if want and want != self.position["side"] and held >= min_hold:
                    reason = f"Signal reversed to {sig} (held {held:.0f}s)"
            if reason:
                self._close_position(reason)
            return

        # flat -> look for entry on selected index only
        if name != self.cmb_index.get() or sig == "NEUTRAL":
            return
        ai = st.get("atm_info")
        rows = st.get("rows") or {}
        if not ai or not rows:
            return
        atm = ai["atm"]
        leg_key = "ce" if sig == "BULLISH" else "pe"
        leg = rows.get(atm, {}).get(leg_key) or {}
        sec_id = leg.get("sec_id")
        ltp = leg.get("ltp")
        if not sec_id or ltp is None:
            return
        # edge check from infographic: fair-value mispricing or clear momentum
        fv_leg = ai[leg_key]
        cheap = (fv_leg["diff"] is not None and fv_leg["diff"] < 0)
        if not cheap and abs(self.strat.momentum_signal(name, spot)[1]) < 0.01:
            return  # no edge, no strong momentum -> wait
        self._open_position(name, sig, atm, sec_id, float(ltp), fv_leg)

    def _qty(self, name):
        lot = self.api.lot_sizes.get(name) or self.settings["lot_sizes"].get(name, INDEXES[name]["lot"])
        return int(lot * int(self.spn_lots.get() or 1))

    def _open_position(self, name, sig, strike, sec_id, ltp, fv):
        qty = self._qty(name)
        live = self.live_var.get()
        side = "CE" if sig == "BULLISH" else "PE"
        if live:
            ok, resp = self.api.place_order(sec_id, INDEXES[name]["order_seg"], "BUY", qty)
            if not ok:
                self.log(f"ORDER REJECTED: {resp}")
                return
        entry = ltp * (1.001 if live else 1.0)  # tiny slippage in live
        self.position = {"name": name, "side": side, "strike": strike, "sec_id": sec_id,
                         "qty": qty, "entry": entry, "live": live, "time": datetime.now(),
                         "peak": 0.0, "trail_active": False}
        self.log(f"BUY {name} {strike:.0f} {side} x{qty} @ {entry:.2f} "
                 f"({'LIVE order ' + str(resp.get('orderId')) if live else 'PAPER'}) | "
                 f"fair={fv['fv']:.1f} diff={fv['diff']:+.1f}")
        self._poslist_add(name, f"{datetime.now():%H:%M:%S}  BUY   {side} {strike:.0f}  "
                                 f"x{qty} @ {entry:.2f}  ({'LIVE' if live else 'PAPER'})", "buy")
        self.ui_q.put(lambda: self.lbl_pos.configure(
            text=f"{name} {strike:.0f} {side} x{qty} @ {entry:.2f} ({'LIVE' if live else 'PAPER'})"))

    def _current_pnl(self, name):
        p = self.position
        if not p or p["name"] != name:
            return None
        rows = self.state[name].get("rows") or {}
        leg = rows.get(p["strike"], {}).get("ce" if p["side"] == "CE" else "pe") or {}
        ltp = leg.get("ltp")
        if ltp is None:
            return None
        return (float(ltp) - p["entry"]) * p["qty"]

    def _close_position(self, reason):
        p = self.position
        if not p:
            return
        self.position = None
        name = p["name"]
        rows = self.state[name].get("rows") or {}
        leg = rows.get(p["strike"], {}).get("ce" if p["side"] == "CE" else "pe") or {}
        ltp = leg.get("ltp")
        exit_px = float(ltp) if ltp is not None else p["entry"]
        pnl = (exit_px - p["entry"]) * p["qty"]
        if p["live"]:
            ok, resp = self.api.place_order(p["sec_id"], INDEXES[name]["order_seg"], "SELL", p["qty"])
            self.log(f"SELL {'filled/rejected check: ' + str(resp) if not ok else 'order ' + str(resp.get('orderId'))}")
        sess = getattr(self, "session_pnl", 0.0) + pnl
        self.session_pnl = sess
        col = "#3fb950" if pnl >= 0 else "#ff7b72"
        self.log(f"CLOSED {name} {p['strike']:.0f} {p['side']} @ {exit_px:.2f} | P&L INR {pnl:+.2f} "
                 f"| {reason}")
        self._poslist_add(name, f"{datetime.now():%H:%M:%S}  CLOSE {p['side']} {p['strike']:.0f}  "
                                 f"@ {exit_px:.2f}  P&L {pnl:+.2f}  ({reason})",
                          "win" if pnl >= 0 else "loss")
        halt = pnl > 0 and self.settings.get("stop_after_profit", False)
        if halt:
            self.running = False
            self.log("Profit booked - bot stopped (Stop-after-profit is ON). Press START to trade again.")
        def upd():
            if halt:
                self.btn_start.configure(state="normal")
                self.btn_stop.configure(state="disabled")
            self.lbl_pos.configure(text="FLAT (no open trade)")
            self.lbl_sess.configure(text=f"INR {sess:+.2f}", fg=col)
        self.ui_q.put(upd)

    # ---------- compact position history box ----------
    def _poslist_add(self, name, text, tag=None):
        def do():
            w = self.tabs.get(name)
            if not w:
                return
            tw = w["poslist"]
            tw.configure(state="normal")
            tw.insert("end", text + "\n", (tag,) if tag else ())
            n_lines = int(tw.index("end-1c").split(".")[0])
            if n_lines > 200:
                tw.delete("1.0", f"{n_lines - 200}.0")
            tw.see("end")
            tw.configure(state="disabled")
        self.ui_q.put(do)

    # ---------- live P&L ticker ----------
    def tick_pnl(self):
        pnl = None
        if self.position:
            pnl = self._current_pnl(self.position["name"])
        if pnl is not None:
            col = "#3fb950" if pnl >= 0 else "#ff7b72"
            self.lbl_pnl.configure(text=f"INR {pnl:+,.2f}", fg=col)
        else:
            self.lbl_pnl.configure(text="INR 0.00", fg="#9fb3d9")
        self.after(1000, self.tick_pnl)

if __name__ == "__main__":
    app = BotApp()
    app.session_pnl = 0.0
    app.after(500, app.tick_pnl)
    app.mainloop()
