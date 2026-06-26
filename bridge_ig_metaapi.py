# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════════════════
 PONT IG → MetaApi.cloud (Darwinex Zero) — v2 cloud, sans terminal MT5
═══════════════════════════════════════════════════════════════════════════
 Même principe que bridge_ig_mt5.py (miroir d'événements, IG = source de
 vérité) mais le côté MT5 passe par MetaApi.cloud : leur cloud maintient
 la connexion au compte Darwinex 24/7, ce script peut tourner n'importe où
 (Windows, Linux, mini-VPS…) sans MetaTrader installé.

 Prérequis (une fois) :
   1. Compte sur https://app.metaapi.cloud → générer un token API
   2. Ajouter le compte MT5 : login 4000093713 + mot de passe MASTER +
      serveur Darwinex (de l'email) → noter l'accountId (UUID)
   3. pip install metaapi-cloud-sdk
   4. Remplir la section "metaapi" de config.json

 Usage :
   python bridge_ig_metaapi.py            # boucle normale
   python bridge_ig_metaapi.py --selftest # vérifie tout, n'envoie rien
   python bridge_ig_metaapi.py --once     # un seul cycle (debug)
═══════════════════════════════════════════════════════════════════════════
"""
import asyncio
import json
import logging
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, time as dtime
from pathlib import Path

# Console Windows : forcer l'UTF-8 pour les emojis/symboles des logs
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.json"
STATE_FILE = HERE / "bridge_state_metaapi.json"
LOG_FILE = HERE / "bridge_metaapi.log"

MAGIC = 20260611  # signature des ordres posés par le pont

_handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("bridge-metaapi")
logging.getLogger("metaapi_cloud_sdk").setLevel(logging.WARNING)


# ══════════════════════════════════════════════════════════════════════════
# CONFIG / ÉTAT / TELEGRAM (identiques à la v1)
# ══════════════════════════════════════════════════════════════════════════
def load_config():
    if not CONFIG_FILE.exists():
        log.error("config.json introuvable. Copiez config.example.json -> config.json et remplissez-le.")
        sys.exit(1)
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    for k in ("api_key", "username", "password"):
        if not cfg["ig"].get(k):
            log.error(f"config.json : champ ig.{k} manquant.")
            sys.exit(1)
    ma = cfg.get("metaapi", {})
    if not ma.get("token") or not ma.get("account_id"):
        log.error("config.json : remplissez metaapi.token et metaapi.account_id (depuis app.metaapi.cloud).")
        sys.exit(1)
    return cfg


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"map": {}}  # dealId IG -> {position_id, symbol, volume, ig_size}


# ── Verrou anti-doublon (un seul pont à la fois) ──────────────────────────
_instance_lock = None


def acquire_single_instance():
    """True si ce process obtient le verrou exclusif ; False si un pont tourne déjà."""
    global _instance_lock
    name = "PontIGDarwinexZero_4000093713"
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle or kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _instance_lock = handle
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


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def tg_alert(cfg, msg):
    tok = cfg.get("telegram", {}).get("bot_token", "")
    chat = cfg.get("telegram", {}).get("chat_id", "")
    if not tok or not chat:
        return
    import ssl
    data = json.dumps({"chat_id": chat, "text": f"Pont IG->MetaApi\n{msg}"}).encode()
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
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
        except Exception as e:
            last = e
    log.warning(f"Alerte Telegram impossible : {last}")


# ══════════════════════════════════════════════════════════════════════════
# CLIENT IG (identique à la v1)
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
        try:
            return self._request(path, version=version)[1]
        except RuntimeError as e:
            if "client-token" in str(e) or "401" in str(e):
                log.info("Session IG expirée — reconnexion…")
                self.login()
                return self._request(path, version=version)[1]
            raise

    def positions(self):
        body = self._authed("/positions", "2")
        out = {}
        for p in body.get("positions", []):
            pos, mkt = p.get("position", {}), p.get("market", {})
            deal_id = pos.get("dealId")
            if not deal_id:
                continue
            out[deal_id] = {
                "dealId": deal_id,
                "direction": pos.get("direction"),
                "size": float(pos.get("size") or pos.get("dealSize") or 0),
                "level": float(pos.get("level") or 0),
                "epic": mkt.get("epic", ""),
                "name": mkt.get("instrumentName", ""),
            }
        return out

    def equity(self):
        """Équité du compte de trading IG : le compte « préféré », sinon le mieux doté."""
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


def classify(ig_pos):
    hay = (ig_pos["epic"] + " " + ig_pos["name"]).upper()
    if "NASDAQ" in hay or "US TECH" in hay or "USTECH" in hay:
        return "NASDAQ"
    if "DAX" in hay or "GERMANY 40" in hay or "GER40" in hay:
        return "DAX"
    return None


# ══════════════════════════════════════════════════════════════════════════
# CÔTÉ METAAPI
# ══════════════════════════════════════════════════════════════════════════
class MA:
    """Petit wrapper autour de la connexion RPC MetaApi."""

    def __init__(self, cfg):
        self.cfg = cfg["metaapi"]
        self.conn = None
        self.specs = {}  # cache des spécifications symboles

    async def connect(self):
        from metaapi_cloud_sdk import MetaApi
        api = MetaApi(self.cfg["token"])
        account = await api.metatrader_account_api.get_account(self.cfg["account_id"])
        if account.state not in ("DEPLOYED",):
            log.info(f"Compte MetaApi en état {account.state} — déploiement…")
            await account.deploy()
        await account.wait_connected()
        self.conn = account.get_rpc_connection()
        await self.conn.connect()
        await self.conn.wait_synchronized()
        info = await self.conn.get_account_information()
        log.info(f"Connecté via MetaApi : compte {info.get('login')} ({info.get('server')}) "
                 f"— équité {info.get('equity'):.2f} {info.get('currency')}")
        return info

    async def equity(self):
        info = await self.conn.get_account_information()
        return float(info["equity"])

    async def positions(self):
        return await self.conn.get_positions()

    async def bridge_positions(self):
        """Positions ouvertes par le pont (identifiées par le MAGIC), indexées par
        position_id. Le commentaire (dealId, tronquable) n'est qu'indicatif —
        secours uniquement si le fichier d'état est perdu."""
        out = {}
        for p in await self.positions():
            if p.get("magic") == MAGIC:
                out[p["id"]] = {"comment": p.get("comment", ""), "symbol": p.get("symbol"),
                                "type": p.get("type"), "volume": p.get("volume")}
        return out

    async def resolve_symbol(self, patterns):
        syms = await self.conn.get_symbols()
        for pat in patterns:
            for name in syms:
                if pat.upper() in name.upper():
                    return name
        return None

    async def spec(self, symbol):
        if symbol not in self.specs:
            self.specs[symbol] = await self.conn.get_symbol_specification(symbol)
        return self.specs[symbol]

    async def value_per_point(self, symbol):
        """Valeur (devise du compte) d'1 point pour 1 lot."""
        s = await self.spec(symbol)
        tick_size = s.get("tickSize") or 0
        tick_value = s.get("tickValue") or 0
        if tick_size and tick_value:
            return tick_value / tick_size
        return float(s.get("contractSize") or 0) or None  # repli raisonnable pour un CFD indice

    async def compute_volume(self, symbol, ig_size_eur_pt, ig_equity, mt5_equity, risk_cfg):
        vpp = await self.value_per_point(symbol)
        if not vpp or ig_equity <= 0:
            return None
        target = ig_size_eur_pt * (mt5_equity / ig_equity)
        vol = target / vpp
        s = await self.spec(symbol)
        step = s.get("volumeStep") or 0.01
        vmin = s.get("minVolume") or 0.01
        vmax = s.get("maxVolume") or 100
        vol = max(vmin, min(round(vol / step) * step, vmax, risk_cfg.get("max_volume_lots", 100)))
        return round(vol, 2)

    async def open(self, symbol, direction, volume, stop_pct, dry_run, deal_id):
        price_info = await self.conn.get_symbol_price(symbol)
        buy = direction == "BUY"
        price = price_info["ask"] if buy else price_info["bid"]
        s = await self.spec(symbol)
        digits = int(s.get("digits") or 1)
        sl = round(price * (1 - stop_pct / 100) if buy else price * (1 + stop_pct / 100), digits)
        if dry_run:
            log.info(f"[DRY-RUN] OUVERTURE {symbol} {direction} vol={volume} @~{price} SL={sl}")
            return "DRY", None
        # comment indicatif (tronquable) ; le MAGIC identifie nos positions,
        # le fichier d'état fait foi pour l'appariement.
        opts = {"comment": str(deal_id)[:26], "magic": MAGIC}
        try:
            if buy:
                res = await self.conn.create_market_buy_order(symbol, volume, sl, None, opts)
            else:
                res = await self.conn.create_market_sell_order(symbol, volume, sl, None, opts)
        except Exception as e:
            return None, f"create_order : {e}"
        code = res.get("stringCode") or res.get("numericCode")
        if str(code) not in ("TRADE_RETCODE_DONE", "10009"):
            return None, f"retcode={code}"
        return res.get("positionId") or res.get("orderId"), None

    async def close(self, position_id, dry_run):
        if dry_run:
            log.info(f"[DRY-RUN] FERMETURE position {position_id}")
            return True, None
        try:
            current = {p["id"] for p in await self.positions()}
            if str(position_id) not in {str(i) for i in current}:
                return True, None  # déjà fermée (stop touché ?)
            res = await self.conn.close_position(str(position_id))
            code = res.get("stringCode") or res.get("numericCode")
            if str(code) not in ("TRADE_RETCODE_DONE", "10009"):
                return False, f"échec fermeture {position_id} : {code}"
            return True, None
        except Exception as e:
            return False, f"échec fermeture {position_id} : {e}"


# ══════════════════════════════════════════════════════════════════════════
# CŒUR : cycle de synchronisation (même logique que la v1)
# ══════════════════════════════════════════════════════════════════════════
async def sync_cycle(cfg, ig, ma, state, symbols):
    ig_pos = ig.positions()
    known = state["map"]

    # 1) OUVERTURES
    for deal_id, p in ig_pos.items():
        if deal_id in known:
            continue
        kind = classify(p)
        if kind is None:
            log.info(f"Position IG ignorée (hors périmètre) : {p['name']}")
            known[deal_id] = {"position_id": None, "ignored": True}
            continue
        symbol = symbols.get(kind)
        if not symbol:
            log.error(f"Pas de symbole pour {kind} — {deal_id} non répliqué.")
            continue
        ig_eq = ig.equity()
        ma_eq = await ma.equity()
        vol = await ma.compute_volume(symbol, p["size"], ig_eq, ma_eq, cfg["risk"])
        if not vol:
            log.error(f"Volume incalculable pour {deal_id} ({symbol}).")
            continue
        pos_id, err = await ma.open(symbol, p["direction"], vol,
                                    cfg["risk"]["catastrophe_stop_pct"], cfg["dry_run"], deal_id)
        if err:
            log.error(f"OUVERTURE ÉCHOUÉE {symbol} {p['direction']} : {err}")
            tg_alert(cfg, f"[ERREUR] Échec ouverture {symbol} {p['direction']} ({err})")
            continue
        known[deal_id] = {"position_id": pos_id, "symbol": symbol, "volume": vol, "ig_size": p["size"]}
        save_state(state)
        msg = f"[OUVERT] Ouvert {symbol} {p['direction']} vol={vol} (IG {p['size']}€/pt @ {p['level']})"
        log.info(msg)
        tg_alert(cfg, msg)

    # 2) FERMETURES
    for deal_id in [d for d in list(known) if d not in ig_pos]:
        entry = known.pop(deal_id)
        save_state(state)
        if entry.get("ignored") or entry.get("position_id") is None:
            continue
        if entry.get("position_id") == "DRY":  # position suivie à blanc (dry-run)
            msg = f"[FERME] [DRY-RUN] Fermé {entry.get('symbol')} vol={entry.get('volume')} (IG {deal_id} clôturé)"
            log.info(msg)
            tg_alert(cfg, msg)
            continue
        ok, err = await ma.close(entry["position_id"], cfg["dry_run"])
        if ok:
            msg = f"[FERME] Fermé {entry['symbol']} vol={entry['volume']} (IG {deal_id} clôturé)"
            log.info(msg)
            tg_alert(cfg, msg)
        else:
            log.error(err)
            tg_alert(cfg, f"[ERREUR] {err} — vérifiez sur app.metaapi.cloud !")
            known[deal_id] = entry
            save_state(state)


async def reconcile_at_startup(cfg, ig, ma, state, symbols):
    """Source de vérité = fichier d'état (dealId IG complet -> position_id). Chaque
    position suivie est vérifiée par son position_id — jamais par le commentaire
    (tronquable). Cf. la v1 pour le détail du correctif."""
    open_ids = {p["id"] for p in await ma.positions()}
    ig_pos = ig.positions()

    # Secours : état perdu mais miroirs présents → ré-appariement par instrument/sens
    if not state["map"]:
        orphans = await ma.bridge_positions()  # {position_id: info}
        sym2kind = {v: k for k, v in symbols.items() if v}
        used = set()
        for deal_id, p in ig_pos.items():
            want = "POSITION_TYPE_BUY" if p["direction"] == "BUY" else "POSITION_TYPE_SELL"
            kind = classify(p)
            for pid, info in orphans.items():
                if pid in used:
                    continue
                if sym2kind.get(info["symbol"]) == kind and info["type"] == want:
                    state["map"][deal_id] = {"position_id": pid, "symbol": info["symbol"],
                                             "volume": info["volume"], "ig_size": p["size"]}
                    used.add(pid)
                    log.warning(f"État perdu — miroir ré-apparié par instrument/sens : {deal_id} -> {pid}.")
                    break
        for pid, info in orphans.items():
            if pid not in used:
                log.warning(f"Position MetaApi orpheline ({pid}, {info['symbol']}) sans correspondance IG — laissée OUVERTE (à vérifier).")

    # Vérifie chaque position suivie par son position_id (et non le commentaire)
    for deal_id in list(state["map"]):
        e = state["map"][deal_id]
        pid = e.get("position_id")
        if e.get("ignored") or pid in (None, "DRY"):
            if deal_id not in ig_pos:
                del state["map"][deal_id]
            continue
        if pid not in open_ids:
            log.warning(f"Miroir {deal_id} (position {pid}) absent — stop touché pendant l'arrêt ? Entrée purgée.")
            del state["map"][deal_id]

    # Adoption à blanc des positions IG préexistantes (cf. v1) : non répliquées,
    # seules celles qui apparaissent pendant que le pont tourne le sont.
    for deal_id, p in ig_pos.items():
        if deal_id not in state["map"]:
            state["map"][deal_id] = {"position_id": None, "adopted": True}
            log.info(f"Position IG préexistante {deal_id} ({p['name']}) adoptée à blanc — non répliquée.")

    save_state(state)
    await sync_cycle(cfg, ig, ma, state, symbols)
    log.info("Réconciliation de démarrage terminée.")


async def eod_failsafe(cfg, ig, ma, state):
    """22:05 : ferme tout miroir dont la position IG a disparu. Basé sur le fichier
    d'état + vérif par position_id — jamais sur le commentaire (tronquable)."""
    ig_pos = ig.positions()
    open_ids = {p["id"] for p in await ma.positions()}
    for deal_id in list(state["map"]):
        e = state["map"][deal_id]
        pid = e.get("position_id")
        if e.get("ignored") or pid in (None, "DRY"):
            continue
        if deal_id not in ig_pos and pid in open_ids:
            log.warning(f"FAILSAFE EOD : fermeture du miroir orphelin {deal_id} (position {pid})")
            ok, err = await ma.close(pid, cfg["dry_run"])
            tg_alert(cfg, f"[NUIT] Failsafe 22h05 : miroir orphelin {deal_id} fermé" if ok else f"[ERREUR] Failsafe : {err}")
            state["map"].pop(deal_id, None)
    save_state(state)


def in_window(cfg, now=None):
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    start = dtime(*map(int, cfg["schedule"]["start"].split(":")))
    stop = dtime(*map(int, cfg["schedule"]["stop"].split(":")))
    return start <= now.time() <= stop


async def main():
    cfg = load_config()
    state = load_state()
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    if mode not in ("--selftest", "--once"):
        if not acquire_single_instance():
            log.error("[STOP] Un autre pont tourne déjà — arrêt immédiat pour éviter les doubles positions.")
            sys.exit(3)
        log.info("[VERROU] Verrou unique acquis — ce pont est le seul actif.")

    log.info(f"=== Démarrage du pont MetaApi (dry_run={cfg['dry_run']}) ===")
    ig = IGClient(cfg)
    ig.login()
    log.info(f"Équité IG : {ig.equity():.2f} € — positions ouvertes : {len(ig.positions())}")

    ma = MA(cfg)
    await ma.connect()

    symbols = {}
    for kind, mcfg in cfg["mapping"].items():
        sym = mcfg["mt5_symbol"] if mcfg.get("mt5_symbol") not in ("", "auto", None) \
            else await ma.resolve_symbol(mcfg["patterns"])
        if sym:
            vpp = await ma.value_per_point(sym)
            log.info(f"Mapping {kind} -> {sym} (1 pt/lot ≈ {vpp:.2f})")
        else:
            log.error(f"Symbole introuvable pour {kind} (patterns {mcfg['patterns']})")
        symbols[kind] = sym

    if mode == "--selftest":
        log.info("[OK] SELFTEST OK : IG joignable, MetaApi connecté, symboles mappés. Aucun ordre envoyé.")
        return

    await reconcile_at_startup(cfg, ig, ma, state, symbols)
    if mode == "--once":
        return

    eod_done = None
    eod_t = dtime(*map(int, cfg["schedule"]["eod_sync"].split(":")))
    tg_alert(cfg, f"[OK] Pont MetaApi démarré (dry_run={cfg['dry_run']})")

    while True:
        try:
            now = datetime.now()
            if in_window(cfg, now):
                await sync_cycle(cfg, ig, ma, state, symbols)
                if now.time() >= eod_t and eod_done != now.date():
                    await eod_failsafe(cfg, ig, ma, state)
                    eod_done = now.date()
            await asyncio.sleep(cfg.get("poll_seconds", 5))
        except KeyboardInterrupt:
            log.info("Arrêt demandé. Les stops catastrophe restent en place côté Darwinex.")
            break
        except Exception as e:
            log.error(f"Erreur boucle : {e}")
            tg_alert(cfg, f"[!] Erreur : {e} — nouvel essai dans 60 s")
            await asyncio.sleep(60)
            try:
                ig.login()
            except Exception as e2:
                log.error(f"Re-login IG impossible : {e2}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
