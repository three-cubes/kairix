# Adding entity overrides

`kairix entity suggest` uses spaCy's `en_core_web_sm` model to spot named
entities in your text. The model is fast but misses or mis-labels
specific terms — company acronyms, project codenames, regional
organisation names, anything outside its training data. Overrides give
you a per-deployment fix without touching code or retraining the model.

Closes [#166](https://github.com/three-cubes/kairix/issues/166).

## Where to put the file

```
${KAIRIX_DOCUMENT_ROOT}/04-Agent-Knowledge/_entity-overrides.md
```

This is the standard location every kairix deployment knows to look at.
If you need a different path for testing or a custom layout, set the
`KAIRIX_ENTITY_OVERRIDES_PATH` env var.

If the file doesn't exist, kairix silently uses the defaults — there's
no setup penalty for not using overrides.

## File format

One entry per markdown list item. Each line is `- "<term>": <LABEL>`
with an optional comma-separated flag tail.

```markdown
# Entity Overrides

- "YYY": ORG
- "Jane Doe": PERSON
- "bbb": ORG, case_insensitive: true
```

Two things happen per entry:

1. The term is added to the **allowlist** — if it appears anywhere in
   the input text but spaCy didn't tag it, kairix promotes it as the
   given label.
2. The term is added to the **label-override set** — if spaCy did tag
   it but with the wrong label, kairix relabels it.

So one entry covers both the "missed" and "mistyped" cases.

### Recognised labels

| Label         | Use for                                    |
|---------------|--------------------------------------------|
| `ORG`         | Companies, institutions, banks, agencies   |
| `PERSON`      | People — first + last name, single names   |
| `GPE`         | Countries, cities, states                  |
| `PRODUCT`     | Product or platform names                  |
| `WORK_OF_ART` | Books, films, named documents              |

Anything outside this set logs a warning and is skipped — kairix never
silently honours a typo'd label.

### Flags

| Flag                       | Effect                                                   |
|----------------------------|----------------------------------------------------------|
| `case_insensitive: true`   | Registers upper-, lower-, and title-case surface forms.  |

Case-sensitive is the default — proper nouns are case-sensitive in
English, and `Apple` should not match `apple`. Use the flag for
acronyms that appear in mixed case across documents.

### Comments and prose

Lines that don't start with `- ` are ignored, so you can mix prose,
headings, and entries freely:

```markdown
# Example Corp — entity overrides

## Banks
- "YYY": ORG
- "AAA": ORG

## Internal projects
- "BBB": ORG
- "CCC": ORG, case_insensitive: true
```

## Worked example

Input text:

```
ZZZ spoke with the regional lead at YYY about AAA, BBB, and CCC
```

Without overrides, spaCy catches only `ZZZ` (as `PERSON`). With the
overrides file above:

```
$ kairix entity suggest "ZZZ spoke with the regional lead at YYY about AAA, BBB, and CCC"
ZZZ        PERSON     ...
YYY        ORG        (allowlist)
AAA        ORG        (allowlist)
BBB        ORG        (allowlist)
CCC        ORG        (allowlist)
```

The role phrase `the regional lead` is correctly dropped by the
filter chain — that part of the pipeline is unchanged by overrides.

## Overrides supplement, not replace, NER

The NER model still runs on every call. Overrides only add or correct
specific terms. The chain order is:

1. spaCy NER extracts candidate entities.
2. Role-phrase filter drops things like `the regional lead`.
3. Allowlist promotes override terms that appear in the input but
   weren't caught by NER.
4. Label-override filter relabels override terms NER tagged with the
   wrong label.

Overrides win on conflict: if spaCy says `YYY` is a `PERSON` but the
override says `ORG`, you get `ORG`.

## Errors and recovery

* **Missing file** — no warning, empty overrides.
* **Malformed entry** — logs a warning naming the file and line
  number, skips the entry, keeps loading the rest of the file.
* **Unknown label** — logs a warning, skips the entry.
* **Unreadable file** (permissions, FS error) — logs a warning, falls
  back to empty overrides. `entity suggest` never blocks on this.

If you don't see your override taking effect, the warning is in the
kairix worker logs — search for `entity-overrides:`.

## Related

* [Issue #166](https://github.com/three-cubes/kairix/issues/166) — the
  dogfood report that triggered this feature.
* [`docs/agents/ADMIN-CONVERSATION.md`](../agents/ADMIN-CONVERSATION.md) —
  the agent-side script when a user reports a missing entity.
* [`docs/operations/runbooks/how-to-rebuild-entity-graph.md`](../operations/runbooks/how-to-rebuild-entity-graph.md) —
  rebuild the Neo4j entity graph after editing overrides en masse.
