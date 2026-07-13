# kya-banao build briefs

Flagship track in the portfolio 10x plan. Goal: the maximal PERSONAL tool for this
household (Kriti + Navneet + part-time cook). Depth, not users.

Full design + rationale:
`~/.gstack/projects/kritisharma2708-kya-banao/kritisharma-main-design-20260713-231619.md`

## The 10x frame

Remy stops being a planner you query and becomes a system that runs the kitchen a
day or two ahead. Engine is a loop: Observe -> Predict -> Prep-ahead -> Plan ->
Execute -> Learn. Control surface is an autopilot dial (L1 suggest ... L6 optimize)
you turn up as Remy earns trust. Today Remy sits at L1-L2.

Reject the venture/CFO framing. Nutrition and waste show up only as kitchen delight,
never as dashboards. The no-checkout trust boundary stays until you raise the dial.

---

## Build Brief #1 — Nightly look-ahead prep + deciding morning brief

Reprioritized ahead of vision-pantry after a real-use insight: Remy told Kriti to
make rajma in the morning, but rajma needs overnight soaking, so the instruction was
already too late. Look-ahead prep is the missing capability, and it delivers daily
delight faster than vision, with no new infrastructure.

**Two coupled capabilities:**

1. Look-ahead prep (the rajma fix). Each night (~9PM), Remy reads tomorrow's planned
   meals, detects lead-time prep (soak, marinate, thaw, ferment/set curd), and nudges
   tonight in her voice: "Soak the rajma before bed, we're making it tomorrow."
2. Deciding morning brief. Upgrade the 8AM reminder from "here are options" to
   "here's the day": breakfast / lunch (cook) / dinner, with at least one piece of
   proactive reasoning (weather, calendar, cook-off, or known expiry). Stays at
   suggest/stage autonomy. No auto-order.

**Ground truth (don't rediscover):**
- Real entry is `bot.py`, not `main.py` (dead stub). Weekly plan already writes a
  `meals` table (Sunday 6PM). 8AM menu + 9AM cadence crons already exist in job-queue.
- Sonnet-4-6 via AsyncAnthropic tool loop in `llm.py`. Brain split soul.md + user.md
  + live SQLite as cached system blocks. Voice enforced by post-gen regex.

**Acceptance criteria:**
1. Tomorrow's plan contains a soak/marinate/thaw dish -> tonight ~9PM Remy sends a
   prep nudge, in voice.
2. Morning brief reads as a decided day (not a menu list) and names >=1 proactive
   reason.
3. Prep detection covers the common lead-time classes: soaking (rajma/chole/chana),
   marinating, thawing frozen, fermenting/setting (dosa batter, curd). Reliable, not
   hallucinated per-run.
4. Voice rules hold (no em-dash/asterisk, persona intact).
5. Autonomy stays at suggest/stage. No auto-checkout.

**Decisions (locked 2026-07-13):**
1. Prep source: curated `prep_rules` table (soak/marinate/thaw/ferment for common
   dishes), LLM fallback for dishes not in the table.
2. Nightly nudge: fixed 9PM. Learn bedtime later.
3. Morning-brief coupling: brief stays consistent with last night's prep, AND handles
   the miss: if the prep wasn't done (user says "didn't soak" or never confirmed),
   Remy re-plans — suggests a no-prep alternative and shifts the prep-dependent dish
   to the day after (soak tonight instead). This means prep is tracked as STATE
   (nudged / confirmed / missed), not a fire-and-forget message.

**First step in the repo:** open `bot.py` + `llm.py` + the weekly-plan generator;
confirm how `meals` stores the plan and how crons register; add prep tagging at plan
time + a `prep_rules` fallback; add the ~9PM look-ahead cron; upgrade the 8AM brief.

---

## Build Brief #2 — Vision-based pantry ("the kitchen it truly knows")

The Observe step of the loop. Moves pantry from inferred (guessed from order history)
to known (extracted from a photo). Makes the look-ahead and the morning brief
trustworthy because they're grounded in real inventory + expiry.

Photograph the fridge/pantry -> Claude vision extracts a structured item + quantity
list -> reconciles against inferred pantry into a known-state `pantry` table (item,
qty, last_seen) per `chat_id` -> planner + cadence nudge + look-ahead prefer known
state, fall back to inference when absent.

Constraints: trust boundary stays (stage, never checkout); honest tool-result
contract (report what it couldn't identify); no barcode scanning; one household first.

Open questions: does vision extract quantities reliably or capture presence only and
let you correct? When vision conflicts with order history (vision says no milk,
history says bought 2 days ago), does vision win?

---

## Later (increment #3+)

- Household Memory / taste-and-mood brain (Learn step): running household journal,
  learns patterns like "mushrooms on Mondays", "biryani after stress". The moat.
- Cook loop (Execute, ladder L5): Remy briefs the cook daily from known inventory +
  plan, tracks what got made, learns.
- Raise the autopilot dial (L3-L4) when trust is earned.
