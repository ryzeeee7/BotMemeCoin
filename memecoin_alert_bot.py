#!/usr/bin/env python3
"""
Memecoin Alert Bot (Solana / DexScreener + RugCheck + Telegram)
=================================================================

COSA FA:
Monitora periodicamente nuovi token/pool su Solana via l'API pubblica di
DexScreener, calcola uno score di viralita' (stesso principio dello scanner
precedente: accelerazione volume, rapporto buy/sell, penalita' per boost a
pagamento e per pool appena nati), applica un controllo di sicurezza minimo
via RugCheck (mint/freeze authority, risk score) e, se un token supera le
soglie configurate, invia una notifica su Telegram.

COSA NON FA (di proposito):
  - NON si collega a nessun wallet
  - NON firma ne' invia alcuna transazione sulla blockchain
  - NON compra ne' vende nulla in automatico
E' uno strumento di segnalazione: individua e ti avvisa, la decisione
resta sempre e solo tua.

REQUISITI:
  pip install requests

CONFIGURAZIONE:
Imposta due variabili d'ambiente prima di avviare lo script (altrimenti
gli alert vengono solo stampati a schermo, non inviati su Telegram):
  TELEGRAM_BOT_TOKEN   -> 8739105067:AAEtSroB8vhC3t_Gpay-9CxNdfshnM_rL30
  TELEGRAM_CHAT_ID     -> 847172274,928004300

  macOS / Linux:
    export TELEGRAM_BOT_TOKEN= 8739105067:AAEtSroB8vhC3t_Gpay-9CxNdfshnM_rL30
    export TELEGRAM_CHAT_ID= 847172274,928004300
  Windows (PowerShell):
    $env:TELEGRAM_BOT_TOKEN= 8739105067:AAEtSroB8vhC3t_Gpay-9CxNdfshnM_rL30
    $env:TELEGRAM_CHAT_ID= 847172274,928004300

USO:
  python memecoin_alert_bot.py
  python memecoin_alert_bot.py --interval 60 --min-liquidity 8000 --score-threshold 10

Ctrl+C per fermarlo in qualsiasi momento (lo stato viene salvato prima di uscire).
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Manca la libreria 'requests'. Installala con: pip install requests")

DEXSCREENER_BASE = "https://api.dexscreener.com"
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
CHAIN = "solana"
SEEN_FILE = Path(__file__).parent / "seen_tokens.json"


# ---------------------------------------------------------------------------
# Persistenza dei token gia' notificati (evita di avvisarti due volte)
# ---------------------------------------------------------------------------
def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_seen(seen):
    try:
        SEEN_FILE.write_text(json.dumps(sorted(seen)))
    except OSError as e:
        print(f"[Attenzione] impossibile salvare {SEEN_FILE}: {e}")


# ---------------------------------------------------------------------------
# DexScreener: candidati + dati di mercato
# ---------------------------------------------------------------------------
def get_candidate_tokens(session, limit_profiles=30):
    """Ritorna {token_address: is_boosted} per token Solana recenti."""
    candidates = {}

    try:
        resp = session.get(f"{DEXSCREENER_BASE}/token-profiles/latest/v1", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            for item in data[:limit_profiles]:
                if item.get("chainId") == CHAIN and item.get("tokenAddress"):
                    candidates[item["tokenAddress"]] = False
    except requests.RequestException as e:
        print(f"[Attenzione] token-profiles non raggiungibile: {e}")
    except ValueError as e:
        print(f"[Attenzione] risposta non valida da token-profiles: {e}")

    time.sleep(1)  # rispetta il rate limit 60/min di questo endpoint

    try:
        resp = session.get(f"{DEXSCREENER_BASE}/token-boosts/latest/v1", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            for item in data:
                if item.get("chainId") == CHAIN and item.get("tokenAddress"):
                    candidates[item["tokenAddress"]] = True
    except requests.RequestException as e:
        print(f"[Attenzione] token-boosts non raggiungibile: {e}")
    except ValueError as e:
        print(f"[Attenzione] risposta non valida da token-boosts: {e}")

    return candidates


def get_pairs_for_token(session, token_address):
    try:
        resp = session.get(
            f"{DEXSCREENER_BASE}/token-pairs/v1/{CHAIN}/{token_address}", timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except requests.RequestException:
        return []
    except ValueError:
        return []


def compute_score(pair, is_boosted):
    """Score euristico di viralita'. None se i dati non sono utilizzabili."""
    txns = pair.get("txns") or {}
    volume = pair.get("volume") or {}
    liquidity = pair.get("liquidity") or {}

    m5 = txns.get("m5") or {}
    h1 = txns.get("h1") or {}

    buys_m5 = m5.get("buys") or 0
    sells_m5 = m5.get("sells") or 0
    buys_h1 = h1.get("buys") or 0
    sells_h1 = h1.get("sells") or 0

    vol_m5 = volume.get("m5") or 0
    vol_h1 = volume.get("h1") or 0
    liq_usd = liquidity.get("usd") or 0

    if liq_usd <= 0:
        return None  # senza liquidita' nota non possiamo valutare in sicurezza

    projected_hourly = vol_m5 * 12
    acceleration = projected_hourly / vol_h1 if vol_h1 > 0 else (2.0 if vol_m5 > 0 else 0)

    buy_ratio_m5 = buys_m5 / max(sells_m5, 1)
    buy_ratio_h1 = buys_h1 / max(sells_h1, 1)
    vol_liq_ratio = (vol_h1 / liq_usd) if liq_usd > 0 else 0

    score = (
        acceleration * 3.0
        + buy_ratio_m5 * 2.0
        + buy_ratio_h1 * 1.0
        + min(vol_liq_ratio, 5.0) * 1.0
    )

    if is_boosted:
        score *= 0.6  # promozione pagata, non viralita' organica

    if pair.get("pairCreatedAt"):
        age_minutes = (time.time() * 1000 - pair["pairCreatedAt"]) / 60000
        if age_minutes < 10:
            score *= 0.7  # pool appena nato, dati piu' rumorosi

    return round(score, 2)


# ---------------------------------------------------------------------------
# RugCheck: controllo di sicurezza minimo
# ---------------------------------------------------------------------------
def check_rugcheck_safety(session, token_address):
    """
    Ritorna {"safe": bool, "reason": str, "risk_score": int|None}.
    In caso di errore di rete o risposta non valida, ritorna safe=False:
    se non riesco a verificare, non presumo che il token sia sicuro.
    """
    try:
        resp = session.get(f"{RUGCHECK_BASE}/tokens/{token_address}/report", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return {"safe": False, "reason": f"RugCheck non raggiungibile ({e})", "risk_score": None}
    except ValueError:
        return {"safe": False, "reason": "risposta RugCheck non valida", "risk_score": None}

    if not isinstance(data, dict):
        return {"safe": False, "reason": "risposta RugCheck inattesa", "risk_score": None}

    mint_authority = data.get("mintAuthority")
    freeze_authority = data.get("freezeAuthority")
    risk_score = data.get("score")

    problems = []
    if mint_authority:
        problems.append("mint authority attiva")
    if freeze_authority:
        problems.append("freeze authority attiva")
    if isinstance(risk_score, (int, float)) and risk_score >= 80:
        problems.append(f"risk score RugCheck alto ({risk_score})")

    if problems:
        return {"safe": False, "reason": ", ".join(problems), "risk_score": risk_score}
    return {"safe": True, "reason": "nessun problema rilevato", "risk_score": risk_score}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram_alert(session, bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = session.post(
            url,
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[Attenzione] invio Telegram a {chat_id} fallito: {e}")
        return False


def send_alert_to_all_chats(session, bot_token, chat_ids, text):
    """
    Invia lo stesso messaggio a piu' chat Telegram (una per ogni destinatario).
    Se l'invio a una chat fallisce, continua comunque con le altre invece di
    interrompersi: un chat_id sbagliato non deve bloccare gli altri.
    """
    all_ok = True
    for chat_id in chat_ids:
        ok = send_telegram_alert(session, bot_token, chat_id, text)
        all_ok = all_ok and ok
    return all_ok


def parse_chat_ids(raw_value):
    """
    Converte la variabile d'ambiente TELEGRAM_CHAT_ID (una stringa, con uno o
    piu' id separati da virgola) in una lista pulita di chat_id.
    Esempio: "123456789, 987654321" -> ["123456789", "987654321"]
    """
    if not raw_value:
        return []
    return [c.strip() for c in raw_value.split(",") if c.strip()]


def format_alert_message(pair, score, safety, is_boosted):
    symbol = (pair.get("baseToken") or {}).get("symbol", "?")
    dex = pair.get("dexId", "?")
    price = pair.get("priceUsd", "?")
    liquidity = (pair.get("liquidity") or {}).get("usd", 0) or 0
    url = pair.get("url", "")

    safety_line = "OK sicurezza base" if safety["safe"] else f"ATTENZIONE: {safety['reason']}"
    boost_line = " (promozione a pagamento attiva)" if is_boosted else ""

    return (
        f"<b>{symbol}</b> — score {score}{boost_line}\n"
        f"DEX: {dex} | Prezzo: ${price} | Liquidita': ${liquidity:,.0f}\n"
        f"{safety_line}\n"
        f"{url}"
    )


# ---------------------------------------------------------------------------
# Un ciclo completo di scansione (isolato in una funzione per poterlo testare
# con dati finti, senza dover chiamare davvero la rete)
# ---------------------------------------------------------------------------
def run_once(session, seen, min_liquidity, score_threshold, require_safety, bot_token, chat_ids):
    """
    chat_ids: lista di chat Telegram a cui inviare gli alert (puo' essere
    vuota o None se si vuole solo la stampa a schermo, senza invio).
    """
    new_alerts = 0
    candidates = get_candidate_tokens(session)

    for idx, (token_address, is_boosted) in enumerate(candidates.items()):
        if token_address in seen:
            continue

        pairs = get_pairs_for_token(session, token_address)
        for pair in pairs:
            liquidity = (pair.get("liquidity") or {}).get("usd", 0) or 0
            if liquidity < min_liquidity:
                continue

            score = compute_score(pair, is_boosted)
            if score is None or score < score_threshold:
                continue

            safety = check_rugcheck_safety(session, token_address)
            if require_safety and not safety["safe"]:
                continue

            message = format_alert_message(pair, score, safety, is_boosted)
            if bot_token and chat_ids:
                send_alert_to_all_chats(session, bot_token, chat_ids, message)
            print(message.replace("<b>", "").replace("</b>", ""))
            new_alerts += 1

        seen.add(token_address)

        if idx % 20 == 0:
            time.sleep(0.5)  # rispetta il rate limit DexScreener (300/min)

    return new_alerts


def main():
    parser = argparse.ArgumentParser(description="Memecoin Alert Bot (Solana) - solo monitoraggio")
    parser.add_argument("--interval", type=int, default=90,
                         help="Secondi tra un controllo e l'altro (default: 90)")
    parser.add_argument("--min-liquidity", type=float, default=5000,
                         help="Liquidita' minima USD per considerare un pool (default: 5000)")
    parser.add_argument("--score-threshold", type=float, default=8.0,
                         help="Score minimo per inviare un alert (default: 8.0)")
    parser.add_argument("--no-safety-filter", action="store_true",
                         help="Disattiva il filtro di sicurezza RugCheck (sconsigliato)")
    parser.add_argument("--once", action="store_true",
                         help="Esegue un solo controllo ed esce, senza loop. "
                              "Usato quando la ripetizione e' gestita da uno scheduler "
                              "esterno (es. GitHub Actions o un cron di sistema).")
    args = parser.parse_args()

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_ids = parse_chat_ids(os.environ.get("TELEGRAM_CHAT_ID"))
    if not bot_token or not chat_ids:
        print(
            "[Attenzione] TELEGRAM_BOT_TOKEN e/o TELEGRAM_CHAT_ID non impostati.\n"
            "Gli alert verranno solo stampati qui a schermo, non inviati su Telegram.\n"
        )
    elif len(chat_ids) > 1:
        print(f"Alert configurati per {len(chat_ids)} chat Telegram diverse.\n")

    seen = load_seen()
    session = requests.Session()

    if args.once:
        found = run_once(
            session, seen, args.min_liquidity, args.score_threshold,
            not args.no_safety_filter, bot_token, chat_ids,
        )
        print(f"Controllo singolo completato: {found} segnale/i trovato/i.")
        save_seen(seen)
        return

    print(f"Avviato. Controllo ogni {args.interval}s. Ctrl+C per fermare.\n")
    try:
        while True:
            found = run_once(
                session,
                seen,
                args.min_liquidity,
                args.score_threshold,
                not args.no_safety_filter,
                bot_token,
                chat_ids,
            )
            if found == 0:
                print(f"[{time.strftime('%H:%M:%S')}] nessun nuovo segnale sopra soglia.")
            save_seen(seen)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nInterrotto dall'utente. Salvataggio stato...")
        save_seen(seen)
        print("Fatto.")


if __name__ == "__main__":
    main()
