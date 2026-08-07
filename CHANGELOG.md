# Changelog

Ένα entry ανά ουσιαστική αλλαγή/εύρημα — ώστε να μη χάνεται τίποτα σε memory gap μεταξύ sessions. Βλ. και `ATHENA.md` (κανόνες) και `docs/` (βαθύτερα τεχνικά σημειώματα).

## 2026-08-07

- **Wake word: Jarvis (Porcupine, English-only) → πάντα-ενεργό Ελληνικό "Αθηνά"** (`889d04c`). Το Porcupine δεν υποστηρίζει Ελληνικά· αντικαταστάθηκε με το ήδη υπάρχον satellite protocol (`app/satellite.py`, faster-whisper, wake phrase ήδη default "Αθηνά") συνδεδεμένο τώρα και στο browser tab μέσω νέου cookie-auth path στο `/ws/voice/satellite`. Το 👂 στο `/graph` ξεκινάει να ακούει αυτόματα στο load — όχι πια κουμπί για arm. Vendor bundles (`porcupine-web.js`, `web-voice-processor.js`) αφαιρέθηκαν.
- **Κανόνας τεκμηρίωσης deployment** (`f64a7d7`): προστέθηκε ρητός κανόνας στο `ATHENA.md` — ποτέ "live/deployed" χωρίς SSH επαλήθευση (`docker inspect` revision label + `/health`). Καταγράφηκε ως [issue #7](https://github.com/tommydee1978gr/Athena-/issues/7) το περιστατικό που το προκάλεσε (live container 9 commits πίσω, CI runner-acquisition-failure).
- **Πλήρης χάρτης αρχιτεκτονικής** — διάβασμα ολόκληρου του `app/` (6.7k γραμμές) και artifact με διαγράμματα (ask() tool-loop, confirmation tiers, permission model, integrations, schema): https://claude.ai/code/artifact/babc3f30-2eaa-401d-8b3b-80c9f040bb8d
- **CLIProxyAPI runtime investigation ξεκίνησε** — βλ. `docs/cliproxy-runtime.md`. Επιβεβαιώθηκε ζωντανά (`/v0/management/config`): `transient-error-cooldown-seconds: 0`, `max-retry-credentials: 0`, `max-retry-interval: 0` — δηλαδή ουσιαστικά κανένα cooldown/backoff σε transient errors. Καμία από τις 4 οικογένειες δεν χρησιμοποιεί `*-api-key` blocks (όλες OAuth/subscription, σύμφωνα με τον κανόνα). Σε εξέλιξη: πού ρυθμίζεται το `thinking` level για OAuth-authenticated (όχι api-key) Claude entries.
