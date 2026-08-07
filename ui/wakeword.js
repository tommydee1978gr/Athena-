/* Always-on Greek wake word ("Αθηνά") for the /graph page.
 *
 * Reuses ATHENA's own satellite protocol (app/satellite.py, over
 * /ws/voice/satellite) — the same code path a physical ESP32/Raspberry Pi
 * satellite would use, just authenticated with this browser tab's session
 * cookie instead of a device token. Detection runs server-side with the
 * local faster-whisper model already bundled with ATHENA: free, on-device
 * (same LAN, no third-party cloud), and correctly understands Greek —
 * unlike Picovoice Porcupine, which was tried first but only ships
 * built-in keywords for English/French/German/Italian/Japanese/Korean/
 * Mandarin/Portuguese/Spanish, never Greek.
 *
 * Starts listening automatically on page load — no button press required.
 * The 👂 icon is a status indicator; clicking it only mutes/unmutes.
 *
 * Trade-off vs. the old in-browser approach: audio is streamed continuously
 * to ATHENA's own server (same home network) instead of staying entirely
 * inside the browser. Silently does nothing if getUserMedia or WebSocket
 * are unavailable — this must never break the rest of the /graph page.
 */
(function () {
  const toggle = document.getElementById("wakeToggle");
  if (!toggle) return;
  if (!navigator.mediaDevices || !window.WebSocket) {
    toggle.disabled = true;
    toggle.style.opacity = "0.4";
    return;
  }

  const STORAGE_KEY = "athena_wakeword_muted";
  const TARGET_SAMPLE_RATE = 16000;
  const FRAME_MS = 200;

  let muted = localStorage.getItem(STORAGE_KEY) === "1";
  let athenaBusy = false;
  let socket = null;
  let audioCtx = null;
  let mediaStream = null;
  let workletNode = null;
  let connecting = false;

  function setToggleVisual(state) {
    toggle.classList.toggle("recording", state === "listening" || state === "capturing");
    toggle.title = muted
      ? 'Σιγασμένο — πάτα για να ακούει ξανά "Αθηνά"'
      : 'Ακούει για "Αθηνά" — πάτα για σίγαση';
  }

  // Downmixes/downsamples Float32 chunks from the mic's native rate to
  // 16kHz mono 16-bit PCM, matching what run_satellite_session expects.
  function floatTo16kPCM(float32, inputRate) {
    const ratio = inputRate / TARGET_SAMPLE_RATE;
    const outLength = Math.floor(float32.length / ratio);
    const pcm = new Int16Array(outLength);
    for (let i = 0; i < outLength; i++) {
      const sample = float32[Math.floor(i * ratio)];
      const clamped = Math.max(-1, Math.min(1, sample));
      pcm[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    }
    return pcm;
  }

  async function connect() {
    if (socket || connecting || muted || athenaBusy) return;
    connecting = true;
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const source = audioCtx.createMediaStreamSource(mediaStream);
      const processor = audioCtx.createScriptProcessor(4096, 1, 1);
      workletNode = processor;

      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(`${proto}//${location.host}/ws/voice/satellite`);
      socket.binaryType = "arraybuffer";

      let frameBuffer = new Int16Array(0);
      const samplesPerFrame = (TARGET_SAMPLE_RATE * FRAME_MS) / 1000;

      processor.onaudioprocess = (event) => {
        if (socket.readyState !== WebSocket.OPEN) return;
        const pcm = floatTo16kPCM(event.inputBuffer.getChannelData(0), audioCtx.sampleRate);
        const merged = new Int16Array(frameBuffer.length + pcm.length);
        merged.set(frameBuffer);
        merged.set(pcm, frameBuffer.length);
        frameBuffer = merged;
        while (frameBuffer.length >= samplesPerFrame) {
          socket.send(frameBuffer.slice(0, samplesPerFrame).buffer);
          frameBuffer = frameBuffer.slice(samplesPerFrame);
        }
      };
      source.connect(processor);
      processor.connect(audioCtx.destination);

      socket.onmessage = (event) => {
        if (typeof event.data !== "string") return; // audio replies are handled by app.js's own TTS path
        let payload;
        try {
          payload = JSON.parse(event.data);
        } catch (err) {
          return;
        }
        if (payload.event === "wake_detected") {
          // Hand off to the exact same precise-capture flow the mic button
          // uses (MediaRecorder + STT + ask + TTS, with local Piper
          // fallback) — the satellite socket's own command capture is
          // abandoned by disconnecting below, so the server sees silence
          // and drops it harmlessly.
          setToggleVisual("listening");
          disconnect();
          if (window.ATHENA_startListening) window.ATHENA_startListening();
        }
      };
      socket.onerror = () => disconnect();
      socket.onclose = () => {
        socket = null;
        if (!muted && !athenaBusy) setTimeout(connect, 1500); // reconnect after a drop
      };
      setToggleVisual("idle");
    } catch (err) {
      // Mic permission denied, no HTTPS on a non-localhost origin, unsupported
      // browser, etc. — degrade silently to push-to-talk only.
      console.warn("Wake word unavailable:", err);
      disconnect();
    } finally {
      connecting = false;
    }
  }

  function disconnect() {
    if (workletNode) { try { workletNode.disconnect(); } catch (err) {} workletNode = null; }
    if (audioCtx) { try { audioCtx.close(); } catch (err) {} audioCtx = null; }
    if (mediaStream) { mediaStream.getTracks().forEach((t) => t.stop()); mediaStream = null; }
    if (socket) { try { socket.close(); } catch (err) {} socket = null; }
    setToggleVisual("idle");
  }

  toggle.addEventListener("click", () => {
    muted = !muted;
    localStorage.setItem(STORAGE_KEY, muted ? "1" : "0");
    if (muted) disconnect();
    else connect();
    setToggleVisual("idle");
  });

  window.addEventListener("athena:busy", () => {
    athenaBusy = true;
    disconnect();
  });
  window.addEventListener("athena:idle", () => {
    athenaBusy = false;
    connect();
  });

  setToggleVisual("idle");
  if (!muted) connect(); // on by default — no click needed to arm it
})();
