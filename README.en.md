# Whalecare (鲸鲸) — a self-hosted companion that actually lives on your data

**English** · [中文](README.md)

[![ci](https://github.com/DpVoliin/whalecare/actions/workflows/ci.yml/badge.svg)](https://github.com/DpVoliin/whalecare/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python 3.11+](https://img.shields.io/badge/python-3.11%2B-informational.svg)](pyproject.toml)
[![zero dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](pyproject.toml)
[![pypi](https://img.shields.io/pypi/v/whalecare.svg)](https://pypi.org/project/whalecare/)

Whalecare is a **proactive** personal agent: it watches the data you already generate
(phone screen time, sleep, schedule, PC activity, orders, battery…), keeps it in **your own
SQLite database**, and decides *by itself* when a message is actually worth sending.

It is not a chatbot waiting for you to type. It is the thing that taps you on the shoulder —
and knows when not to.

> Most docs are currently in Chinese (the project's primary language). This file is the
> English entry point; the Chinese `README.md` is the fuller one.
> Translations are welcome — see [`docs/COMMUNITY.md`](docs/COMMUNITY.md).

---

## 30 seconds: what it looks like

A real message it sent (translated):

> 🐋 *(has been watching all day)* You've been at the screen for 9 hours — go stretch. By the way,
> it'll rain tomorrow morning, 88%. And you have class at 8.

And the reason it decided to speak **right then** is logged, not guessed:

```
next gap 20 min  (daytime | rain today | social 30 min | 3 classes | parcel pending |
                  disk 1.1% free | screen 554 min | 1 anomaly vs own baseline |
                  rich material(8) → speak sooner | you just picked up the phone | said 5 today)
```

That line is the whole point of the project: **the timing is computed, not random.**

---

## Five design tradeoffs (they decide whether it's pleasant or annoying)

1. **Relative to *your* baseline, not an absolute threshold.** "6 hours of screen time" is
   normal for one person and a lot for another. All anomaly detection is median + MAD against
   your own history, with small-sample shrinkage so it stays quiet when it isn't sure yet.
2. **Silence is acceptable; being wrong is costly.** The system is tuned to under-speak:
   a utility gate (`p(accept) > cost_false / (cost_false + cost_miss)`) plus a daily cap that
   adapts to your ✓/✗ feedback.
3. **Nothing that can't run on your machine.** Runtime is **zero third-party dependencies** —
   pure Python standard library. The whole backend ships as one readable file you can audit.
4. **De-identify before the model sees anything.** `/llm-preview` shows you exactly what the
   model would receive (categories and coarse numbers), and what was stripped.
5. **Don't let it claim what it can't do.** It never pretends to perform physical actions
   ("dimming the lights"), and the text it speaks never contains stage directions.

---

## Architecture

```
collector (Android)          hub.py (your machine)          speaker               outputs
────────────────────         ─────────────────────          ───────               ───────
screen / sleep / health      SQLite  +  de-identification    4 research-backed     WeChat
calendar / music / orders →  rules   +  statistics       →  algorithms        →   desktop
battery / Bluetooth      →   reminders +  delivery queue      + Thompson           small screens
   HTTPS + cert pinning      (one file, zero deps)            bandits              (voice module)
```

Everything except the phone lives on a machine you control. The phone talks to *your* hub
over HTTPS with certificate pinning; the hub needs no account, no cloud, no telemetry.

---

## Quick start

```bash
git clone https://github.com/DpVoliin/whalecare && cd whalecare
python3 hub/hub.py            # starts on :11440, prints a masked token, creates hub.json + hub.db
```

Then open `http://127.0.0.1:11440/llm-preview` — that's what the model would see, and
`/health` for status. Install the Android collector and point it at your hub
(see [`docs/DEPLOY-GUIDE.md`](docs/DEPLOY-GUIDE.md), Chinese).

Prefer a container? `docker compose up -d` (data stays in `./data`).
Prefer pip? **`pip install whalecare && whalecare`** — published on PyPI, zero dependencies;
>
> ⚠️ **If you are on a China-based PyPI mirror** (many cloud hosts default to one):
> mirrors lag the official PyPI by **a few hours**. Seeing "no matching distribution" for the
> newest tag usually means the mirror hasn't caught up yet — not that the release is missing.
> Pin the official index to get it immediately:
> ```bash
> pip install -i https://pypi.org/simple whalecare
> ```
the package is a thin shell that just locates and runs the same single-file hub.

Optional pieces: `hubctl.py` (CLI: status/stats/dump/restore/doctor/token/…),
`speaker/` (the "decides when to talk" layer, needs an LLM API key),
`desktop/` (Windows widget), `mcu/` (LAN relay for microcontrollers).

---

## How the timing works (the interesting part)

It isn't a cron job with random delays. The speaker layer applies published research:

| Mechanism | What it does | Source |
|---|---|---|
| **Interruptibility / breakpoint delivery** | speak when you just picked up the phone, not mid-focus | Iqbal & Bailey, CHI 2007 |
| **Goldilocks time windows** | every topic has hours where it's useful; outside them, don't send | arXiv:2504.09332 |
| **Expected-utility gate** | act only if `p(accept)` beats the false-positive cost | Horvitz, CHI 1999 |
| **Bucketed Thompson sampling** | learn your acceptance per context band (morning/weekend…), requiring ≥4 samples per band before trusting it | arXiv:2608.04416 |
| **Interruption dashboard** | `hubctl interruption` → open rate, accept rate, hour histogram, last reasons | "interruption budget" literature |

Plus a "rich material" score: weather, upcoming class, low disk, low battery, unusual-relative-to-you
signals raise the score → speak sooner; nothing worth saying → back off (5–90 minute bounds, daily cap).

---

## Self-checks: it cannot go quietly silent

The dangerous failure mode of a proactive agent is not a crash — it is **failing silently**:
everything looks healthy, it just stops doing anything. On 2026-09-24 four of those happened in
one day (empty model replies, cross-day dedup, failed acks causing repeats, stale reminders
delivered at the wrong time). Each took thousands of log lines to find. So they are now guarded
by **mechanisms**, not by "remembering to check":

- **Model profiles** (`speaker/model_profile.py`) — reasoning vs non-reasoning models differ a lot:
  the token parameter name (`o1/o3/gpt-5` only accept `max_completion_tokens`), the budget
  (reasoning 2400 / plain 400), and `temperature` constraints (o-series accepts only 1).
  Unknown names are conservatively treated as reasoning models; if a response carries
  `reasoning_content` while the name says otherwise, the profile is corrected at runtime.
  On top of that the call site retries with 3× budget, then falls back to a secondary model,
  then to a template.
- **Self-check & watchdog** — on startup it verifies hub reachability, token, model and state
  directory. While running, if the API errors ≥5 times, the model returns empty ≥5 times, or it
  has said nothing for ≥6 active hours **while there was material**, she sends one self-report
  message (at most once a day; quiet hours excluded). Per-category counters cover all 11
  "decided not to speak / dropped" exits.
- **Policy regression gate** (`speaker/sim_week.py`) — replays a simulated week against the
  **real functions** (with a fake clock, otherwise the per-day ledger never resets), and treats
  "wrong-time delivery / out-of-band message rate / quiet hours broken" as invariants: it exits
  non-zero when one breaks. Runs in CI on every push.

## How often she speaks (rate tiers)

Four tiers, one command to switch, **effective within 2 minutes** (no restart):

| Tier | Daily cap | Speak / urgent threshold | Interval multiplier | Small-talk |
|---|---|---|---|---|
| `quiet` | **4** | 65 / 85 | ×2.50 | none |
| `low` | **9** | 55 / 72 | ×1.70 | none |
| `normal` (default) | **12** | 38 / 62 | ×1.00 | 1 |
| `high` | **24** | 22 / 50 | ×0.60 | 2 |

Three things people get wrong: the cap is only **one of four** things a tier changes (changing the
cap alone gives a "fake effect" — the threshold still gates you); the **urgent channel ignores the
cap** (disk nearly full, battery dying, weather warnings, class starting — those are always said);
and `quiet` adds a **time gate** on top (07–09 / 11–14 / 21–24 only) because raising a threshold
cannot control *when* something is said.

## Delivery channels (beyond WeChat)

The primary path is *speaker → gateway webhook → WeChat*. The hub also ships its own
**direct-send** channels — deliberately **never automatic** (so they can't duplicate the
speaker), meant for cron / extensions / manual calls. All are plain HTTP, no SDK, no deps.
**There is also a 10th path: the phone notification.** The collector app polls
`/pending?for=<device>` every 120 s and posts a **local notification** — local notifications are
not rate-limited by anyone, and they are private by construction. Each notification carries
**👍 / 👎** buttons that post straight to `/feedback` (the only input her Thompson sampling
learns from). It does not duplicate the WeChat path: the hub keeps **per-target** queues.

Her **art assets** are served by the hub at `/asset/<name>` (from `$WHALE_HOME/assets/`) — never
committed to any repo, so swapping art needs no rebuild; open-source users fall back to the
bundled vector placeholder.

**8 of them are text channels** (they send *words*, for humans); the **9th is a data channel**
(it sends **AI-analysed structured data**, for machines):

| Channel | Config keys | Notes |
|---|---|---|
| WeCom group bot | `channels.wecom_webhook` | official, no rate limit, one URL |
| Generic webhook | `channels.generic_webhook` | anything accepting `POST {"text": "..."}` |
| WeCom app message | `channels.wecom_corpid/secret/agentid` | can target specific members |
| **ntfy** | `channels.ntfy_url` (+`ntfy_token`) | raw-text `POST` to `https://ntfy.sh/<topic>` |
| **Bark** | `channels.bark_url` (+`bark_sound`) | iOS, path form `/<key>/<title>/<body>` |
| **DingTalk** | `channels.dingtalk_webhook` (+`dingtalk_secret`) | official custom-robot webhook, optional official signing |
| **Discord** | `channels.discord_webhook` | official webhook, `{"content": ...}` |
| **QQ** | `channels.qq_appid` + `qq_secret` + `qq_target` (+`qq_kind`) | **official bot API** — fetch `access_token`, then `POST /v2/users|groups/<id>/messages` |
| **Analysis** (data) | `channels.analysis_webhook` / `analysis_file` | sends **data, not sentences** — see below |

Check status with `hubctl channels` (reports *configured / empty* only — never prints the
webhook URL itself, since that URL is a credential). Send a test with `POST /channels?test=1`.

### The 9th channel: analysis out (data, not sentences)

The first 8 channels emit **prose**. The analysis channel emits the **result of having an AI
analyse the data** — structured JSON (values / trends / outliers / pairs / scores / tags) with
**no wording applied at all** — for a **machine** to consume: your own dashboard, a script, the
desktop widget, an MCU screen.

```
hub /llm-preview (already redacted)
   └─▶ speaker/whale_analyze.py: model is told to *compute JSON*, not to write prose
          └─▶ normalised to the spec + dry-run against the hub validator → POST /analysis
                 └─▶ hub **hard-validates** the spec: non-conforming is rejected;
                     conforming is stored + fanned out to
                        ├─ channels.analysis_webhook  (POST the JSON to your service)
                        ├─ channels.analysis_file     (atomic JSON file for widget/web/MCU)
                        └─ GET /analysis?limit=N      (pull it; works with no channel configured)
```

**The format is a spec, not a best effort**: [`docs/analysis.schema.json`](docs/analysis.schema.json)
(JSON Schema 2020-12; human version [`docs/ANALYSIS-FORMAT.md`](docs/ANALYSIS-FORMAT.md)). The hub
embeds a copy and hard-validates on `POST /analysis` — non-conforming payloads are **rejected**
(not stored, not fanned out) and audited. Helpers: `GET /analysis/schema` returns the spec itself,
`POST /analysis?validate=1` dry-runs it.

Three invariants, each enforced by a mechanism rather than by good intentions:

| Invariant | How it's enforced |
|---|---|
| **Only redacted context** | the analyser reads the same `/llm-preview` payload (no raw notifications, app names, minute-level timestamps or location) — it never opens a new data source |
| **Never invent data** | every metric `id` must exist in that context (otherwise the item is dropped and counted); values that don't match the context are flagged `unverified=true` |
| **Never write prose** | all strings must be short labels (≤24 chars, no sentence punctuation); the spec validator rejects prose, the writer feeds errors back for one rewrite, and gives up rather than sending garbage |

Run it: `python3 speaker/whale_analyze.py --now` (analyse + send), `--dry` (analyse only),
`--show -n 3`, `--schema`. Hub side: `hubctl analysis`.

> If you run this on **Hermes**, prefer its platform plugins for DingTalk / Discord / Feishu /
> WeCom / ntfy / Telegram / Slack / WhatsApp (`hermes plugins enable <name>-platform`) — those
> are the maintained, official integrations. The channels above exist so **non-Hermes
> deployments** can still deliver.


## Privacy & security (verifiable, not just claimed)

- **Raw data stays on your machine.** `privacy.store_raw_text` defaults to `false`;
  health notification text is not persisted.
- **What enters the model is a whitelist.** Coordinates, app names, titles, addresses and
  message bodies are stripped before `llm_context()`. `GET /llm-preview` lets you check.
- **GDPR-style endpoints built in:** `GET /export` (portability) and `POST /erase?confirm=…`
  (deletion, with an automatic backup first).
- **Auth:** header-only `X-Token` (query tokens rejected), 1 MB body cap, rate limiting per source.
- **Transport:** HTTPS with a self-signed cert you generate on your own machine; the Android
  collector pins it and encrypts its offline queue with the Keystore.
- **Search results are gated** (trusted domains only, harmful-word filter, always ≥2 corroborating
  results before she says anything about the outside world).

Security scanners, dependency updates, SBOM generation and OpenSSF Scorecard run in CI.
The repository contains **no third-party art assets** (see `desktop/assets/README.md`).

---

## Contributing

You don't need to understand the whole system to help. [`docs/COMMUNITY.md`](docs/COMMUNITY.md)
lists tasks with **evidence and acceptance criteria** (reproducible builds, auto-generated API
lists, English strings, doc translations, more delivery channels…), plus the versioning policy
and when 1.0 happens.

The one project-specific rule: the backend is **fragment sources → amalgamated single file**
(`hub/src/whalecare/*.py` → `hub/hub.py`), and CI enforces byte-for-byte equality —
so **change a fragment and the product together** (see [`CONTRIBUTING.md`](CONTRIBUTING.md)).

## License

MIT. Your data is yours; the code is here to be audited.

