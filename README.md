# geo-llm-visibility-check

**English** · [Русский](README.ru.md)

Ten checks that tell you whether a site is visible to search engines **and to
language models**. One Python file, zero third-party libraries, one command.

```
$ python geo_check.py https://example.com

видимость сайта · https://example.com
==========================================================================
 1  доступ ботам моделей и поисковиков прошёл     агентов пропускается 15 из 15, поимённо указано 8
 2  карта сайта                        прошёл     адресов 5, из них с датой правки 5
 3  llms.txt                           прошёл     2697 знаков, строк 49
 4  структурированные данные           НЕ ПРОШЁЛ  структурированных данных нет
 5  мета-теги и язык                   НЕ ПРОШЁЛ  не хватает: описание, canonical, Open Graph
 6  счётчик один на страницу           прошёл     счётчиков по одному: Метрика 12345678
 7  событийные цели                    прошёл     вызовов целей в коде: 1
 8  https и сертификат                 прошёл     действует ещё 40 дн, выдан Let's Encrypt
 9  www и apex согласованы             прошёл     www.example.com сводится к example.com
10  лента материалов                   прошёл     лента найдена по адресу /rss.xml
==========================================================================
Итог: не прошли пункты 4, 5.
```

Output is in Russian; `--json` gives machine-readable results for any pipeline.

---

## Who this is for

**Marketing and SEO.** Everyone now talks about being visible to language
models, and almost nobody has a checklist you can actually run. This is that
checklist, as a command.

**Content teams.** It tells you whether your article pages carry structured
data and whether your feed is discoverable — the two things that decide whether
your material gets quoted rather than ignored.

**E-commerce and brand sites.** The checks that silently cost money: a doubled
analytics counter inflating your numbers, a missing canonical splitting your
pages, www and apex living as two separate sites.

**Anyone running a fleet of sites.** Run it over all of them in a loop and the
pattern shows up immediately. In our own run across six live brand sites, five
of them were missing something on the **home page** while every inner page was
complete — which told us the problem was in the template, not in the content.

---

## Why "and to language models"

Classic SEO advice assumes a crawler that indexes pages and ranks them. Model
crawlers behave differently and they are gated differently:

- They identify themselves by **their own names**. A site that closes `*` and
  opens a few named agents will quietly exclude every agent it forgot to name,
  and a group for one agent **overrides** the wildcard group entirely — that is
  what the standard says, and it is the single most common way to block a model
  by accident.
- They reward **explicit structure**: structured data, a stated canonical, a
  short plain-text summary of the site in `llms.txt`.
- They have no patience for a page whose meaning has to be inferred from the
  layout.

So the checks here are the intersection: everything that helps a search engine,
plus the few things that specifically decide whether a model can read you at
all.

---

## The ten checks

| # | Check | Passes when | Why it matters |
|---|---|---|---|
| 1 | Crawler access | `robots.txt` lets through 12 named model agents and 3 search agents, and declares a `Sitemap:` line | A named group overrides the wildcard. One forgotten name is one crawler locked out |
| 2 | Sitemap | `/sitemap.xml` answers and lists URLs (a sitemap index counts) | Without it a crawler finds only what it can reach by following links |
| 3 | `llms.txt` | File exists and has real content, not a stub | A short plain-text description of the site, for models. Cheap to write, and nothing else gives a model the same summary |
| 4 | Structured data | At least one valid JSON-LD block of a useful type | This is where you *state* what the page is instead of hoping it is inferred. An unparseable block is a fail, not a pass — it is silently ignored in production |
| 5 | Meta and language | `title`, description, `canonical`, Open Graph, Twitter card, `lang` on `<html>` — each present **and not duplicated** | Open Graph is the preview when a link is pasted into a chat. Without it, a shared link is a bare URL. Two different `canonical` tags are worse than none: the page looks configured and the search engine picks one for you |
| 6 | One counter per page | No analytics id initialised twice, and no tag manager with an empty container id | A duplicated counter does not break anything visible, so it survives for months while double-counting visits and bounce rate |
| 7 | Event goals | At least one goal call in the page code | Without calls in the code, only URL-based goals work in the analytics account: no button, no form, no download is ever counted. Reported as a remark, not a failure |
| 8 | HTTPS and certificate | Valid chain, more than 14 days left | 14 days is the point where automated renewal has already had several attempts and failed |
| 9 | www and apex agree | One redirects to the other | Two live copies of a site split link equity and split your statistics |
| 10 | Content feed | RSS/Atom declared in the markup or answering at a usual path | How aggregators and models subscribe to what you publish. A remark, not a failure |

Points 7 and 10 are remarks: they do not affect the exit code.

---

## Install and run

Requires **Python 3.9+** and nothing else.

```bash
curl -O https://raw.githubusercontent.com/vladimirsave/geo-llm-visibility-check/main/geo_check.py
python geo_check.py https://example.com
```

Options:

| Command | What it does |
|---|---|
| `python geo_check.py <url>` | The table above |
| `python geo_check.py <url> --json` | Same result as JSON |
| `python geo_check.py --selftest` | 18 offline checks of the parsers. No network, no side effects |

Exit code: `0` when every required check passed, `1` otherwise. That makes it
usable as a gate in CI:

```bash
python geo_check.py https://staging.example.com || exit 1
```

Over a list of sites:

```bash
for host in example.com shop.example.com blog.example.com; do
  python geo_check.py "https://$host" --json >> report.jsonl
done
```

---

## What it does on purpose

**Reads only.** It issues plain GET requests for the page, `robots.txt`,
`sitemap.xml`, `llms.txt` and possibly a feed. It changes nothing, submits
nothing and stores nothing.

**Says why, not just whether.** Every line carries the reason: how many agents
are let through, how many URLs the sitemap holds, which counter is doubled,
how many days the certificate has left. A bare "FAIL" tells you nothing about
what to fix.

**Decompresses whatever arrives.** Some servers behind a CDN return gzip even
when it was not requested. Until that was handled, the checker saw binary noise
and reported that a page had no title and no meta tags at all — that is, it
lied. It now unpacks by fact, not by request.

**Parses `robots.txt` the way the standard says.** Consecutive `User-agent`
lines form one group; the longest matching prefix wins; `Allow` beats `Disallow`
at equal length; an agent's own group replaces the wildcard group rather than
adding to it. Each of those is a real-world way to misread a file.

---

## What it does not do

- It does not render JavaScript. Markup injected on the client is invisible to
  it — as it is to most crawlers, which is rather the point.
- It does not validate structured data against schema.org. It checks that the
  JSON parses and that the types are meaningful. For full validation use the
  search engines' own validators.
- It does not crawl. One URL per run, by design: you choose which pages matter
  and loop over them yourself.
- It does not judge your content. No check here says whether the text is good.

---

## The checklist behind it

The ten checks are what was left after running the same acceptance over a set
of live brand sites for several months. Three of them exist because of specific
losses:

- **One counter per page** — a duplicated analytics tag survived for weeks and
  quietly doubled the traffic numbers a decision was then made on.
- **Event goals** — goals existed in the analytics account, so the dashboard
  looked configured, while the page code had no calls at all and nothing but
  page views was ever counted.
- **Structured data must parse** — a block with a trailing comma is ignored
  without a word. The page looks marked up and is not.
- **Duplication counts as much as absence** — the check originally only looked
  for presence. A live site turned out to have two different `canonical` tags
  and two different descriptions on one page, and the check let it through: the
  tag was, after all, there.

And one caveat from the same review, about the tool itself: a Metrika counter id
appears in page code three ways — in the script URL, in an `init` call as a
number, and in an `init` call as a string. CMS plugins use the last one. While
only the first was handled, the check reported "no counter" on pages where one
was installed and working. If you extend it for your own analytics, count every
way it gets wired up in your stack — otherwise you get a false alarm where
everything is fine.

---

## License

MIT. Use it.
