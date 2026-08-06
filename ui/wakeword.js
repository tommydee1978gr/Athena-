/* On-device wake word ("Jarvis") via Picovoice Porcupine — runs entirely in the
 * browser via WASM. No audio leaves the machine while just listening for the wake
 * word; only after "Jarvis" fires does the existing precise-capture flow (app.js's
 * startListening, which does send audio to ATHENA's own STT) take over.
 *
 * Silently does nothing if no AccessKey is configured (window.ATHENA_PICOVOICE_KEY
 * empty) or the browser/engine can't init — this must never break the rest of the
 * /graph page.
 */
(function () {
  const accessKey = window.ATHENA_PICOVOICE_KEY;
  const toggle = document.getElementById("wakeToggle");
  if (!toggle) return;

  if (!accessKey) {
    toggle.disabled = true;
    toggle.title = 'Ρύθμισε ένα Picovoice AccessKey στο /integrations για να ενεργοποιηθεί η αφύπνιση με "Jarvis"';
    toggle.style.opacity = "0.4";
    return;
  }

  const STORAGE_KEY = "athena_wakeword_armed";
  let porcupine = null;
  let armed = false; // user's saved preference — "should we be listening whenever idle"
  let subscribed = false; // are we actually attached to the mic right now
  let athenaBusy = false;

  function setToggleVisual() {
    toggle.classList.toggle("recording", subscribed);
    toggle.title = armed ? 'Ακούω για "Jarvis" — πάτα για απενεργοποίηση' : 'Πάτα για συνεχή ακρόαση ("Jarvis")';
  }

  async function ensurePorcupine() {
    if (porcupine) return porcupine;
    porcupine = await PorcupineWeb.PorcupineWorker.create(
      accessKey,
      PorcupineWeb.BuiltInKeyword.Jarvis,
      () => {
        // Wake word heard — stop passively listening and hand off to the real capture.
        unsubscribe().finally(() => {
          if (window.ATHENA_startListening) window.ATHENA_startListening();
        });
      }
    );
    return porcupine;
  }

  async function subscribe() {
    if (subscribed || athenaBusy || !armed) return;
    try {
      const engine = await ensurePorcupine();
      await WebVoiceProcessor.WebVoiceProcessor.subscribe(engine);
      subscribed = true;
      setToggleVisual();
    } catch (err) {
      // Mic permission denied, no HTTPS, unsupported browser, bad key, etc. — degrade
      // silently to push-to-talk rather than throwing inside a global script.
      console.warn("Wake word unavailable:", err);
      armed = false;
      localStorage.setItem(STORAGE_KEY, "0");
      setToggleVisual();
    }
  }

  async function unsubscribe() {
    if (!subscribed) return;
    subscribed = false;
    setToggleVisual();
    try {
      await WebVoiceProcessor.WebVoiceProcessor.unsubscribe(porcupine);
    } catch (err) {
      /* already torn down — fine */
    }
  }

  toggle.addEventListener("click", () => {
    armed = !armed;
    localStorage.setItem(STORAGE_KEY, armed ? "1" : "0");
    setToggleVisual();
    if (armed) subscribe();
    else unsubscribe();
  });

  window.addEventListener("athena:busy", () => {
    athenaBusy = true;
    unsubscribe();
  });
  window.addEventListener("athena:idle", () => {
    athenaBusy = false;
    subscribe();
  });

  armed = localStorage.getItem(STORAGE_KEY) === "1";
  setToggleVisual();
  if (armed) subscribe();
})();
