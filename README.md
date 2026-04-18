# Japanese — A Self-Paced Project

A markdown-only Japanese learning project, organized for daily 15–20 minute lessons. Designed for someone returning to the language after years away — beginner-treatment, but it assumes you can pick up patterns quickly.

## How this is organized

```
japanese/
├── README.md                 ← you are here
├── day-01.md … day-28.md     ← daily lessons (Phase 1)
├── phase-2-outline.md        ← scaffold for the next 400 words
├── phase-2/                  ← (stubs for phase 2 reference content)
│
├── vocab/      ← reference: words & phrases by topic
├── grammar/    ← reference: grammar concepts
├── phrases/    ← reference: situational dialog templates
├── culture/    ← reference: short cultural notes
├── listening/  ← reference: pronunciation, ear training, resources
└── immersion/  ← reference: habits, reading, anime, environment, shadowing
```

The **daily files** are short and bite-sized. They're the entry point. Each day links into the topic-organized **subfolder reference files** for the deeper explanations. Treat the subfolders as your textbook; treat the daily files as your homework planner.

## How to use

1. Read **day-01.md** today. Spend 15–20 minutes. Don't skip ahead.
2. Tomorrow read **day-02.md**, do the SRS-review questions first (which point back to day 1), and continue.
3. Each lesson includes:
   - **New vocabulary** (a small handful — never overwhelming).
   - **SRS review** of items from ~1, ~3, and ~7 days prior. This is your spaced repetition; it's baked in.
   - **One grammar concept** with examples.
   - **Reading practice** — short sentences to read aloud.
   - **Speaking practice** — a quick out-loud exercise.
   - **Quiz** — 3–6 questions with answers hidden in `<details>` tags so you can self-test.
4. When you want more depth on a topic mentioned in a day-file, follow the link into `vocab/`, `grammar/`, etc.

## Phase 1 vs Phase 2

- **Phase 1** (days 1–28): The 100 most foundational words and phrases. Greetings, pronouns, 15 essential verbs, numbers 1–10, time words, directions, common nouns, particles, question words, and basic adjectives. Plus polite ~ます verb conjugation (non-past, past, negative, negative past), the copula です, は vs が, the 11 phase-1 particles, and i-adjective basics.
- **Phase 2** (planned): Expansion to ~500 total words — more verbs, more adjectives, body parts, emotions, calendar/dates/time precision, transit, work, family, weather. New grammar: て-form, ～たい (want to), potential, conditional, more counters, casual register. See [phase-2-outline.md](./phase-2-outline.md).

The full structure for phase 2 is scaffolded in `phase-2/` but not yet filled in.

## Design choices

A few decisions made up front so the lessons stay coherent:

### Kana strategy
- **Days 1–3**: progressive hiragana onboarding (full chart by day 3).
- **Days 4–6**: katakana onboarding alongside (full chart by day 6).
- **Vocabulary presentation**: kanji + kana + romaji + English in early lessons. Romaji starts dropping out around day 18; phase 2 generally drops romaji.
- Recognition before production. Don't sweat handwriting in phase 1.

### Cadence
- Target **15–20 minutes per day**. On low-energy days, 15. On high-energy days, optionally extend with listening or reading practice (see `listening/` and `immersion/`).
- **28 days** for phase 1. Aim for daily; if you skip a day, just resume — don't double up.
- **3 review-heavy days** sprinkled through phase 1 (days 7, 14, 18, 23, 28) to consolidate without piling on new vocab.

### Spaced repetition
- Each daily lesson reviews items from ~1 day, ~3 days, and ~7 days prior. No app required.
- Quizzes are short and self-graded. Hide answers under `<details>` collapsibles.
- Capstone quiz on day 28 sweeps the whole phase.

### Politeness register
- The default register taught in phase 1 is polite (~ます / です). This is the safe default for any new social context in Japan.
- Casual / plain forms get introduced in phase 2.
- Honorific (尊敬語) and humble (謙譲語) keigo are deferred beyond phase 2.

### The 100-word selection
Picked for everyday utility, in roughly this distribution:
- 12 greetings & polite phrases
- 8 pronouns & demonstratives
- 15 essential verbs
- 10 numbers (1–10)
- 6 time words
- 8 direction/location words
- 14 common nouns (food, places, people, things)
- 11 particles
- 7 question words
- 8 adjectives
- 4 counters & 円
- ~3 fillers (とても, ちょっと, 元気)

Total: ~106 (slightly over 100 because counters and the copula are useful as words too).

## Reference subfolders at a glance

- **`vocab/`** — words and phrases organized by topic, each with a stable heading anchor that daily lessons link to.
- **`grammar/`** — explanations of particles, verb conjugation, copula, adjective types, demonstratives, existence verbs.
- **`phrases/`** — situational dialog scripts (greetings, self-intro, restaurant, shopping, directions, apologies, small talk).
- **`culture/`** — politeness levels, bowing etiquette, name suffixes, food etiquette, holidays/seasons, writing systems.
- **`listening/`** — pronunciation, mora rhythm, minimum pairs, suggested audio resources.
- **`immersion/`** — daily input habits, graded readers and manga, anime-with-subs strategy, label-your-environment, shadowing.

## When you're stuck or rusty

- Re-read the relevant subfolder file. The day-files are deliberately compact; the subfolders are the source of truth.
- Re-do an old quiz. If you can still answer, you're solid. If not, that's where to spend the next session.
- Use `すみません、わかりません` shamelessly with native speakers — it's polite and saves time.

## A note on perfection

You will mispronounce things. You'll mix up は and が. You'll forget vocabulary you "just learned." That's normal. Consistency beats perfection — one bad lesson is infinitely better than a missed lesson.

頑張ってください！ — *ganbatte kudasai* — "Good luck / give it your best."

---

Start now: [day-01.md](./day-01.md)
