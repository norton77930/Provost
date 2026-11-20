# Feature map contract

The feature map lives in `.claude/skills/verify-<app>/features/`. It is the maintained source for what the app does from a user's point of view and how to prove it. Keep implementation details out of it: name user paths, stable handles, scenarios, commands and observable results.

## `features/README.md`

The index holds what every feature shares:

- **Baseline.** The launch command, the default scenario, the doctor command.
- **Variants.** The viewport widths, themes and locales this project verifies, how the capture command selects them, and the rule used to decide which combinations get a row (see "Which combinations to list").
- **State id format.** `<feature>-<state>-<variant values>`, lowercase with hyphens, for example `loans-overdue-390-dark`. An id never changes once a gate has used it; a retired state is deleted, not renamed.
- **Proof rules.** What every state's evidence must show (for a UI: the page identity visible, which screenshot is compared with a design source; for a CLI: the command, stdout, stderr and exit code), and which console errors or failed requests count as a defect.
- **Features.** One line per feature file with its number of states.

## One file per feature

Each file starts with an H1 title and one paragraph that describes the behavior a user sees. It then has these H2 sections in this order.

1. `How to get to it` lists every entry point a user has (a route, a menu item, a command).
2. `What the user must be able to tell or do` lists three to six plain statements about the experience, for example "the due date of the next book to return is readable without scrolling on a phone". A verifier checks each statement on the evidence. These are the place for usability expectations that a mockup comparison does not cover: what is most prominent, what feedback an action gives, how an empty or failed state explains itself.
3. `States` is the table described below.
4. `Interactions` lists the actions that change what is shown (open a dialog, switch a tab, submit a form), each with the state id that covers it, or "no state yet". Leave the section out when the feature has none.
5. `Gotchas` lists what can waste or invalidate a run: timing, data that a scenario lacks, a fixed element in a whole-page screenshot, a design source that is suspended because it predates the current app, and which states depend on a property of their scenario.

### The States table

| Column | Content |
|---|---|
| `id` | The state id. It starts every evidence file name and is the acceptance gate id. |
| `scenario` | The named data state from the skill's Scenarios table, or `(none)` when no scenario provides it. |
| variant columns | One column per variant the project verifies (for example `width`, `theme`). |
| `observable result` | What the evidence must show, written so that it can be checked without knowing the app. It names scenario values by what they are and does not quote them; the figures are in the manifest's `expected`. |
| `design source` | The path of the mockup or baseline for this state, or `none`. |
| `capturable` | `yes`, or `no` with the reason and a pointer to `Gotchas`. |

### Which combinations to list

List only combinations that differ in what the user sees.

- The main state of a feature with a design source gets a row for every width and theme the design source covers.
- The main state of a feature without a design source gets two rows: the narrowest width in the default theme, and the widest width in the other theme. Together they show every width and every theme once.
- Every other state gets one row, at the narrowest width in the default theme.
- Add a second row for another state only when the page arranges that state's content differently at the other width, such as a table that becomes cards. When in doubt, keep one row.

## The state definitions file

The capture command does not parse the table. It reads a definitions file in `scripts/` (a module or a data file in the language of the capture script) with one entry per state. The fields shared with the table are `id`, feature, `scenario`, the variant values, the design source and `capturable`; doctor and capture compare them with the table and fail on any difference.

The definitions add what only the capture needs:

| Field | Content |
|---|---|
| entry | The route or command where the state starts. |
| steps | The user actions after the entry (click a named link, choose an option, type a value). Empty when the entry alone shows the state. |
| reach conditions | What must be present or absent before the evidence is taken, as stable handles: a role with an accessible name, a label, a route. A raw selector is the last resort. |
| scenario values | Which values the capture reads from the scenario's files and looks for on the page: a name for the value, the file and field it is read from, and where on the page it should appear. They are read at run time and written into the manifest as `expected` with the value and whether it was found, never typed into the definitions. |
| preconditions | Properties of the scenario that this state depends on (the record has no history, there is exactly one week). Checked against the scenario's files before the app is driven. Only for states that other plausible data in the same scenario would turn into a different state. |
| record rule | When the state is about a record chosen by a property and not by a fixed identifier: the rule, resolved from the scenario's files at run time. The chosen record goes into the manifest. |
| focus | The element to scroll to before the viewport screenshot, when the subject is below the first screen. |
| reason | Why the state is not capturable, when it is not. |

Keep reach conditions to what tells this state from the others. "The overdue banner is present" is a reach condition. "The banner shows 3 books" is a scenario value. "The banner is red and above the list" is for the verifier to judge on the screenshot.

Matching by accessible name has traps worth a comment in the definitions file: a label that wraps its control has a longer name than its visible text, and an exact match then finds nothing.

## Example

The example is an invented library app. Use it for the shape, not the content.

```markdown
# My loans

The page lists the books a member has on loan, the due date of each, and
which ones are overdue. A member can renew a book from the list.

## How to get to it

- Open `/loans`.
- Choose "My loans" in the navigation from any page.
- Follow the link in the "due soon" email, which opens `/loans?due=soon`.

## What the user must be able to tell or do

- The next due date is the first thing read and needs no scrolling at 390px.
- An overdue book is marked in words, not by color alone.
- After a renewal the new due date is shown on the same row, and a message
  says the renewal worked.
- With no books on loan, the page says so and links to the catalogue.

## States

| id | scenario | width | theme | observable result | design source | capturable |
|---|---|---|---|---|---|---|
| loans-typical-390-light | three-loans | 390 | light | Three rows, each with a title, a due date and a "Renew" button; the earliest due date is repeated above the list | docs/design/loans-phone.png | yes |
| loans-typical-1280-light | three-loans | 1280 | light | Same content as a table with columns title, due date, action | docs/design/loans-desktop.png | yes |
| loans-overdue-390-light | one-overdue | 390 | light | A banner above the list says one book is overdue and names it; that row carries the word "Overdue" | none | yes |
| loans-empty-390-light | no-loans | 390 | light | The page says there are no books on loan and shows a link named "Browse the catalogue" | none | yes |
| loans-renewed-390-light | three-loans | 390 | light | After "Renew" on the first row: that row shows a later due date than in the before file, and a message says the book was renewed | none | yes |
| loans-renewfail-390-light | (none) | 390 | light | After "Renew" on a reserved book: a message says the book is reserved by another member; the due date is unchanged | none | no: no scenario has a reserved book, see Gotchas |

## Interactions

- Renew a book: `loans-renewed-390-light`.
- Renew a reserved book: `loans-renewfail-390-light` (not capturable yet).
- Sort by title: no state yet.

## Gotchas

- The page header is sticky. In a whole-page screenshot it must appear once,
  at the top.
- The design files show the whole page, so compare them with `<id>.full.png`.
- No scenario contains a reserved book, so `loans-renewfail-390-light`
  cannot be captured until one is added to the seed data.
- The due dates in `three-loans` are relative to the scenario's fixed clock
  (2031-03-10). The rows above quote none of them; the capture reads them
  from the seed file and records them in the manifest's `expected`.
- `loans-overdue-390-light` depends on `one-overdue` having exactly one
  overdue book. The capture checks that before it opens the page.
- `loans-renewed-390-light` changes stored data. The capture reads the new
  due date back from the loans API and records it in the manifest; cleanup
  reloads the scenario.
```
