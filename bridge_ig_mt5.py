# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════════════════
 PONT IG → MT5 (Darwinex Zero) — Réplication miroir des positions
═══════════════════════════════════════════════════════════════════════════
 Principe : "miroir d'événements". Le pont interroge l'API IG toutes les
 N secondes. Chaque position qui APPARAÎT chez IG est ouverte au marché
 sur MT5 ; chaque position qui DISPARAÎT chez IG est fermée sur MT5.
 Aucune décision de trading n'est prise ici — IG est la source de vérité.

 Sizing : réplication du risque relatif. IG donne la taille en €/point ;
 on applique le même €/point *proportionnellement à l'équité* du compte
 MT5 (mode "réinvestissement" : les tailles suivent le capital).

 Garde-fous :
   1. Stop catastrophe posé sur chaque position MT5 dès l'ouverture
   2. Réconciliation au démarrage (état IG vs MT5 vs fichier d'état)
   3. Failsafe fin de journée : ferme tout MT5 orphelin (sans miroir IG)
   4. dry_run : simule sans envoyer d'ordre (pour valider sereinement)
   5. Alertes Telegram optionnelles sur chaque événement/erreur

 Usage :
   python bridge_ig_mt5.py            # boucle normale
   python bridge_ig_mt5.py --selftest # vérifie config + connexions, sans trader
   python bridge_ig_mt5.py --once     # un seul cycle de poll (debug)
═══════════════════════════════════════════════════════════════════════════
"""
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, time as dtime
from pathlib import Path

# Console Windows : forcer l'UTF-8 pour les emojis/symboles des logs
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── Chemins ──────────────────────────────────────────────────────────────
HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.json"
STATE_FILE = HERE / "bridge_state.json"
LOG_FILE = HERE / "bridge.log"

MAGIC = 20260611  # signature des ordres posés par le pont

# ── Verrou anti-doublon ────────────────────────────────────────────────────
# Garantit qu'UN SEUL pont peut tourner. Si un second démarre, il ne peut pas
# acquérir le verrou et s'arrête immédiatement → JAMAIS de positions en double.
_instance_lock = None  # référence globale : tant qu'elle vit, le verrou tient


def acquire_single_instance():
    """True si ce process obtient le verrou exclusif ; False si un pont tourne déjà.
    Windows : mutex nommé du noyau. Linux/Mac : verrou fcntl sur un fichier."""
    global _instance_lock
    name = "PontIGDarwinexZero_4000093713"
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, name)
        ERROR_ALREADY_EXISTS = 183
        if not handle or kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False
        _instance_lock = handle  # garder le handle ouvert = garder le verrou
        return True
    else:
        import fcntl
        f = open(HERE / "bridge.lock", "w")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        f.write(str(os.getpid()))
        f.flush()
        _instance_lock = f
        return True

# ── Logging ──────────────────────────────────────────────────────────────
# Handlers : fichier toujours ; console seulement si elle existe (pythonw/VPS = pas de console)
_handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("bridge")


# ══════════════════════════════════════════════════════════════════════════
# CONFIG & ÉTAT
# ══════════════════════════════════════════════════════════════════════════
def load_config():
    if not CONFIG_FILE.exists():
        log.error("config.json introuvable. Copiez config.example.json -> config.json et remplissez-le.")
        sys.exit(1)
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    for k in ("api_key", "username", "password"):
        if not cfg["ig"].get(k):
            log.error(f"config.json : champ ig.{k} manquant (mêmes identifiants que le bouton 'Synchroniser IG' du dashboard).")
            sys.exit(1)
    return cfg


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"map": {}}  # dealId IG -> {ticket, symbol, volume, ig_size}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════
# TELEGRAM (optionnel)
# ══════════════════════════════════════════════════════════════════════════
def tg_alert(cfg, msg, parse_mode=None):
    tok = cfg.get("telegram", {}).get("bot_token", "")
    chat = cfg.get("telegram", {}).get("chat_id", "")
    if not tok or not chat:
        return
    import ssl
    payload = {"chat_id": chat, "text": f"Pont IG → MT5\n{msg}"}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    data = json.dumps(payload).encode()
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    # Voie sûre (certifi si dispo, sinon magasin système) ; repli sans vérif si le
    # magasin de CA racine du VPS n'est pas à jour (notification non critique).
    try:
        import certifi
        safe = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        safe = ssl.create_default_context()
    unverified = ssl.create_default_context()
    unverified.check_hostname = False
    unverified.verify_mode = ssl.CERT_NONE
    last = None
    for ctx in (safe, unverified):
        try:
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10, context=ctx)
            return
        except Exception as e:  # l'alerte ne doit jamais faire tomber le pont
            last = e
    log.warning(f"Alerte Telegram impossible : {last}")


# ══════════════════════════════════════════════════════════════════════════
# CLIENT IG (REST v2 — même API que le dashboard)
# ══════════════════════════════════════════════════════════════════════════
IG_BASE = "https://api.ig.com/gateway/deal"


class IGClient:
    def __init__(self, cfg):
        self.cfg = cfg["ig"]
        self.cst = None
        self.xst = None

    def _request(self, path, method="GET", version="2", body=None, auth=True):
        headers = {
            "Accept": "application/json; charset=UTF-8",
            "X-IG-API-KEY": self.cfg["api_key"],
            "Version": version,
        }
        if auth:
            headers["CST"] = self.cst or ""
            headers["X-SECURITY-TOKEN"] = self.xst or ""
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json; charset=UTF-8"
        req = urllib.request.Request(IG_BASE + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return dict(r.headers), json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"IG {method} {path} -> HTTP {e.code} : {detail}") from None

    def login(self):
        hdrs, _ = self._request("/session", "POST", "2",
                                {"identifier": self.cfg["username"], "password": self.cfg["password"]},
                                auth=False)
        self.cst = hdrs.get("CST") or hdrs.get("cst")
        self.xst = hdrs.get("X-SECURITY-TOKEN") or hdrs.get("x-security-token")
        if not self.cst or not self.xst:
            raise RuntimeError("Connexion IG : jetons CST/X-SECURITY-TOKEN absents.")
        log.info("Connecté à l'API IG.")

    def _authed(self, path, version="2"):
        """GET avec re-login automatique si la session a expiré."""
        try:
            return self._request(path, version=version)[1]
        except RuntimeError as e:
            if "client-token" in str(e) or "401" in str(e):
                log.info("Session IG expirée — reconnexion…")
                self.login()
                return self._request(path, version=version)[1]
            raise

    def positions(self):
        """Liste des positions ouvertes IG, normalisée."""
        body = self._authed("/positions", "2")
        out = {}
        for p in body.get("positions", []):
            pos, mkt = p.get("position", {}), p.get("market", {})
            deal_id = pos.get("dealId")
            if not deal_id:
                continue
            out[deal_id] = {
                "dealId": deal_id,
                "direction": pos.get("direction"),               # BUY / SELL
                "size": float(pos.get("size") or pos.get("dealSize") or 0),  # € / point
                "level": float(pos.get("level") or 0),
                "epic": mkt.get("epic", ""),
                "name": mkt.get("instrumentName", ""),
            }
        return out

    def equity(self):
        """Équité du compte de trading IG : le compte « préféré », sinon le mieux doté.
        (Un login IG peut porter plusieurs comptes, dont certains à 0.)"""
        body = self._authed("/accounts", "1")
        best = 0.0
        for acc in body.get("accounts", []):
            bal = acc.get("balance") or {}
            eq = float(bal.get("balance", 0)) + float(bal.get("profitLoss", 0))
            if acc.get("preferred") and eq > 0:
                return eq
            best = max(best, eq)
        if best <= 0:
            raise RuntimeError("Aucun compte IG avec une équité positive — vérifiez le compte.")
        return best


# ══════════════════════════════════════════════════════════════════════════
# CÔTÉ MT5
# ══════════════════════════════════════════════════════════════════════════
try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


def mt5_connect(cfg, fatal=True):
    """Initialise/relance le terminal MT5 et vérifie le compte.
    fatal=True (démarrage) : arrête le pont en cas d'échec.
    fatal=False (reconnexion à chaud) : retourne None sans tuer le pont."""
    if mt5 is None:
        log.error("Module MetaTrader5 absent : pip install MetaTrader5")
        sys.exit(1)
    m = cfg["mt5"]
    kwargs = {}
    if m.get("terminal_path"):
        kwargs["path"] = m["terminal_path"]
    if m.get("password"):  # sinon : on s'attache au terminal déjà connecté
        kwargs.update(login=int(m["login"]), server=m["server"], password=m["password"])
    if not mt5.initialize(**kwargs):
        log.error(f"Échec connexion MT5 : {mt5.last_error()} — le terminal MT5 est-il lancé et connecté au compte {m['login']} ?")
        if fatal:
            sys.exit(1)
        return None
    info = mt5.account_info()
    if info is None or (m.get("login") and info.login != int(m["login"])):
        log.error(f"Terminal MT5 connecté au compte {getattr(info,'login','?')} au lieu de {m['login']}.")
        if fatal:
            sys.exit(1)
        return None
    log.info(f"Connecté à MT5 : compte {info.login} ({info.server}) — équité {info.equity:.2f} {info.currency}")
    return info


def mt5_alive():
    """True si le terminal MT5 répond et reste connecté au compte."""
    try:
        return mt5 is not None and mt5.account_info() is not None
    except Exception:
        return False


def resolve_symbol(patterns):
    """Trouve le symbole MT5 Darwinex correspondant (ex. NDX / GER40)."""
    all_syms = [s.name for s in (mt5.symbols_get() or [])]
    for pat in patterns:
        for name in all_syms:
            if pat.upper() in name.upper():
                mt5.symbol_select(name, True)
                return name
    return None


def euro_per_point_per_lot(symbol):
    """Valeur (devise du compte) d'1 point de l'indice pour 1 lot."""
    si = mt5.symbol_info(symbol)
    if si is None or si.trade_tick_size == 0:
        return None
    return si.trade_tick_value / si.trade_tick_size


def compute_volume(symbol, ig_size_eur_pt, ig_equity, mt5_equity, risk_cfg):
    """Même €/point qu'IG, à l'échelle de l'équité MT5 (réinvestissement)."""
    eppl = euro_per_point_per_lot(symbol)
    if not eppl or ig_equity <= 0:
        return None
    target_eur_pt = ig_size_eur_pt * (mt5_equity / ig_equity)
    vol = target_eur_pt / eppl
    si = mt5.symbol_info(symbol)
    step = si.volume_step or 0.01
    vol = max(si.volume_min, min(round(vol / step) * step, si.volume_max,
                                 risk_cfg.get("max_volume_lots", 100)))
    return round(vol, 2)


def mt5_open(symbol, direction, volume, stop_pct, dry_run, deal_id):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None, f"Pas de cotation pour {symbol}"
    buy = direction == "BUY"
    price = tick.ask if buy else tick.bid
    sl = price * (1 - stop_pct / 100) if buy else price * (1 + stop_pct / 100)
    si = mt5.symbol_info(symbol)
    sl = round(sl, si.digits)
    if dry_run:
        log.info(f"[DRY-RUN] OUVERTURE {symbol} {direction} vol={volume} @~{price} SL={sl}")
        return -1, None
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
        "deviation": 50,
        "sl": sl,
        "magic": MAGIC,
        # dealId en commentaire à titre indicatif seulement : le serveur MT5 peut
        # le TRONQUER (Darwinex coupe à ~16 car.). L'appariement fait foi via le
        # fichier d'état (dealId complet -> ticket), jamais via ce commentaire.
        "comment": str(deal_id)[:31],
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    res = mt5.order_send(req)
    if res is None:
        return None, f"order_send None : {mt5.last_error()}"
    if res.retcode == mt5.TRADE_RETCODE_INVALID_FILL:  # certains serveurs exigent IOC
        req["type_filling"] = mt5.ORDER_FILLING_IOC
        res = mt5.order_send(req)
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        return None, f"retcode={res.retcode} {res.comment}"
    return res.order, None


def mt5_close(ticket, dry_run):
    """Ferme la position MT5 et retourne (ok, err, info).
    info = {pnl, pct, price_open, price_close, currency, volume, direction} pour la notif."""
    if dry_run:
        log.info(f"[DRY-RUN] FERMETURE ticket {ticket}")
        return True, None, None
    pos = next((p for p in (mt5.positions_get() or []) if p.ticket == ticket), None)
    if pos is None:
        return True, None, None  # déjà fermée (stop touché ?) — rien à faire
    tick = mt5.symbol_info_tick(pos.symbol)
    acc = mt5.account_info()
    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    # P&L latent juste avant la clôture au marché ≈ P&L réalisé (à un demi-spread près)
    pnl = float(pos.profit)
    equity = float(acc.equity) if acc else 0.0
    base = equity - pnl  # capital hors ce trade
    info = {
        "pnl": pnl,
        "pct": (pnl / base * 100) if base else 0.0,
        "price_open": pos.price_open,
        "price_close": (tick.bid if is_buy else tick.ask) if tick else None,
        "currency": acc.currency if acc else "",
        "volume": pos.volume,
        "direction": "BUY" if is_buy else "SELL",
    }
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pos.symbol,
        "volume": pos.volume,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "position": ticket,
        "deviation": 50,
        "magic": MAGIC,
        "comment": "IG close",
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    res = mt5.order_send(req)
    if res is not None and res.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
        req["type_filling"] = mt5.ORDER_FILLING_IOC
        res = mt5.order_send(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        return False, f"échec fermeture {ticket} : {getattr(res,'retcode','?')} {getattr(res,'comment','')}", None
    return True, None, info


def mt5_bridge_positions():
    """Positions MT5 ouvertes par le pont (identifiées par le MAGIC), indexées par
    ticket. Le commentaire (dealId, potentiellement tronqué) n'est qu'indicatif —
    sert uniquement au ré-appariement de secours si le fichier d'état est perdu."""
    out = {}
    for p in mt5.positions_get() or []:
        if p.magic == MAGIC:
            out[p.ticket] = {"comment": p.comment, "symbol": p.symbol,
                             "type": p.type, "volume": p.volume}
    return out


# ══════════════════════════════════════════════════════════════════════════
# MAPPING INSTRUMENT
# ══════════════════════════════════════════════════════════════════════════
def classify(ig_pos):
    """NASDAQ / DAX / None à partir de l'epic ou du nom IG."""
    hay = (ig_pos["epic"] + " " + ig_pos["name"]).upper()
    if "NASDAQ" in hay or "US TECH" in hay or "USTECH" in hay:
        return "NASDAQ"
    if "DAX" in hay or "GERMANY 40" in hay or "GER40" in hay:
        return "DAX"
    return None


def instrument_display(symbol_or_kind):
    """(drapeau, nom lisible) pour les notifs Telegram, à partir du kind
    (NASDAQ/DAX) ou du symbole MT5 (NDX/GDAXI)."""
    s = (symbol_or_kind or "").upper()
    if "NDX" in s or "NASDAQ" in s:
        return "🇺🇸", "NASDAQ"
    if "GDAXI" in s or "DAX" in s:
        return "🇩🇪", "DAX 40"
    return "", str(symbol_or_kind)


# ══════════════════════════════════════════════════════════════════════════
# CŒUR : un cycle de synchronisation
# ══════════════════════════════════════════════════════════════════════════
def sync_cycle(cfg, ig, state, symbols):
    ig_pos = ig.positions()
    known = state["map"]

    # 1) OUVERTURES : positions IG sans miroir MT5
    for deal_id, p in ig_pos.items():
        if deal_id in known:
            continue
        kind = classify(p)
        if kind is None:
            log.info(f"Position IG ignorée (instrument hors périmètre) : {p['name']}")
            known[deal_id] = {"ticket": None, "ignored": True}
            continue
        symbol = symbols.get(kind)
        if not symbol:
            log.error(f"Pas de symbole MT5 pour {kind} — position {deal_id} non répliquée.")
            continue
        ig_eq = ig.equity()
        mt5_eq = mt5.account_info().equity
        vol = compute_volume(symbol, p["size"], ig_eq, mt5_eq, cfg["risk"])
        if not vol:
            log.error(f"Volume incalculable pour {deal_id} ({symbol}).")
            continue
        ticket, err = mt5_open(symbol, p["direction"], vol,
                               cfg["risk"]["catastrophe_stop_pct"], cfg["dry_run"], deal_id)
        if err:
            log.error(f"OUVERTURE ÉCHOUÉE {symbol} {p['direction']} : {err}")
            tg_alert(cfg, f"[ERREUR] Échec ouverture {symbol} {p['direction']} ({err})")
            continue
        known[deal_id] = {"ticket": ticket, "symbol": symbol, "volume": vol, "ig_size": p["size"]}
        save_state(state)
        log.info(f"[OUVERT] {symbol} {p['direction']} vol={vol} (IG {p['size']}EUR/pt @ {p['level']})")
        sens = "⬆️ ACHAT" if p["direction"] == "BUY" else "⬇️ VENTE"
        flag, name = instrument_display(kind)
        tg_alert(cfg, f"{flag} {name}  {sens}\n{vol} lot · entrée {p['level']}")

    # 2) FERMETURES : miroirs dont la position IG a disparu
    for deal_id in [d for d in list(known) if d not in ig_pos]:
        entry = known.pop(deal_id)
        save_state(state)
        if entry.get("ignored") or entry.get("ticket") is None:
            continue
        if entry.get("ticket") == -1:  # position suivie à blanc (dry-run)
            msg = f"[FERME] [DRY-RUN] Fermé {entry.get('symbol')} vol={entry.get('volume')} (IG {deal_id} clôturé)"
            log.info(msg)
            tg_alert(cfg, msg)
            continue
        ok, err, info = mt5_close(entry["ticket"], cfg["dry_run"])
        if ok:
            sym, vol = entry["symbol"], entry["volume"]
            if info and info.get("pnl") is not None:
                pnl, cur, pct = info["pnl"], info["currency"], info["pct"]
                gain = pnl >= 0
                # console (texte simple, lisible dans voir_pont.bat)
                log.info(f"[FERME] {sym} vol={vol} PnL={pnl:+.0f} {cur} ({pct:+.2f}%) (IG {deal_id})")
                # Telegram : gros emoji de couleur + montant en GRAS (lisible sur fond
                # sombre, gain comme perte — le bloc diff rendait le rouge peu lisible).
                head = "🟢 GAIN" if gain else "🔴 PERTE"
                flag, name = instrument_display(sym)
                detail = ""
                if info.get("price_open") and info.get("price_close"):
                    detail = f"\n{info['direction']} {vol} lot · {info['price_open']} → {info['price_close']}"
                tg_alert(cfg,
                         f"{head} — {flag} <b>{name}</b> clôturé\n"
                         f"{head[0]} <b>{pnl:+.0f} {cur}</b>   (<b>{pct:+.2f} %</b>){detail}",
                         parse_mode="HTML")
            else:
                log.info(f"[FERME] {sym} vol={vol} (IG {deal_id} clôturé)")
                tg_alert(cfg, f"[FERME] {sym} clôturé")
        else:
            log.error(err)
            tg_alert(cfg, f"[ERREUR] {err} — FERMEZ MANUELLEMENT sur MT5 !")
            known[deal_id] = entry  # on réessaiera au prochain cycle
            save_state(state)


def reconcile_at_startup(cfg, ig, state, symbols):
    """Aligne état / MT5 / IG après un (re)démarrage.

    Source de vérité = le fichier d'état (dealId IG complet -> ticket MT5).
    Chaque position suivie est vérifiée par son TICKET — jamais par le commentaire
    MT5, que le serveur Darwinex tronque à ~16 car. (sinon l'appariement casse et
    une position est fermée à tort)."""
    open_tickets = {p.ticket for p in (mt5.positions_get() or [])}
    ig_pos = ig.positions()

    # Secours : fichier d'état perdu mais des miroirs existent sur MT5 → on les
    # ré-apparie aux positions IG par (instrument, sens), pas par le commentaire.
    if not state["map"]:
        orphans = mt5_bridge_positions()  # {ticket: info}
        sym2kind = {v: k for k, v in symbols.items() if v}
        used = set()
        for deal_id, p in ig_pos.items():
            want_type = 0 if p["direction"] == "BUY" else 1
            kind = classify(p)
            for tk, info in orphans.items():
                if tk in used:
                    continue
                if sym2kind.get(info["symbol"]) == kind and info["type"] == want_type:
                    state["map"][deal_id] = {"ticket": tk, "symbol": info["symbol"],
                                             "volume": info["volume"], "ig_size": p["size"]}
                    used.add(tk)
                    log.warning(f"État perdu — miroir ré-apparié par instrument/sens : {deal_id} -> ticket {tk}.")
                    break
        for tk, info in orphans.items():
            if tk not in used:
                log.warning(f"Position MT5 orpheline (ticket {tk}, {info['symbol']}) sans correspondance IG — laissée OUVERTE (à vérifier).")

    # Vérifie chaque position suivie par son TICKET (et non le commentaire)
    for deal_id in list(state["map"]):
        e = state["map"][deal_id]
        tk = e.get("ticket")
        if e.get("ignored") or tk in (None, -1):
            # entrée « à blanc » : ne la garder que si la position IG existe encore
            if deal_id not in ig_pos:
                del state["map"][deal_id]
            continue
        if tk not in open_tickets:
            log.warning(f"Miroir MT5 {deal_id} (ticket {tk}) absent — stop touché pendant l'arrêt ? Entrée purgée.")
            del state["map"][deal_id]

    # Adoption à blanc : une position IG déjà ouverte AVANT que le pont ne la
    # réplique (aucun miroir MT5) n'est PAS convertie — entrer en cours de route à
    # un prix décalé fausserait le miroir. Seules les positions qui APPARAISSENT
    # pendant que le pont tourne sont répliquées.
    for deal_id, p in ig_pos.items():
        if deal_id not in state["map"]:
            state["map"][deal_id] = {"ticket": None, "adopted": True}
            log.info(f"Position IG préexistante {deal_id} ({p['name']}) adoptée à blanc — non répliquée.")

    save_state(state)
    sync_cycle(cfg, ig, state, symbols)
    log.info("Réconciliation de démarrage terminée.")


def eod_failsafe(cfg, ig, state):
    """22:05 : tout miroir MT5 encore ouvert dont la position IG a disparu est fermé.
    Basé sur le fichier d'état (dealId complet) + vérif par ticket — jamais sur le
    commentaire MT5 (tronquable)."""
    ig_pos = ig.positions()
    open_tickets = {p.ticket for p in (mt5.positions_get() or [])}
    for deal_id in list(state["map"]):
        e = state["map"][deal_id]
        tk = e.get("ticket")
        if e.get("ignored") or tk in (None, -1):
            continue
        if deal_id not in ig_pos and tk in open_tickets:
            log.warning(f"FAILSAFE EOD : fermeture du miroir orphelin {deal_id} (ticket {tk})")
            ok, err, _ = mt5_close(tk, cfg["dry_run"])
            tg_alert(cfg, f"[NUIT] Failsafe 22h05 : miroir orphelin {deal_id} fermé" if ok else f"[ERREUR] Failsafe : {err}")
            state["map"].pop(deal_id, None)
    save_state(state)


# ══════════════════════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ══════════════════════════════════════════════════════════════════════════
def in_window(cfg, now=None):
    now = now or datetime.now()
    if now.weekday() >= 5:  # samedi/dimanche
        return False
    start = dtime(*map(int, cfg["schedule"]["start"].split(":")))
    stop = dtime(*map(int, cfg["schedule"]["stop"].split(":")))
    return start <= now.time() <= stop


def main():
    cfg = load_config()
    state = load_state()
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    # Verrou anti-doublon (pas pour les diagnostics courts --selftest/--once)
    if mode not in ("--selftest", "--once"):
        if not acquire_single_instance():
            log.error("[STOP] Un autre pont tourne déjà — arrêt immédiat pour éviter les doubles positions.")
            sys.exit(3)  # code 3 = doublon : le lanceur .bat ne redémarre pas
        log.info("[VERROU] Verrou unique acquis — ce pont est le seul actif.")

    log.info(f"=== Démarrage du pont (dry_run={cfg['dry_run']}) ===")
    ig = IGClient(cfg)
    ig.login()
    log.info(f"Équité IG : {ig.equity():.2f} € — positions ouvertes : {len(ig.positions())}")

    mt5_connect(cfg)
    symbols = {}
    for kind, mcfg in cfg["mapping"].items():
        sym = mcfg["mt5_symbol"] if mcfg.get("mt5_symbol") not in ("", "auto", None) \
            else resolve_symbol(mcfg["patterns"])
        if sym:
            mt5.symbol_select(sym, True)
            log.info(f"Mapping {kind} -> {sym} (1 pt/lot = {euro_per_point_per_lot(sym):.2f} {mt5.account_info().currency})")
        else:
            log.error(f"Symbole MT5 introuvable pour {kind} (patterns {mcfg['patterns']})")
        symbols[kind] = sym

    if mode == "--selftest":
        log.info("[OK] SELFTEST OK : IG joignable, MT5 connecté, symboles mappés. Aucun ordre envoyé.")
        return

    reconcile_at_startup(cfg, ig, state, symbols)
    if mode == "--once":
        return

    eod_done = None
    eod_t = dtime(*map(int, cfg["schedule"]["eod_sync"].split(":")))
    tg_alert(cfg, f"[OK] Pont démarré (dry_run={cfg['dry_run']})")

    while True:
        try:
            now = datetime.now()
            if in_window(cfg, now):
                # Garde-fou : si le terminal MT5 s'est fermé/déconnecté, le relancer
                # avant de trader (sinon le cycle échouerait).
                if not mt5_alive():
                    log.warning("MT5 ne répond plus — tentative de reconnexion…")
                    try:
                        mt5.shutdown()
                    except Exception:
                        pass
                    if mt5_connect(cfg, fatal=False):
                        for kind, sym in symbols.items():
                            if sym:
                                mt5.symbol_select(sym, True)
                        log.info("MT5 reconnecté.")
                        tg_alert(cfg, "[OK] MT5 reconnecté après une coupure.")
                    else:
                        log.error("Reconnexion MT5 impossible — nouvel essai au prochain cycle.")
                        tg_alert(cfg, "[!] MT5 injoignable — reconnexion en cours.")
                        time.sleep(30)
                        continue
                sync_cycle(cfg, ig, state, symbols)
                if now.time() >= eod_t and eod_done != now.date():
                    eod_failsafe(cfg, ig, state)
                    eod_done = now.date()
            time.sleep(cfg.get("poll_seconds", 5))
        except KeyboardInterrupt:
            log.info("Arrêt demandé. Les stops catastrophe restent en place sur MT5.")
            break
        except Exception as e:
            log.error(f"Erreur boucle : {e}")
            indice = ""
            if "api-key-invalid" in str(e):
                indice = " (clé IG suspendue : utilisée en parallèle ailleurs ? — voir clé dédiée)"
            tg_alert(cfg, f"[!] Erreur : {e}{indice} — nouvel essai dans 20 s")
            time.sleep(20)
            try:
                ig.login()
            except Exception as e2:
                log.error(f"Re-login IG impossible : {e2}")
            try:  # rétablir aussi MT5 si c'est lui qui a lâché
                if not mt5_alive():
                    mt5.shutdown()
                    mt5_connect(cfg, fatal=False)
            except Exception as e3:
                log.error(f"Reconnexion MT5 impossible : {e3}")


if __name__ == "__main__":
    main()
