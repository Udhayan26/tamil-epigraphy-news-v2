# Tamil Epigraphy & Archaeology News Collector

Automatically collects news articles (with URLs) about **Tamil inscriptions,
hero stones, rock paintings and archaeology**, from Indian and international
websites, going back about 20 years, and keeps the collection updated daily.

**தமிழ் கல்வெட்டு, நடுகல், பாறை ஓவியம், தொல்லியல்** தொடர்பான செய்திகளை
இந்திய மற்றும் வெளிநாட்டு இணையதளங்களிலிருந்து (கடந்த ~20 ஆண்டுகள்) தானாகச் சேகரிக்கும் கருவி.

## Where are the results? / முடிவுகள் எங்கே?

| File | What it is |
|---|---|
| [`data/articles.xlsx`](data/articles.xlsx) | All articles (Excel). Click it, then **Download** (⬇) |
| [`data/articles.csv`](data/articles.csv) | Same data as CSV (opens in Excel / Google Sheets) |
| [`data/daily/`](data/daily) | One CSV per day with only that day's **new** articles |
| [`data/status.txt`](data/status.txt) | Progress report: new today, last 7 days, history progress |
| `data/collector.sqlite` | Internal database (progress + every link checked) |

The Excel file has these sheets: **Articles** (everything), **New (last 7 days)**, and
counts **By keyword**, **By year**, **By source** and **By place**.

Columns: Date · Year · Title · Keyword group (Tamil/English) · Matched terms ·
Places mentioned · Source · Source country · Language · **URL** · Archive copy ·
Snippet · Match basis · Found via · Found on.

* **Match basis = full-text**: the page was opened and the keywords were found in the article.
* **Match basis = title-only**: the page could not be opened, but the headline matched.
* **Archive copy**: Wayback Machine link when the original page is gone.
* A date starting with **≤** means the exact date is unknown; the article existed by that date.
* **Found on**: the day this collector first found the article.

## How it works

Every 6 hours (05:30, 11:30, 17:30, 23:30 IST) GitHub Actions runs one job:

1. **Today's news**: Google News (Tamil and English editions, ~55 search phrases
   such as `நடுகல் கண்டுபிடிப்பு`, `சோழர் கால கல்வெட்டு`, `"hero stone" Tamil`)
   and GDELT (worldwide). These are checked first.
2. **20-year history**, continuing where the last run stopped:
   * **Wayback Machine URL index** of ~55 news sites (The Hindu, New Indian Express,
     DT Next, Times of India, Dinamani, Dinamalar, Dinakaran, Daily Thanthi,
     Maalaimalar, Vikatan, Nakkheeran, Hindu Tamil, Sri Lankan, Malaysian,
     Singapore, Egypt and Oman papers, archaeology news sites).
   * **The Hindu print archive** headline lists since 2006.
   * **GDELT** month by month since 2017.
3. Every link is opened and kept only if the keywords really appear **and** it has a
   Tamil connection (Tamil Nadu place, Chola/Pandya/Pallava, Tamil script, Sri Lanka
   Tamil sites, etc.).

When the history is complete, runs only do step 1 (about 15 minutes).
To start a run by hand: **Actions** tab → **Collect news** → **Run workflow**.

## Changing keywords or websites

* Keywords: edit [`config/keywords.yaml`](config/keywords.yaml) (Tamil and English
  lists per group, plus search queries).
* Websites: edit [`config/sources.yaml`](config/sources.yaml).
* Extra URLs to check: add them to [`config/seed_urls.txt`](config/seed_urls.txt).

Edit the file on GitHub (pencil ✏️ icon) and commit; the next run uses it.
Note: the history collection walks through websites in order, so add new
websites at the **end** of the list.

## Running on your own computer (optional)

```bash
pip install -r requirements.txt
python -m collector status                 # progress
python -m collector backfill --minutes 60  # collect for one hour
python -m collector run --minutes 30       # same as the scheduled job
python -m collector daily                  # recent news only
python -m collector export                 # rebuild Excel/CSV
python -m pytest                           # offline tests
```

## Limits

* E-paper page images (scanned print pages) are not read.
* Paywalled pages are checked through their Wayback copy when possible.
* Web archives do not contain every article ever published, so coverage is
  broad but not 100% complete — especially for Tamil papers before ~2012.
