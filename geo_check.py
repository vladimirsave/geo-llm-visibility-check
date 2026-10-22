#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""geo-llm-visibility-check — проверка сайта на видимость для поисковиков
и для языковых моделей. Один файл, только стандартная библиотека.

Девять пунктов приёмки. Каждый проверяется запросом к живому сайту, каждый
отвечает «прошёл», «не прошёл» или «не применимо», и к каждому выводится
причина, а не только вердикт: по голому «не прошёл» непонятно, что чинить.

  python geo_check.py https://example.com
  python geo_check.py https://example.com --json
  python geo_check.py --selftest          прогон на встроенных образцах, без сети

Код возврата: 0 — все обязательные пункты прошли, 1 — есть провалы.
"""
from __future__ import annotations

import gzip
import json
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone

UA = "geo-llm-visibility-check/1.0 (+https://github.com/topics/seo)"
TIMEOUT = 25

# Агенты, которым доступ нужен отдельной строкой. Со звёздочкой тоже работает,
# но многие сайты закрывают звёздочку и открывают поимённо — тогда отсутствие
# строки означает отказ. Поэтому проверяем каждого.
AI_AGENTS = [
    "GPTBot", "OAI-SearchBot", "ChatGPT-User",
    "ClaudeBot", "Claude-SearchBot", "Claude-User",
    "PerplexityBot", "Google-Extended", "Applebot-Extended",
    "meta-externalagent", "Amazonbot", "Bytespider",
]
SEARCH_AGENTS = ["Googlebot", "Bingbot", "YandexBot"]

# Типы структурированных данных, которые осмысленно видеть на сайте бренда.
USEFUL_TYPES = {
    "Organization", "WebSite", "Brand", "Product", "Offer", "FAQPage",
    "AboutPage", "ContactPage", "ContactPoint", "CollectionPage",
    "BreadcrumbList", "Article", "NewsArticle", "MedicalWebPage", "Person",
}


# ------------------------------------------------------------------ загрузка

def unpack(raw: bytes, encoding: str) -> bytes:
    """Разжать тело ответа.

    Часть серверов за сетями доставки отдаёт gzip, даже когда его не просили:
    так ведёт себя, например, python.org. Пока это не учитывалось, проверка
    видела двоичный мусор и сообщала, что на странице нет ни заголовка, ни
    мета-тегов — то есть врала. Поэтому разжимаем по факту, а не по запросу.
    """
    enc = (encoding or "").lower()
    if "gzip" in enc or raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if "deflate" in enc:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def fetch(url: str) -> tuple[int, str, dict]:
    """Страница по адресу. Возвращает код, текст и заголовки.
    Код 0 означает, что до сервера не дошли вовсе."""
    # Просим именно gzip и не просим brotli: распаковщика brotli в стандартной
    # библиотеке нет, а зависимостей у этого инструмента нет по условию.
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = unpack(r.read(6_000_000), r.headers.get("Content-Encoding", ""))
            charset = r.headers.get_content_charset() or "utf-8"
            return r.status, raw.decode(charset, errors="replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, "", dict(e.headers or {})
    except Exception:                                        # noqa: BLE001
        return 0, "", {}


def final_url(url: str) -> str:
    """Куда адрес приводит после всех перенаправлений."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.url
    except urllib.error.HTTPError as e:
        return e.url or url
    except Exception:                                        # noqa: BLE001
        return ""


# -------------------------------------------------------------------- robots

def parse_robots(text: str) -> tuple[dict[str, list[tuple[str, str]]], list[str]]:
    """Группы правил по агентам и строки Sitemap.

    Файл robots.txt устроен группами: несколько строк User-agent подряд
    относятся к одному набору правил ниже. Разбираем именно так, иначе правила
    достаются только последнему агенту в группе.
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    sitemaps: list[str] = []
    current: list[str] = []
    fresh = True
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, value = line.split(":", 1)
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if not fresh:
                current = []
                fresh = True
            current.append(value.lower())
            groups.setdefault(value.lower(), [])
        elif field in ("allow", "disallow"):
            fresh = False
            for agent in current:
                groups.setdefault(agent, []).append((field, value))
        elif field == "sitemap":
            sitemaps.append(value)
    return groups, sitemaps


def allowed(groups: dict[str, list[tuple[str, str]]], agent: str, path="/") -> bool:
    """Пустит ли robots.txt этого агента на указанный путь.

    Правило отбора по стандарту: берётся САМОЕ ДЛИННОЕ совпадение префикса, и
    при равной длине Allow сильнее Disallow. Своя группа агента полностью
    перекрывает звёздочку — даже если в ней всего одна строка.
    """
    rules = groups.get(agent.lower())
    if rules is None:
        rules = groups.get("*", [])
    best_len, best_kind = -1, None
    for kind, value in rules:
        if value == "":
            # «Disallow:» без значения означает «можно всё» и к совпадениям
            # не относится. Длину считаем нулевой и трактуем как allow.
            if kind == "disallow" and best_len < 0:
                best_len, best_kind = 0, "allow"
            continue
        pattern = value.rstrip("$")
        if path.startswith(pattern.replace("*", "")):
            if len(pattern) > best_len or (len(pattern) == best_len and kind == "allow"):
                best_len, best_kind = len(pattern), kind
    return best_kind != "disallow"


# ----------------------------------------------------------------- проверки

def check_crawlers(base: str) -> dict:
    code, text, _ = fetch(base.rstrip("/") + "/robots.txt")
    if code != 200 or not text.strip():
        return {"state": "fail", "why": "robots.txt не отдаётся (код %s)" % code}
    groups, sitemaps = parse_robots(text)
    blocked_ai = [a for a in AI_AGENTS if not allowed(groups, a)]
    blocked_search = [a for a in SEARCH_AGENTS if not allowed(groups, a)]
    named = [a for a in AI_AGENTS + SEARCH_AGENTS if a.lower() in groups]
    why = "агентов пропускается %d из %d, поимённо указано %d" % (
        len(AI_AGENTS) + len(SEARCH_AGENTS) - len(blocked_ai) - len(blocked_search),
        len(AI_AGENTS) + len(SEARCH_AGENTS), len(named))
    if not sitemaps:
        why += "; строки Sitemap нет"
    if blocked_search:
        return {"state": "fail", "why": why + "; закрыты поисковики: "
                + ", ".join(blocked_search)}
    if blocked_ai:
        return {"state": "fail", "why": why + "; закрыты модели: "
                + ", ".join(blocked_ai)}
    return {"state": "pass" if sitemaps else "fail", "why": why}


def check_sitemap(base: str) -> dict:
    code, text, _ = fetch(base.rstrip("/") + "/sitemap.xml")
    if code != 200:
        return {"state": "fail", "why": "sitemap.xml не отдаётся (код %s)" % code}
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)
    mods = re.findall(r"<lastmod>", text)
    if not locs:
        if "<sitemapindex" in text:
            return {"state": "pass", "why": "это индекс карт, вложенных карт: %d"
                    % text.count("<sitemap>")}
        return {"state": "fail", "why": "адресов внутри нет"}
    return {"state": "pass", "why": "адресов %d, из них с датой правки %d"
            % (len(locs), len(mods))}


def check_llms(base: str) -> dict:
    code, text, _ = fetch(base.rstrip("/") + "/llms.txt")
    if code != 200:
        return {"state": "fail", "why": "llms.txt не отдаётся (код %s)" % code}
    if len(text.strip()) < 100:
        return {"state": "fail", "why": "файл есть, но пустой по сути (%d знаков)"
                % len(text.strip())}
    return {"state": "pass", "why": "%d знаков, строк %d"
            % (len(text.strip()), len(text.strip().splitlines()))}


def jsonld_types(html: str) -> tuple[list[str], int]:
    """Типы структурированных данных и число неразобравшихся блоков."""
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.S | re.I)
    types, broken = [], 0

    def walk(node):
        if isinstance(node, dict):
            t = node.get("@type")
            if isinstance(t, str):
                types.append(t)
            elif isinstance(t, list):
                types.extend(x for x in t if isinstance(x, str))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for block in blocks:
        try:
            walk(json.loads(block))
        except Exception:                                    # noqa: BLE001
            broken += 1
    return types, broken


def check_jsonld(html: str) -> dict:
    types, broken = jsonld_types(html)
    if not types and not broken:
        return {"state": "fail", "why": "структурированных данных нет"}
    if broken:
        return {"state": "fail", "why": "блоков не разобралось: %d; типы: %s"
                % (broken, ", ".join(sorted(set(types))) or "нет")}
    useful = sorted(set(types) & USEFUL_TYPES)
    if not useful:
        return {"state": "fail", "why": "разметка есть, но полезных типов нет: %s"
                % ", ".join(sorted(set(types)))}
    return {"state": "pass", "why": "типы: " + ", ".join(useful)}


def check_meta(html: str) -> dict:
    """Мета-теги: и отсутствие, и ЗАДВОЕНИЕ.

    Задвоение проверяется наравне с отсутствием, потому что ведёт себя хуже.
    Отсутствующий тег виден в любом инструменте и его замечают. Два разных
    canonical на одной странице выглядят как настроенная страница, а выдача
    выбирает из них сама — и часто не тот, который имели в виду. То же с
    описанием: показан будет один из двух, и предсказать какой нельзя.
    Этот случай нашёлся на живом сайте уже после того, как проверка на наличие
    его пропустила.
    """
    counts = {
        "title": len(re.findall(r"<title[\s>]", html, re.I)),
        "описание": len(re.findall(r'<meta[^>]+name=["\']description["\']', html, re.I)),
        "canonical": len(re.findall(r'<link[^>]+rel=["\']canonical["\']', html, re.I)),
        "Open Graph": len(re.findall(r'<meta[^>]+property=["\']og:', html, re.I)),
        "карточка Twitter": len(re.findall(r'<meta[^>]+name=["\']twitter:', html, re.I)),
        "lang у html": len(re.findall(r"<html[^>]+lang=", html, re.I)),
    }
    # Описание без содержимого считаем отсутствующим: пустой тег не помогает.
    if counts["описание"] and not re.search(
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']\s*\S', html, re.I):
        counts["описание"] = 0

    missing = [k for k, v in counts.items() if v == 0]
    # Open Graph и Twitter — наборы из нескольких тегов, у них дубль не считаем.
    single = ("title", "описание", "canonical", "lang у html")
    doubled = ["%s ×%d" % (k, counts[k]) for k in single if counts[k] > 1]

    parts = []
    if missing:
        parts.append("не хватает: " + ", ".join(missing))
    if doubled:
        parts.append("задвоено: " + ", ".join(doubled))
    if parts:
        return {"state": "fail", "why": "; ".join(parts)}
    return {"state": "pass", "why": "всё по одному: title, описание, canonical, "
                                    "язык; тегов Open Graph %d, Twitter %d"
                                    % (counts["Open Graph"], counts["карточка Twitter"])}


def check_counters(html: str) -> dict:
    """Счётчик должен быть ровно один на страницу.

    Дубль не ломает страницу визуально и потому живёт месяцами, а статистику
    портит молча: визиты и отказы считаются дважды.
    """
    found: dict[str, int] = {}

    def add(key: str) -> None:
        found[key] = found.get(key, 0) + 1

    # Идентификатор счётчика Метрики встречается тремя способами, и все три
    # живут в реальных сборках: в адресе скрипта, в вызове init числом и в
    # вызове init строкой. Плагины для готовых систем управления сайтом ставят
    # как раз последний — скрипт подключают без параметров, а номер передают
    # строкой. Пока учитывался только первый, проверка сообщала «счётчика нет»
    # на страницах, где он стоял и работал.
    for m in re.finditer(r"metrika/tag(?:_ww)?\.js\?id=(\d+)", html):
        add("Метрика " + m.group(1))
    for m in re.finditer(r"""ym\(\s*["']?(\d{5,})["']?\s*,\s*["']init["']""", html):
        key = "Метрика " + m.group(1)
        found[key] = max(found.get(key, 0), 1)
    for m in re.finditer(r"gtag\(\s*[\"']config[\"']\s*,\s*[\"'](G-[A-Z0-9]+)[\"']", html):
        add(m.group(1))
    for m in re.finditer(r"googletagmanager\.com/gtm\.js\?id=(GTM-[A-Z0-9]+)", html):
        add(m.group(1))

    # Пустой идентификатор контейнера — отдельная беда: снippet вставлен,
    # выглядит как подключённая аналитика и не собирает ничего.
    empty_gtm = re.search(r"googletagmanager\.com/gtm\.js\?id=(?![A-Za-z0-9])", html)

    if not found:
        why = "счётчика на странице нет"
        if empty_gtm:
            why += "; при этом подключён диспетчер тегов без идентификатора контейнера"
        return {"state": "fail", "why": why}
    doubled = [k for k, v in found.items() if v > 1]
    if doubled:
        return {"state": "fail", "why": "дубль счётчика: " + ", ".join(doubled)}
    why = "счётчиков по одному: " + ", ".join(sorted(found))
    if empty_gtm:
        return {"state": "fail", "why": why
                + "; но диспетчер тегов подключён без идентификатора контейнера"}
    return {"state": "pass", "why": why}


def check_goals(html: str) -> dict:
    """Событийные цели. Без вызова в коде в кабинете работают только цели по
    адресу страницы, то есть ни кнопки, ни формы не считаются."""
    ym = len(re.findall(r"reachGoal", html))
    ga = len(re.findall(r"gtag\(\s*[\"']event[\"']", html))
    if ym + ga == 0:
        return {"state": "warn", "why": "вызовов целей в коде нет — считаются "
                                        "только переходы по адресам"}
    return {"state": "pass", "why": "вызовов целей в коде: %d" % (ym + ga)}


def check_tls(url: str) -> dict:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        return {"state": "fail", "why": "адрес не по https"}
    host = parts.hostname or ""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, parts.port or 443), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except Exception as exc:                                 # noqa: BLE001
        return {"state": "fail", "why": "сертификат не проверился: %s" % exc}
    until = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(
        tzinfo=timezone.utc)
    days = (until - datetime.now(timezone.utc)).days
    issuer = dict(x[0] for x in cert.get("issuer", ()) if x).get("organizationName", "")
    state = "pass" if days > 14 else "fail"
    return {"state": state, "why": "действует ещё %d дн, выдан %s"
            % (days, issuer or "неизвестно")}


def check_host_agreement(url: str) -> dict:
    """www и адрес без www должны сводиться к одному. Две живые версии сайта —
    это разделённый вес ссылок и раздвоенная статистика."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    other = host[4:] if host.startswith("www.") else "www." + host
    target = urllib.parse.urlunsplit((parts.scheme, other, "/", "", ""))
    landed = final_url(target)
    if not landed:
        return {"state": "warn", "why": "%s не отвечает — проверить вручную" % other}
    landed_host = urllib.parse.urlsplit(landed).hostname or ""
    if landed_host == host:
        return {"state": "pass", "why": "%s сводится к %s" % (other, host)}
    if landed_host == other:
        return {"state": "fail", "why": "%s отвечает сам и не сводится к %s — "
                                        "это две живые копии сайта" % (other, host)}
    return {"state": "fail", "why": "%s уводит на третий адрес %s"
            % (other, landed_host)}


def check_feed(base: str, html: str) -> dict:
    if re.search(r'rel=["\']alternate["\'][^>]+(?:rss|atom)\+xml', html, re.I):
        return {"state": "pass", "why": "лента объявлена в разметке страницы"}
    for path in ("/rss.xml", "/feed.xml", "/feed/", "/atom.xml"):
        code, _, _ = fetch(base.rstrip("/") + path)
        if code == 200:
            return {"state": "pass", "why": "лента найдена по адресу " + path}
    return {"state": "warn", "why": "ленты не нашлось"}


# -------------------------------------------------------------------- отчёт

MARK = {"pass": "прошёл", "fail": "НЕ ПРОШЁЛ", "warn": "замечание"}
REQUIRED = {1, 2, 3, 4, 5, 6, 8, 9}        # пункты 7 и 10 — замечания, не провал


def run(url: str) -> tuple[list[dict], bool]:
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    code, html, _ = fetch(url)
    if code != 200 or not html:
        return [{"n": 0, "name": "страница открывается",
                 "state": "fail", "why": "код ответа %s" % code}], False

    items = [
        {"n": 1, "name": "доступ ботам моделей и поисковиков", **check_crawlers(base)},
        {"n": 2, "name": "карта сайта", **check_sitemap(base)},
        {"n": 3, "name": "llms.txt", **check_llms(base)},
        {"n": 4, "name": "структурированные данные", **check_jsonld(html)},
        {"n": 5, "name": "мета-теги и язык", **check_meta(html)},
        {"n": 6, "name": "счётчик один на страницу", **check_counters(html)},
        {"n": 7, "name": "событийные цели", **check_goals(html)},
        {"n": 8, "name": "https и сертификат", **check_tls(url)},
        {"n": 9, "name": "www и apex согласованы", **check_host_agreement(url)},
        {"n": 10, "name": "лента материалов", **check_feed(base, html)},
    ]
    ok = not any(i["state"] == "fail" and i["n"] in REQUIRED for i in items)
    return items, ok


def report(url: str, items: list[dict], ok: bool) -> None:
    print("видимость сайта · %s" % url)
    print("=" * 74)
    for i in items:
        print("%2s  %-34s %-10s %s" % (i["n"], i["name"][:34],
                                       MARK.get(i["state"], i["state"]), i["why"]))
    print("=" * 74)
    failed = [i["n"] for i in items if i["state"] == "fail"]
    warned = [i["n"] for i in items if i["state"] == "warn"]
    if ok and not warned:
        print("Итог: всё прошло.")
    elif ok:
        print("Итог: обязательные пункты прошли, с замечаниями по пунктам %s."
              % ", ".join(map(str, warned)))
    else:
        print("Итог: не прошли пункты %s." % ", ".join(map(str, failed)))


# --------------------------------------------------------------- самопроверка

ROBOTS_OK = """
User-agent: *
Disallow: /admin/

User-agent: GPTBot
User-agent: ClaudeBot
Allow: /

Sitemap: https://example.com/sitemap.xml
"""

ROBOTS_CLOSED = """
User-agent: *
Allow: /

User-agent: GPTBot
Disallow: /
"""

HTML_OK = """<!doctype html><html lang="ru"><head>
<title>Пример</title>
<meta name="description" content="Описание страницы">
<link rel="canonical" href="https://example.com/">
<meta property="og:title" content="Пример">
<meta name="twitter:card" content="summary">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization","name":"Пример"}
</script>
<script src="https://mc.yandex.ru/metrika/tag.js?id=12345678"></script>
</head><body>ym(12345678,'reachGoal','order')</body></html>"""


def selftest() -> int:
    bad = 0

    def check(name: str, got, want) -> None:
        nonlocal bad
        good = got == want
        bad += 0 if good else 1
        print("  %-52s %s" % (name, "ок" if good else "ПЛОХО: %r" % (got,)))

    print("Разбор robots.txt:")
    groups, sitemaps = parse_robots(ROBOTS_OK)
    check("две строки User-agent подряд дают одну группу",
          allowed(groups, "ClaudeBot"), True)
    check("звёздочка пускает на корень", allowed(groups, "Bingbot"), True)
    check("закрытый подраздел не влияет на корень",
          allowed(groups, "Bingbot", "/"), True)
    check("закрытый подраздел виден как закрытый",
          allowed(groups, "Bingbot", "/admin/x"), False)
    check("строка Sitemap найдена", len(sitemaps), 1)

    closed, _ = parse_robots(ROBOTS_CLOSED)
    check("своя группа агента сильнее звёздочки",
          allowed(closed, "GPTBot"), False)
    check("остальных звёздочка пускает", allowed(closed, "PerplexityBot"), True)

    print("Разбор страницы:")
    types, broken = jsonld_types(HTML_OK)
    check("тип разметки найден", types, ["Organization"])
    check("неразобравшихся блоков нет", broken, 0)
    check("мета-теги полные", check_meta(HTML_OK)["state"], "pass")
    check("счётчик один", check_counters(HTML_OK)["state"], "pass")
    check("цели в коде найдены", check_goals(HTML_OK)["state"], "pass")

    doubled = HTML_OK.replace("</head>",
                              '<script src="https://mc.yandex.ru/metrika/'
                              'tag.js?id=12345678"></script></head>')
    check("дубль счётчика замечен", check_counters(doubled)["state"], "fail")

    two_canon = HTML_OK.replace(
        "</head>", '<link rel="canonical" href="https://example.com/?x=1"></head>')
    check("два canonical замечены", check_meta(two_canon)["state"], "fail")
    check("в причине названо задвоение",
          "задвоено" in check_meta(two_canon)["why"], True)
    two_descr = HTML_OK.replace(
        "</head>", '<meta name="description" content="Другое описание"></head>')
    check("два описания замечены", check_meta(two_descr)["state"], "fail")
    check("пустое описание считается отсутствующим",
          "описание" in check_meta(
              HTML_OK.replace('content="Описание страницы"', 'content=" "'))["why"], True)

    # Так счётчик ставят плагины готовых систем управления сайтом: скрипт без
    # параметров, номер строкой в init. Этот случай проверка когда-то не узнала
    # и сообщила «счётчика нет» на странице, где он работал.
    plugin_style = """<script src="https://mc.yandex.ru/metrika/tag.js"></script>
    <script>ym("12345678", "init", {clickmap:true});</script>"""
    check("счётчик с номером в кавычках узнан",
          check_counters(plugin_style)["state"], "pass")
    check("диспетчер тегов без идентификатора замечен",
          check_counters(HTML_OK + '<script src="https://www.googletagmanager.com'
                                   '/gtm.js?id="></script>')["state"], "fail")
    check("отсутствие разметки замечено",
          check_jsonld("<html><body>нет</body></html>")["state"], "fail")

    print()
    print("Итог: всё зелёное" if not bad else "Итог: плохо в %d проверках" % bad)
    return 1 if bad else 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if "--selftest" in flags:
        return selftest()
    if not args:
        print(__doc__.strip())
        return 2
    items, ok = run(args[0])
    if "--json" in flags:
        print(json.dumps({"url": args[0], "ok": ok, "items": items},
                         ensure_ascii=False, indent=2))
    else:
        report(args[0], items, ok)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                        # noqa: BLE001
        pass
    sys.exit(main())
