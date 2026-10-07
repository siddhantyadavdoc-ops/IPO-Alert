# IPO last-day alert (WhatsApp + Telegram)

Every weekday at about **7:50 AM IST**, this bot checks which Indian IPOs close for subscription **today**. If an IPO's grey-market premium (GMP) is **20% or more** of its issue price, it sends you a WhatsApp and/or Telegram message with the GMP maths and a three-year snapshot of the company's restated financials. Days with nothing qualifying stay silent.

Set up WhatsApp, Telegram, or both. If both are set up, you get every alert on both.

It runs free on GitHub Actions, so your phone or laptop doesn't need to be on.

## What an alert looks like

Illustrative numbers:

```
🔔 Last day: Example Technologies IPO
SME · ₹52.70 Cr
Example Technologies sells IT infrastructure monitoring software to enterprises.

📈 GMP ₹40 (+47.1%) on ₹85 → est. listing ≈ ₹125
Lot 1,600 shares = ₹1,36,000 · GMP per lot ≈ ₹64,000
Subscribed so far 1.70x · GMP as of 4-Oct 21:15
⏰ Closes today, 5 PM (many brokers stop earlier) · lists 8 Oct

📊 Financials (restated)
₹ Cr           FY24   FY25   FY26
Total income   45.6   57.8   65.9
EBITDA         17.4   22.6   23.3
PAT            10.8   14.1   13.1
PAT margin    23.7%  24.4%  19.9%
Net worth      32.2   47.4   60.6
Total income CAGR +20% · PAT CAGR +10% (FY24→FY26)
RoNW ≈22% (FY26)
⚠️ PAT fell 7% in FY26
💰 Mcap ₹196.5 Cr · P/E 11.0x pre / 15.0x post-issue

IPO details · GMP trend
GMP is unofficial and can move fast. Not a promise of listing gains.
```

The financials block shows revenue (or total income), EBITDA, profit after tax, PAT margin, net worth and borrowings for the last three financial years, plus growth rates, debt/equity and return on net worth. Small-company figures published in ₹ lakh are converted to ₹ crore. If the company has disclosed a more recent part-year period, that is shown on its own line. It flags loss years, a fall in revenue or profit, a sudden profit jump (worth checking for one-off gains) and negative net worth.

## Setup (about 15 minutes, best done on a computer)

You need a free GitHub account (github.com) and your phone.

### Step 1 — WhatsApp key (2 minutes, on your phone)

WhatsApp messages are sent through **CallMeBot**, a free service for personal notifications.

1. Open the CallMeBot WhatsApp page: https://www.callmebot.com/blog/free-api-whatsapp-messages/ and note the bot's current phone number (it changes from time to time, so always take it from that page).
2. Save that number in your phone's contacts under any name, e.g. "CallMeBot".
3. In WhatsApp, send that contact exactly: `I allow callmebot to send me messages`
4. Within a couple of minutes it replies "API Activated for your phone number. Your APIKEY is 123456". Note the number — that's your **API key**. (No reply? Try again after 24 hours, as the page suggests.)
5. Your **phone** for the bot is your WhatsApp number in international format, no spaces: `+919876543210`.

### Step 2 (optional) — Telegram bot

Skip this if WhatsApp alone is enough.

1. In Telegram, open **@BotFather**, send `/newbot`, and pick a name and a username ending in `bot`. Copy the token it gives you.
2. Open your new bot and tap **Start**, then send it "hi".
3. In a browser, open `https://api.telegram.org/bot<TOKEN>/getUpdates` (your token in place of `<TOKEN>`) and find `"chat":{"id":123456789` — that number is your chat ID.

### Step 3 — Put the code on GitHub

1. On github.com, click **+ → New repository**. Give it any name (e.g. `ipo-alert`), choose **Private**, and click **Create repository**. (Private matters: GitHub switches off scheduled jobs in public repos after 60 days without activity.)
2. On the new repo's page, click **uploading an existing file**.
3. Unzip `ipo-alert.zip` on your computer and open the `ipo-alert` folder. Drag its **contents** (not the folder itself) into the upload box, including the hidden `.github` folder. On a Mac, press **Cmd + Shift + .** in Finder to see hidden folders.
4. Click **Commit changes**.
5. Check that the repo shows `.github/workflows/ipo-alert.yml`. If `.github` is missing, click **Add file → Create new file**, type `.github/workflows/ipo-alert.yml` as the name (the slashes create the folders), paste in that file's contents, and commit.

### Step 4 — Add your secrets

In the repo: **Settings → Secrets and variables → Actions → New repository secret**. Add each of these as a separate secret (name exactly as shown):

| Name | Value |
|---|---|
| `WHATSAPP_PHONE` | your WhatsApp number, e.g. `+919876543210` |
| `CALLMEBOT_APIKEY` | the API key from Step 1 |
| `TELEGRAM_BOT_TOKEN` | *(optional)* the BotFather token |
| `TELEGRAM_CHAT_ID` | *(optional)* the chat ID number |

### Step 5 — Send yourself a test

1. Click the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
2. Click **IPO last-day alert** in the left list, then **Run workflow** on the right. Leave "Send a sample alert now" ticked and press the green **Run workflow** button.
3. Within a minute or two you should get a list of the IPOs open right now (✅ marks those above your GMP threshold) and a sample brief for the one with the highest GMP.
4. If nothing arrives, click the run, then the **alert** job, and see Troubleshooting below.

That's it. From now on it runs by itself every weekday morning; you don't need to do anything.

## Settings

Edit `.github/workflows/ipo-alert.yml` on GitHub (pencil icon, then **Commit changes**).

| Setting | Default | What it does |
|---|---|---|
| `GMP_THRESHOLD_PCT` | `"20"` | Alert when GMP ÷ issue price is at least this percentage |
| `INCLUDE_SME` | `"true"` | `"false"` limits alerts to mainboard IPOs. SME alerts are always labelled "SME" |
| `cron` | `"20 2 * * 1-5"` | Run time in **UTC** (IST is UTC + 5:30). `"20 2"` is 7:50 AM IST; `"30 1"` would be 7:00 AM IST |

To pause the bot, open **Actions → IPO last-day alert**, then the **⋯** menu → **Disable workflow**.

## How it works

1. Reads the live GMP table that InvestorGain's website loads (issue price, GMP, lot size, dates, subscription).
2. Keeps IPOs whose last bidding day is today in IST and whose GMP is at or above the threshold.
3. Finds each company's IPO page on Chittorgarh and reads the restated financials, valuation (P/E, market cap) and the "About the company" paragraph.
4. Sends one message per qualifying IPO, highest GMP first, to each channel you've set up. If one channel fails, the other still gets the alert and the run is marked failed so GitHub emails you.

The bot makes a handful of light requests once a day. Neither site offers an official API, so if one changes its format the bot sends you a "couldn't check today's IPOs" warning rather than failing silently. If only the financials can't be read, the alert still arrives with a link to the IPO page.

## Troubleshooting

Open **Actions**, click the latest run, then the **alert** job to see its log. If a run fails outright, GitHub normally emails you about it.

| What you see | Likely cause |
|---|---|
| `WhatsApp (CallMeBot) failed … APIKey is invalid` | Wrong key, or the phone secret doesn't match the number you activated. Use the `+91…` format |
| `WhatsApp (CallMeBot) failed` with no clear reason | CallMeBot is a free service and is occasionally slow or down. One-off: ignore. Repeated: re-do Step 1, or add Telegram as a backup |
| `Telegram returned 401` or `404` | Token is wrong or has a stray space. Re-copy it into the secret |
| `chat not found` or `bot can't initiate conversation` | Wrong chat ID, or you haven't tapped **Start** in the bot |
| `add repository secrets for at least one channel` | The secrets are missing or misnamed |
| "IPO alert couldn't check today's IPOs" in Telegram | A data source was down or changed. One-off: ignore it. Repeated: the feed needs updating |
| "Financials: couldn't read them automatically" | That company's page has an unusual layout. Use the IPO details link |
| Message arrives late, or (rarely) not at all | GitHub can delay scheduled runs at busy times and occasionally drops one. Busiest is on the hour, which is why it runs at :20 past |
| Scheduled runs stopped | Check the repo is private, and that the workflow isn't disabled under **Actions** |

## Running it on your own computer

```
pip install -r requirements.txt
python ipo_alert.py --test --dry-run          # print a sample instead of sending it
python ipo_alert.py --date 2026-10-05 --dry-run   # what would be sent on that morning
```

Without any WhatsApp or Telegram environment variables set, it prints messages instead of sending them.

## Caveats

CallMeBot is free for personal use and run by a third party, so your alerts pass through their server and delivery isn't guaranteed. Telegram's bot API is the more dependable of the two, which is why it's worth setting up both. (WhatsApp's official Business API is an option too, but needs a Meta business account and approved message templates.)

GMP is an unofficial grey-market indication. It can swing sharply in the last day or two and is not a reliable predictor of listing price. Figures come from third-party websites and can be late or wrong, so check the RHP before applying. This tool is for information only and is not investment advice.
