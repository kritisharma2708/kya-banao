# Kya Banao? — Product Spec

## Overview

An AI-powered meal planning agent built on Swiggy's MCP that manages food decisions for a two-person household with a part-time cook. The agent eliminates the mental overhead of grocery planning, enables intelligent discovery of new items, and handles cook-vs-order decisions automatically — without either person needing to coordinate manually.

---

## Problem Statement

Managing meals for two people with different preferences and goals is a recurring cognitive load. The current setup (cook on weekdays, Swiggy on leave days or by mood) leads to:

- Repeated grocery items because discovery is passive (scrolling, friends)
- Kriti bearing the mental load of all grocery and food decisions
- No awareness of what's already at home when ordering
- No mechanism for the partner to input preferences without going through Kriti
- Poor variety — ordering defaults to familiar choices

---

## Users

| User | Role |
|------|------|
| Primary user (Kriti) | Manages groceries and food decisions; wants to reduce this load |
| Partner | Has different food preferences and dietary goals; currently not in the loop |

---

## Core Constraints

### 1. Two-Person Household
- Each person has an individual profile with preferences, dietary restrictions, and goals
- Meals must satisfy both people — either a shared dish or a split order
- Goals may differ (e.g., high protein for one, lighter meals for the other)

### 2. Cook Calendar
- Cook has a weekly schedule with known leave days
- On cook-off days, agent automatically switches to "order mode"
- Cook's weekly menu (when known) is factored in to avoid duplicating cuisines or dishes
- Agent handles hybrid days (cook does lunch, agent plans dinner)

### 3. Grocery & Pantry Intelligence
- Pantry state is tracked — agent knows what's already at home
- Staples (milk, eggs, atta, etc.) are auto-reordered on a set cadence
- Items nearing expiry are surfaced and built around before new items are ordered
- 1 new discovery item per week: a snack, vegetable, or fruit outside the usual list — curated, not scrolled

### 4. Discovery
- Novelty budget: at least 1 meal per week from outside the usual rotation
- Cuisine rotation: tracks the past 30 days and surfaces underexplored options
- Friend recommendations can be logged and fed into future planning cycles
- Seasonal and locally popular items surfaced proactively

### 5. Mental Load Distribution
- Partner can input preferences directly (via WhatsApp or Telegram) without routing through Kriti
- Both users receive a shared weekly plan preview every Sunday
- Either person can veto or swap a meal without Kriti being the coordinator
- Agent is proactive: surfaces decisions before they become urgent (e.g., "cook is off Thursday — want me to schedule a 7pm order?")

---

## Modes of Operation

| Mode | Trigger | Agent Behavior |
|------|---------|---------------|
| Cook mode | Cook is scheduled | Surfaces grocery list for what cook needs; no ordering |
| Order mode | Cook is on leave | Recommends restaurants, places or schedules order |
| Instamart mode | Missing ingredient / pantry gap | Orders specific items to fill the gap |
| Hybrid mode | Cook handles one meal | Plans the other meal around what's already been eaten |

---

## Ordering Parameters

| Parameter | Description |
|-----------|-------------|
| Occasion | lazy night / guests / quick lunch / post-workout / special dinner |
| Time | Preferred delivery window |
| Budget | Per-order cap; monthly food budget tracking |
| Weather | Rainy day → comfort food nudge |
| Split preference | One shared dish vs. two separate items |

---

## Individual Profiles

Each user profile stores:
- Cuisine preferences (loved / disliked / neutral)
- Dietary restrictions or allergies
- Health / nutrition goals
- Usual meal timing
- Spice tolerance, portion preference

When preferences conflict, the agent:
1. Finds overlap (same restaurant, different dishes)
2. Rotates — some nights optimized for one person's goals, some for the other
3. Flags irreconcilable conflicts for manual input rather than guessing

---

## Discovery Engine

The biggest differentiator from standard meal planners. Rather than surfacing everything and expecting the user to choose:

- **Weekly new item**: 1 curated suggestion per category (snack / produce / pantry staple) based on season, locality, and order history
- **Cuisine nudge**: if Italian hasn't appeared in 3 weeks, it gets surfaced
- **Friend recommendations**: logged via chat, queued for the next planning cycle
- **Festival/seasonal specials**: e.g., "Alphonso mangoes are in season" or "Pongal special thali available nearby"

---

## Inputs Required

| Input | Frequency | Source |
|-------|-----------|--------|
| Cook's leave schedule | Weekly / on change | Kriti or partner |
| Cook's planned menu | Weekly (optional) | Kriti |
| Pantry state | On grocery run / photo | Kriti or Instamart order history |
| Individual preferences/goals | One-time setup, updatable | Both users |
| Monthly food budget | One-time setup | Kriti |
| Occasion context | Per meal, optional | Either user via chat |
| Friend recommendations | Ad hoc | Either user via chat |

---

## Outputs

| Output | Frequency | Channel |
|--------|-----------|---------|
| Weekly meal plan | Every Sunday | WhatsApp / Telegram to both |
| Daily dinner suggestion | Evening, when relevant | WhatsApp / Telegram |
| Cook-off order prompt | Day of leave | WhatsApp / Telegram |
| Grocery list for cook | Before cook's workday | WhatsApp |
| Weekly discovery nudge | Once a week | WhatsApp / Telegram |
| Pantry reorder reminder | When staples run low | WhatsApp |

---

## What This Agent Doesn't Do (v1 Scope)

- Does not track nutrition at a granular macro level (v2)
- Does not integrate with fitness apps
- Does not manage restaurant reservations (Dineout — separate feature)
- Does not auto-place orders without confirmation (suggests + waits for approval)

---

## Open Questions

1. How does the agent learn the pantry state — manual input, photo parsing, or synced from Instamart order history?
2. How does the partner submit their preferences — separate Telegram bot session or shared one?
3. What's the fallback when Swiggy MCP doesn't have the preferred restaurant available?
4. Should the agent learn over time from accepted/rejected suggestions, or use explicit feedback?

---

## Success Metrics

| Metric | Target |
|--------|--------|
| Reduction in repeat orders (same dish 2x in 7 days) | -50% |
| New item tried per week | ≥ 1 |
| Meals where both users' goals are met | ≥ 80% |
| Times Kriti had to manually initiate food decision | ≤ 2/week |
| Partner engagement with the plan | ≥ 3 interactions/week |
