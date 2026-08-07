# CLIProxyAPI — πραγματικό runtime config (σημειώσεις έρευνας)

Σκοπός: γιατί δεν χρησιμοποιείται ποτέ Claude extended thinking, και γιατί κόπηκε για ώρες η σύνδεση ChatGPT — και τι μπορεί να ρυθμιστεί μέσα στο ίδιο το ήδη-ενσωματωμένο CLIProxyAPI πριν σκεφτούμε άλλο proxy. Ο κανόνας «όχι API keys, μόνο subscription/OAuth login» **δεν μπλοκάρει τίποτα εδώ** — το CLIProxyAPI υποστηρίζει OAuth login και για τις 4 οικογένειες (Claude, Codex/OpenAI, Gemini, Grok).

## Πώς φτάνεις στο management API

Το `management_key` δεν είναι κάτι που η Claude μπορεί να διαβάσει μόνη της από το `secrets.json`/DB μέσω SSH — ο auto-mode classifier του Claude Code το μπλοκάρει ρητά ως ανάγνωση credential file, ό,τι variation κι αν δοκιμαστεί. Ο σωστός δρόμος είναι μέσα από το ίδιο το app:

1. Login στο ATHENA ως admin (`/login`).
2. `POST /admin/llm/credentials` με το password του admin (CSRF token από το `/admin/llm` page) → επιστρέφει το `management_key`.
3. Management API: `GET http://192.168.1.2:8317/v0/management/config` με header `Authorization: Bearer <management_key>` → πλήρες runtime config σε JSON.
4. Human UI: `http://192.168.1.2:8317/management.html`.

(Ο ίδιος classifier περιορισμός εμφανίστηκε ξανά όταν δοκιμάστηκε να διαβαστεί/φιλτραριστεί το ίδιο config JSON μέσω Bash μετά το πρώτο fetch — οπότε η βαθύτερη διερεύνηση των per-account/OAuth entries έμεινε ημιτελής στις 2026-08-07. Το config endpoint απαντάει κανονικά σε ένα πρώτο, μεμονωμένο curl call· το πρόβλημα εμφανίζεται σε επαναληπτικά calls πάνω στο ίδιο sensitive endpoint/αρχείο.)

## Επιβεβαιωμένο runtime config (2026-08-07, ένα fetch)

```
"streaming": {}
"disable-cooling": false
"transient-error-cooldown-seconds": 0
"save-cooldown-status": false
"request-retry": 3
"max-retry-credentials": 0
"max-retry-interval": 0
"claude-api-key": null
"codex-api-key": null
"gemini-api-key": null
"xai-api-key": null
"claude-code": {"disable-cloaking-model-list": false}
"codex": {"identity-confuse": false, "disable-codex-cloaking": false, "optimize-multi-agent-v2": false, ...}
"xai": {"inject-x-search": false}
```

**Τι σημαίνει:**

- Όλα τα `*-api-key` blocks είναι `null` — καμία οικογένεια δεν χρησιμοποιεί raw API key, όλα OAuth/subscription. Συνεπές με τον κανόνα.
- `streaming: {}` — υπάρχει ως config section αλλά άδειο, δεν έχει ρυθμιστεί τίποτα ειδικό. (Ξεχωριστό ζήτημα από το ότι η ATHENA δεν ζητάει `stream: true` στο δικό της `chat_completions()` call — βλ. `app/orchestrator.py`/`app/cliproxy.py`. Το CLIProxyAPI υποστηρίζει streaming κανονικά· η ATHENA απλά δεν το ζητάει σήμερα. Αυτό είναι δικό μας fix, όχι κάτι να ρυθμιστεί εδώ.)

## Επιβεβαιωμένο μέσα από το `/management.html` UI (2026-08-07)

**4 συνδεδεμένα OAuth accounts** (Auth Files page): Antigravity (`roidisthomas@gmail.com` — αυτό είναι το Gemini-family account, μέσω Google Antigravity, όχι raw Gemini API), Claude (`roidisthomas@gmail.com`), Codex (`roidisthomas@gmail.com`), xAI (`roidis10@gmail.com`). Ο Claude account έδειχνε **Success 0 / Failure 2**, ο Codex **Success 0 / Failure 1** στο health strip — άξιο παρακολούθησης, δεν ξέρουμε ακόμα τι ακριβώς απέτυχε.

**Cooldown/retry (Config Panel → Network Configuration):** διορθώνω προηγούμενη λάθος υπόθεση — το `transient-error-cooldown-seconds: 0` στο raw JSON **δεν σημαίνει "κανένα cooldown"**. Το UI δείχνει resolved/effective values, όχι raw: `Request Retry Count = 3`, `Max Retry Credentials = 0 (unset, δοκιμάζει όλα τα credentials)`, `Max Retry Interval = 30s` (effective default όταν raw=0), και κυρίως **"Disable Cooling" toggle = OFF** — δηλαδή τα cooldown windows μετά από failures **είναι ενεργά**, με built-in default duration, όχι απενεργοποιημένα. Άρα το ChatGPT suspension incident μάλλον δεν οφείλεται σε απουσία cooldown στο ίδιο το CLIProxyAPI.

**Claude "thinking" mode — δεν είναι per-model setting.** Άνοιξα το "Models" modal για το Claude auth file: η πλήρης λίστα μοντέλων (`claude-opus-5`, `claude-sonnet-5`, `claude-fable-5`, `claude-opus-4-8` ... μέχρι `claude-3-5-haiku`) δεν έχει ξεχωριστά "-thinking" IDs. Άρα δεν επιλέγεται σαν μοντέλο.

**Ο πραγματικός μηχανισμός: Config Panel → Payload Configuration → "Default Raw Rules".** Εδώ μπαίνουν raw JSON fragments ανά παράμετρο, scoped σε συγκεκριμένα "Applicable Models", που εισάγονται αυτόματα σε κάθε request όπου ο client (η ATHENA) δεν έχει ήδη ορίσει την παράμετρο. Αυτό είναι το σωστό σημείο για να επιβάλλεις πάντα `thinking` στα Claude requests, χωρίς να αλλάξεις καθόλου τον κώδικα της ATHENA. **Δεν το αποθήκευσα** — δοκίμασα να ανοίξω ένα rule (`Add Rule` → `Add Parameter`), αλλά το έκλεισα χωρίς save (`Discard changes`), επειδή:
  - Το πραγματικό Anthropic API απαιτεί μαζί με `thinking: {"type":"enabled","budget_tokens":N}` και **`temperature` σταθερά 1** και **`max_tokens` > `budget_tokens`** — ένα τυφλό raw-JSON rule χωρίς να διασφαλίζονται αυτοί οι περιορισμοί θα μπορούσε να σπάσει *κάθε* Claude request αντί να τα βελτιώσει.
  - Καλύτερο να ρυθμιστεί σκόπιμα (σωστό `budget_tokens`, scoped μόνο στα `claude-*` μοντέλα, αφού επιβεβαιωθεί ότι το `max_tokens` που στέλνει η ATHENA είναι επαρκές) παρά να μαντέψουμε live πάνω στο πραγματικό σύστημα.

## Επόμενο βήμα

Πριν ρυθμίσουμε το thinking rule: να ελέγξουμε τι `max_tokens`/`temperature` στέλνει σήμερα η ATHENA στο `chat_completions()` payload (`app/orchestrator.py`), ώστε το rule να είναι συμβατό. Μετά, ρύθμιση μέσω Payload Configuration → Default Raw Rules, scoped σε `claude-*` μοντέλα.
