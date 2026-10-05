#!/usr/bin/env python3
"""Radar Bambu: coleta ofertas, calcula custo efetivo, guarda histórico e alerta no Telegram."""
import csv, html, json, os, re, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
HIST = DATA / "history.csv"
STATE = DATA / "state.json"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}
NOW = datetime.now(timezone.utc)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------- classificação ----------
class Classifier:
    def __init__(self, cfg):
        self.models = cfg["modelos"]
        self.glob = [re.compile(p, re.I) for p in cfg.get("excluir_global", [])]
        self.rules = {
            k: ([re.compile(p, re.I) for p in m.get("todos", [])],
                [re.compile(p, re.I) for p in m.get("nenhum", [])])
            for k, m in self.models.items()
        }

    def classify(self, title, price_brl, forced=None):
        if forced:
            return forced if forced in self.models else None
        if any(r.search(title) for r in self.glob):
            return None
        for k, (todos, nenhum) in self.rules.items():
            if all(r.search(title) for r in todos) and not any(r.search(title) for r in nenhum):
                if price_brl < (self.models[k].get("piso") or 0):
                    return None
                return k
        return None


# ---------- câmbio e custo ----------
def usd_brl(cfg):
    try:
        r = requests.get("https://economia.awesomeapi.com.br/json/last/USD-BRL", timeout=15)
        return float(r.json()["USDBRL"]["bid"])
    except Exception as e:
        log("câmbio: fallback", e)
        return float(cfg["cambio"]["fallback_usd_brl"])


def custo_efetivo(price, moeda, frete_brl, paraguai, cfg, fx):
    if moeda == "BRL":
        return round(price + frete_brl, 2)
    taxa = fx * (1 + cfg["cambio"]["spread"])
    py = cfg["paraguai"]
    usd = price * (1 + py["taxa_contato_pct"] / 100)
    imposto = 0.0
    if paraguai:
        excesso = max(0.0, usd - py["cota_usd"])
        imposto = excesso * py["imposto_excesso"]
        frete_brl += py["frete_fixo_brl"]
    return round((usd + imposto) * taxa + frete_brl, 2)


# ---------- fontes ----------
def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if re.search(r"\d\.\d{3},\d", s) or ("," in s and "." not in s):
        s = s.replace(".", "").replace(",", ".")
    s = re.sub(r"[^\d.]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def parse_page(page):
    """Extrai (titulo, preco, moeda, disponivel) de JSON-LD ou metatags."""
    title, price, cur, avail = None, None, None, None
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        for node in _walk(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" in types and not title:
                title = node.get("name")
            if "Offer" in types or "AggregateOffer" in types:
                p = _num(node.get("price")) or _num(node.get("lowPrice"))
                if p and (price is None or p < price):
                    price, cur = p, node.get("priceCurrency")
                    avail = node.get("availability")
    if price is None:
        for pat in (r'property="product:price:amount"\s+content="([^"]+)"',
                    r'itemprop="price"\s+content="([^"]+)"',
                    r'content="([^"]+)"\s+itemprop="price"'):
            m = re.search(pat, page, re.I)
            if m:
                price = _num(m.group(1))
                break
    if not title:
        m = re.search(r'property="og:title"\s+content="([^"]+)"', page, re.I) or \
            re.search(r"<title>(.*?)</title>", page, re.I | re.S)
        title = m.group(1).strip() if m else ""
    disponivel = None if avail is None else ("InStock" in str(avail) or "PreOrder" in str(avail))
    return title, price, cur, disponivel


def fonte_paginas(cfg, fx):
    out = []
    for p in cfg["fontes"].get("paginas") or []:
        try:
            r = requests.get(p["url"], headers=UA, timeout=25)
            r.raise_for_status()
            title, price, cur, disp = parse_page(r.text)
            if not price:
                log(f"[{p['nome']}] preço não encontrado na página")
                continue
            if disp is False:
                log(f"[{p['nome']}] fora de estoque")
                continue
            moeda = p.get("moeda") or cur or "BRL"
            out.append(dict(fonte=p["nome"], titulo=title, preco=price, moeda=moeda, url=p["url"],
                            frete=p.get("frete_brl", 0), paraguai=p.get("paraguai", False),
                            forced=p.get("modelo")))
        except Exception as e:
            log(f"[{p['nome']}] erro: {e}")
        time.sleep(2)
    return out


def _ml_token():
    cid, sec = os.getenv("ML_CLIENT_ID"), os.getenv("ML_CLIENT_SECRET")
    if not (cid and sec):
        return None
    try:
        r = requests.post("https://api.mercadolibre.com/oauth/token", timeout=20, data={
            "grant_type": "client_credentials", "client_id": cid, "client_secret": sec})
        return r.json().get("access_token")
    except Exception as e:
        log("ML token:", e)
        return None


def fonte_mercado_livre(cfg, fx):
    c = cfg["fontes"].get("mercado_livre") or {}
    if not c.get("ativo"):
        return []
    tok = _ml_token()
    h = dict(UA, **({"Authorization": f"Bearer {tok}"} if tok else {}))
    out = []
    for q in c.get("buscas", []):
        try:
            r = requests.get("https://api.mercadolibre.com/sites/MLB/search",
                             params={"q": q, "limit": 50, "condition": "new"}, headers=h, timeout=25)
            if r.status_code in (401, 403):
                log("Mercado Livre exige token: configure ML_CLIENT_ID / ML_CLIENT_SECRET")
                return out
            r.raise_for_status()
            for it in r.json().get("results", []):
                if it.get("condition") != "new":
                    continue
                free = (it.get("shipping") or {}).get("free_shipping", False)
                out.append(dict(fonte="Mercado Livre", titulo=it["title"], preco=float(it["price"]),
                                moeda=it.get("currency_id", "BRL"), url=it["permalink"],
                                frete=0 if free else c.get("frete_padrao_brl", 0),
                                paraguai=False, forced=None))
        except Exception as e:
            log(f"[ML '{q}'] erro: {e}")
    return out


# ---------- estado / histórico ----------
def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"melhor": {}, "ultimo": {}, "alertado": {}}


def append_history(rows):
    new = not HIST.exists()
    with HIST.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "modelo", "canal", "fonte", "titulo", "preco", "moeda", "custo_efetivo", "url"])
        for o in rows:
            w.writerow([NOW.isoformat(timespec="minutes"), o["modelo"], o["canal"], o["fonte"], o["titulo"],
                        o["preco"], o["moeda"], o["custo"], o["url"]])


def daily_min_series():
    series = {}
    if not HIST.exists():
        return series
    with HIST.open() as f:
        for r in csv.DictReader(f):
            if r.get("canal") == "py":
                continue
            d = r["ts"][:10]
            s = series.setdefault(r["modelo"], {})
            v = float(r["custo_efetivo"])
            if d not in s or v < s[d]:
                s[d] = v
    return {m: sorted(s.items()) for m, s in series.items()}


# ---------- alertas ----------
def telegram(msg):
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        log("[alerta sem Telegram]\n" + msg)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=20, data={
            "chat_id": chat, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": "true"})
    except Exception as e:
        log("Telegram:", e)


def brl(v):
    return "R$ " + f"{v:,.0f}".replace(",", ".")


def decide_alerts(offers, state, cfg):
    a = cfg["alertas"]
    cool = timedelta(hours=a["cooldown_horas"])
    pct_py = cfg["paraguai"].get("alerta_se_economia_pct")
    best_br = {}
    for o in offers:
        if o["canal"] == "br":
            best_br.setdefault(o["modelo"], o["custo"])  # offers já ordenadas por custo
    msgs = []
    for o in offers:
        k, m = o["url"], cfg["modelos"][o["modelo"]]
        motivos = []
        if o["canal"] == "py":
            ref = best_br.get(o["modelo"])
            if pct_py is not None and ref and o["custo"] <= ref * (1 - pct_py / 100):
                motivos.append(f"Paraguai {100 * (1 - o['custo'] / ref):.0f}% abaixo do melhor preço BR "
                               f"({brl(ref)}) — sem garantia/suporte no Brasil")
        else:
            alvo = m.get("alvo")
            if alvo and o["custo"] <= alvo:
                motivos.append(f"abaixo do alvo ({brl(alvo)})")
            best = state["melhor"].get(o["modelo"])
            if a["novo_minimo"] and best and o["custo"] < best["custo"] * 0.995:
                motivos.append(f"novo mínimo histórico BR (antes {brl(best['custo'])})")
            last = state["ultimo"].get(k)
            if last and o["custo"] <= last * (1 - a["queda_pct"] / 100):
                motivos.append(f"caiu {100 * (1 - o['custo'] / last):.1f}% (era {brl(last)})")
        prev = state["alertado"].get(k)
        if motivos and prev:
            fresh = NOW - datetime.fromisoformat(prev["ts"]) > cool
            if not fresh and o["custo"] >= prev["custo"]:
                motivos = []
        if motivos:
            sel = "🇵🇾 " if o["canal"] == "py" else ""
            msgs.append(f"{sel}<b>{m['nome']}</b> — {brl(o['custo'])} efetivo\n"
                        f"{html.escape(o['fonte'])}: {html.escape(o['titulo'][:90])}\n" +
                        "\n".join("• " + x for x in motivos) + f"\n{o['url']}")
            state["alertado"][k] = {"ts": NOW.isoformat(), "custo": o["custo"]}
    return msgs


# ---------- principal ----------
def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    DATA.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)
    fx = usd_brl(cfg)
    clf = Classifier(cfg)
    raw = fonte_mercado_livre(cfg, fx) + fonte_paginas(cfg, fx)

    offers, seen = [], set()
    for o in raw:
        if o["url"] in seen:
            continue
        seen.add(o["url"])
        o["custo"] = custo_efetivo(o["preco"], o["moeda"], o["frete"], o["paraguai"], cfg, fx)
        o["modelo"] = clf.classify(o["titulo"], o["custo"], o.pop("forced"))
        if o["modelo"]:
            o["canal"] = "py" if o["paraguai"] else "br"
            offers.append(o)
    offers.sort(key=lambda o: o["custo"])
    log(f"{len(raw)} anúncios lidos, {len(offers)} válidos, USD/BRL {fx:.3f}")

    state = load_state()
    msgs = decide_alerts(offers, state, cfg)
    for o in offers:
        state["ultimo"][o["url"]] = o["custo"]
        b = state["melhor"].get(o["modelo"])
        if o["canal"] == "br" and (not b or o["custo"] < b["custo"]):
            state["melhor"][o["modelo"]] = {"custo": o["custo"], "fonte": o["fonte"],
                                            "url": o["url"], "ts": NOW.isoformat()}
    if offers:
        append_history(offers)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    for msg in msgs:
        telegram(msg)

    best_now, best_py = {}, {}
    for o in offers:
        (best_now if o["canal"] == "br" else best_py).setdefault(o["modelo"], o)
    (DOCS / "data.json").write_text(json.dumps({
        "atualizado": NOW.isoformat(timespec="minutes"),
        "usd_brl": fx,
        "modelos": {k: {"nome": m["nome"], "alvo": m.get("alvo")} for k, m in cfg["modelos"].items()},
        "agora": best_now,
        "agora_py": best_py,
        "melhor_historico": state["melhor"],
        "ofertas": offers[:200],
        "serie": daily_min_series(),
    }, ensure_ascii=False))
    log(f"{len(msgs)} alerta(s)")


if __name__ == "__main__":
    main()
