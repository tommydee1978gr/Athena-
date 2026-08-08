/* Wires the graph engine (graph.js) to ATHENA's real API — /api/graph/data,
 * /api/ask, /api/voice/stt, /api/voice/tts. Nothing here talks to a mock. */
(function () {
  const SILENCE_MS = 900; // named per the brief — tune here if the turn ends too eagerly/late
  const SILENCE_LEVEL_THRESHOLD = 0.02;

  const csrf = window.ATHENA_CSRF;
  const graphCanvas = document.getElementById("graphCanvas");
  const inspector = document.getElementById("inspector");
  const hubsList = document.getElementById("hubsList");
  const filterList = document.getElementById("filterList");
  const reactor = document.getElementById("reactor");
  const modelLabel = document.getElementById("modelLabel");
  const reactorWave = document.getElementById("reactorWave");
  const FAMILY_LABELS = { claude: "Claude", codex_openai: "Codex/OpenAI", gemini: "Gemini", grok: "Grok" };
  const askInput = document.getElementById("askInput");
  const askSend = document.getElementById("askSend");
  const micButton = document.getElementById("micButton");
  const refreshButton = document.getElementById("refreshButton");
  const answerCard = document.getElementById("answerCard");
  const exampleHint = document.getElementById("exampleHint");

  const EXAMPLES = [
    "Πες μου prompt ιδέα για Suno σε lofi ύφος",
    "Ποια projects είναι σε εξέλιξη;",
    "Τι έμαθες σήμερα;",
    "Δείξε μου τι δούλεψε στο Higgsfield τελευταία",
  ];
  let exampleIndex = 0;
  function rotateExample() {
    askInput.placeholder = EXAMPLES[exampleIndex % EXAMPLES.length];
    exampleIndex++;
  }
  rotateExample();
  setInterval(rotateExample, 5000);

  async function api(path, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    if (options.body && !(options.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
    }
    if (options.method && options.method !== "GET") headers["X-CSRF-Token"] = csrf;
    const response = await fetch(path, Object.assign({}, options, { headers }));
    if (!response.ok) {
      const text = await response.text();
      throw new Error(`${response.status}: ${text.slice(0, 300)}`);
    }
    return response;
  }

  const reactorLabel = document.getElementById("reactorLabel");
  function setReactor(state) {
    reactor.className = state;
    reactorLabel.textContent = { idle: "idle", listening: "ακούω", thinking: "σκέφτομαι", speaking: "μιλάω" }[state] || state;
    // Single choke point for the wake-word listener (wakeword.js) to know when it's
    // safe to listen for "Αθηνά" again — never while ATHENA itself has the mic, is
    // thinking, or is talking (talking back into its own wake-word ears would loop).
    window.dispatchEvent(new CustomEvent(state === "idle" ? "athena:idle" : "athena:busy"));
  }

  // --- graph -----------------------------------------------------------------
  const graph = new window.AthenaGraph(graphCanvas, {
    onFocus: renderInspector,
    onHover: (node) => {
      if (node) graphCanvas.title = node.label;
    },
  });

  let lastNodes = [];
  async function loadGraph() {
    const response = await api("/api/graph/data");
    const data = await response.json();
    lastNodes = data.nodes;
    graph.setData(data.nodes, data.edges);
    renderFilters(data.nodes);
    renderHubs();
    if (!inspector.dataset.hasFocus) renderInspector(null);
  }

  function renderFilters(nodes) {
    const counts = { project: 0, platform: 0, memory: 0 };
    for (const n of nodes) counts[n.type] = (counts[n.type] || 0) + 1;
    const labels = { project: "Projects", platform: "Πλατφόρμες", memory: "Μνήμες" };
    filterList.innerHTML = Object.keys(labels)
      .map(
        (type) => `<div class="filterRow">
          <label><input type="checkbox" checked data-type="${type}"><span class="dot ${type}"></span>${labels[type]}</label>
          <span class="count">${counts[type] || 0}</span>
        </div>`
      )
      .join("");
    filterList.querySelectorAll("input[data-type]").forEach((box) => {
      box.addEventListener("change", () => graph.setTypeVisible(box.dataset.type, box.checked));
    });
  }

  function renderHubs() {
    const hubs = graph.topHubs(5).filter((n) => (n.connections || 0) > 0);
    hubsList.innerHTML = hubs.length
      ? hubs.map((n) => `<li data-id="${n.id}">${n.label} <span style="color:var(--muted)">(${n.connections})</span></li>`).join("")
      : '<li class="empty" style="list-style:none;color:var(--muted)">Δεν υπάρχουν ακόμα συνδέσεις.</li>';
    hubsList.querySelectorAll("li[data-id]").forEach((li) => {
      li.addEventListener("click", () => graph.focusNodeById(li.dataset.id));
    });
  }

  function renderInspector(node) {
    if (!node) {
      inspector.dataset.hasFocus = "";
      document.getElementById("inspectorBody").innerHTML = '<p class="empty">Κάνε κλικ σε έναν κόμβο για λεπτομέρειες. Shift+κλικ σε δεύτερο κόμβο δείχνει τη συντομότερη διαδρομή ανάμεσά τους.</p>';
      return;
    }
    inspector.dataset.hasFocus = "1";
    const typeLabel = { project: "Project", platform: "Πλατφόρμα", memory: "Μνήμη" }[node.type] || node.type;
    const rows = [`<div class="field"><b>Τύπος</b>${typeLabel}</div>`];
    if (node.status) rows.push(`<div class="field"><b>Status</b>${node.status}</div>`);
    if (node.kind) rows.push(`<div class="field"><b>Είδος</b>${node.kind}</div>`);
    if (node.namespace) rows.push(`<div class="field"><b>Χώρος</b>${node.namespace}</div>`);
    rows.push(`<div class="field"><b>Συνδέσεις</b>${node.connections || 0}</div>`);
    document.getElementById("inspectorBody").innerHTML = `<h2>${escapeHtml(node.label)}</h2>${rows.join("")}`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // --- dynamic answer cards ------------------------------------------------
  // ask() used to only ever show prose text. Some questions have a much more
  // useful shape than a paragraph — health stats, a list of emails, a list of
  // home devices — and the tool ATHENA already called to answer them (see
  // tool_results in orchestrator.py's ask()) has exactly that structured data
  // sitting right there. Each entry here is CARD_RENDERERS[toolName](result) =>
  // an HTML string, or null to fall back to plain text (e.g. the tool errored,
  // or has nothing to show yet). Adding a new card later is just adding a new
  // entry — no other wiring needed, tool_results already carries every tool
  // call ATHENA made this turn. Reuses the escapeHtml() already defined
  // above for graph node labels.
  const CARD_RENDERERS = {
    health_summary(result) {
      if (!result || !result.has_any_data) {
        return `<div class="dynCard dynCard-health"><div class="dynCard-title">🩺 Υγεία</div>
          <p class="muted">Δεν έχουν συγχρονιστεί δεδομένα ακόμα — ρύθμισε το Health Connect Webhook app στο κινητό σου (βλ. /integrations).</p></div>`;
      }
      const rows = [];
      if (result.avg_steps_per_day != null) rows.push(statRow("👣", "Βήματα/ημέρα (μ.ο.)", Math.round(result.avg_steps_per_day).toLocaleString("el-GR")));
      if (result.avg_sleep_hours != null) rows.push(statRow("😴", "Ύπνος (μ.ο.)", result.avg_sleep_hours + " ώρες"));
      if (result.heart_rate_bpm) rows.push(statRow("❤️", "Καρδιακοί παλμοί", `${result.heart_rate_bpm.latest} bpm (${result.heart_rate_bpm.min}-${result.heart_rate_bpm.max})`));
      if (result.resting_heart_rate_bpm) rows.push(statRow("💤", "Ηρεμίας", result.resting_heart_rate_bpm.latest + " bpm"));
      if (result.weight_kg) rows.push(statRow("⚖️", "Βάρος", result.weight_kg.latest + " kg"));
      return `<div class="dynCard dynCard-health"><div class="dynCard-title">🩺 Υγεία <span class="muted">(τελευταίες ${result.window_days} ημέρες)</span></div>${rows.join("")}</div>`;
    },
    gmail_search(result) {
      const messages = (result && result.messages) || [];
      if (!messages.length) return `<div class="dynCard dynCard-email"><div class="dynCard-title">📧 Email</div><p class="muted">Καμία αντιστοιχία.</p></div>`;
      const rows = messages.slice(0, 6).map((m) => {
        const from = (m.headers && m.headers.from || "").replace(/<.*>/, "").trim() || "?";
        const subject = (m.headers && m.headers.subject) || "(χωρίς θέμα)";
        return `<div class="dynCard-row"><div class="dynCard-row-main"><strong>${escapeHtml(from)}</strong><span class="dynCard-row-sub">${escapeHtml(subject)}</span></div>
          <div class="dynCard-row-detail">${escapeHtml((m.snippet || "").slice(0, 90))}</div></div>`;
      }).join("");
      const more = messages.length > 6 ? `<p class="muted">+${messages.length - 6} ακόμα</p>` : "";
      return `<div class="dynCard dynCard-email"><div class="dynCard-title">📧 Email</div>${rows}${more}</div>`;
    },
    homeassistant_states(result) {
      const entities = Array.isArray(result) ? result : (result && result.entity_id ? [result] : []);
      if (!entities.length) return `<div class="dynCard dynCard-home"><div class="dynCard-title">🏠 Σπίτι</div><p class="muted">Καμία συσκευή βρέθηκε.</p></div>`;
      const DOMAIN_ICON = { light: "💡", switch: "🔌", climate: "🌡️", lock: "🔒", cover: "🪟", media_player: "📺", sensor: "📊", person: "🧍", binary_sensor: "🔘" };
      const OK_STATES = new Set(["on", "home", "unlocked", "open", "playing"]);
      const rows = entities.slice(0, 16).map((e) => {
        const domain = (e.entity_id || "").split(".")[0];
        const icon = DOMAIN_ICON[domain] || "⚙️";
        const label = (e.attributes && e.attributes.friendly_name) || e.entity_id;
        const stateClass = OK_STATES.has(e.state) ? "ok" : (e.state === "unavailable" || e.state === "unknown" ? "warn" : "");
        return `<div class="dynCard-row dynCard-row-compact"><span>${icon} ${escapeHtml(label)}</span><span class="dynCard-state ${stateClass}">${escapeHtml(e.state)}</span></div>`;
      }).join("");
      const more = entities.length > 16 ? `<p class="muted">+${entities.length - 16} ακόμα</p>` : "";
      return `<div class="dynCard dynCard-home"><div class="dynCard-title">🏠 Σπίτι</div>${rows}${more}</div>`;
    },
  };

  function statRow(icon, label, value) {
    return `<div class="dynCard-row dynCard-row-compact"><span>${icon} ${label}</span><strong>${escapeHtml(String(value))}</strong></div>`;
  }

  // Picks the last tool_results entry ATHENA's own reasoning actually used
  // this turn that both has a renderer here and didn't error — "last" so
  // that if several tools were called, whichever one drove the final answer
  // (typically called closest to producing it) wins.
  function renderCardFor(toolResults) {
    if (!Array.isArray(toolResults)) return null;
    for (let i = toolResults.length - 1; i >= 0; i--) {
      const entry = toolResults[i];
      const renderer = entry && CARD_RENDERERS[entry.name];
      if (!renderer) continue;
      if (entry.result && entry.result.status === "error") continue;
      return renderer(entry.result);
    }
    return null;
  }

  // --- ask ---------------------------------------------------------------
  // /api/ask streams newline-delimited JSON ({"event":"delta","text":...} then
  // one final {"event":"done","result":{...}}) so text appears as it's
  // generated instead of only once the whole answer is ready.
  async function ask(question, voice = false) {
    if (!question.trim()) return;
    setReactor("thinking");
    answerCard.classList.remove("visible");
    let streamed = "";
    let shownFirstDelta = false;
    try {
      const response = await api("/api/ask", { method: "POST", body: JSON.stringify({ question, voice }) });
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let result = null;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let newlineIndex;
        while ((newlineIndex = buffer.indexOf("\n")) !== -1) {
          const line = buffer.slice(0, newlineIndex);
          buffer = buffer.slice(newlineIndex + 1);
          if (!line) continue;
          const item = JSON.parse(line);
          if (item.event === "delta") {
            streamed += item.text;
            if (!shownFirstDelta) { shownFirstDelta = true; setReactor("thinking"); }
            showAnswer(streamed);
          } else if (item.event === "done") {
            result = item.result;
          }
        }
      }
      const data = result || {};
      const card = renderCardFor(data.tool_results);
      if (card) showCard(card); else showAnswer(data.answer || streamed || "(χωρίς απάντηση)");
      const family = data.brain && data.brain.route_family;
      modelLabel.textContent = family ? FAMILY_LABELS[family] || family : "";
      await maybeSpeak(data.answer || streamed);
      loadGraph(); // a new project/prompt/memory may have just been created
    } catch (err) {
      showAnswer("Σφάλμα: " + err.message);
      setReactor("idle");
    }
  }

  function showAnswer(text) {
    answerCard.textContent = text;
    answerCard.classList.add("visible");
  }

  function showCard(html) {
    answerCard.innerHTML = html;
    answerCard.classList.add("visible");
  }

  askSend.addEventListener("click", () => {
    const q = askInput.value;
    askInput.value = "";
    ask(q);
  });
  askInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") askSend.click();
  });

  // --- voice: press mic once, talk, ~900ms silence ends the turn -------------
  let mediaStream = null;
  let recorder = null;
  let chunks = [];
  let audioCtx = null;
  let analyser = null;
  let levelTimer = null;
  let silenceStartedAt = null;
  let recording = false;

  async function startListening() {
    if (recording) { stopListening(true); return; } // mic button again = barge-in / stop
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      showAnswer("Δεν έχω πρόσβαση στο μικρόφωνο: " + err.message);
      return;
    }
    recording = true;
    micButton.classList.add("recording");
    setReactor("listening");
    chunks = [];
    recorder = new MediaRecorder(mediaStream);
    recorder.ondataavailable = (e) => chunks.push(e.data);
    recorder.onstop = onRecordingStopped;
    recorder.start();

    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const source = audioCtx.createMediaStreamSource(mediaStream);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
    const buffer = new Uint8Array(analyser.frequencyBinCount);
    silenceStartedAt = null;
    // setInterval, not requestAnimationFrame — rAF stops in a backgrounded tab
    // and the mic would silently stop noticing silence.
    levelTimer = setInterval(() => {
      analyser.getByteTimeDomainData(buffer);
      let sumSquares = 0;
      for (let i = 0; i < buffer.length; i++) {
        const v = (buffer[i] - 128) / 128;
        sumSquares += v * v;
      }
      const level = Math.sqrt(sumSquares / buffer.length);
      updateBars(level);
      const now = Date.now();
      if (level < SILENCE_LEVEL_THRESHOLD) {
        if (silenceStartedAt === null) silenceStartedAt = now;
        else if (now - silenceStartedAt >= SILENCE_MS) stopListening(false);
      } else {
        silenceStartedAt = null;
      }
    }, 100);
  }

  function updateBars(level) {
    reactor.style.boxShadow = level > 0.02 ? `0 0 ${8 + level * 60}px -2px var(--ok)` : "";
  }

  function stopListening() {
    if (!recording) return;
    recording = false;
    micButton.classList.remove("recording");
    clearInterval(levelTimer);
    levelTimer = null;
    if (recorder && recorder.state !== "inactive") recorder.stop();
    if (mediaStream) mediaStream.getTracks().forEach((t) => t.stop());
    if (audioCtx) audioCtx.close();
  }

  async function onRecordingStopped() {
    setReactor("thinking");
    const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
    if (blob.size < 500) { setReactor("idle"); return; } // essentially silence, nothing to send
    const fd = new FormData();
    fd.append("audio", blob, "turn.webm");
    fd.append("language", "el"); // was unset — Whisper fell back to auto-detect on a short clip and often guessed wrong, garbling what it heard
    try {
      const sttResponse = await api("/api/voice/stt", { method: "POST", body: fd });
      const stt = await sttResponse.json();
      const heard = (stt.text || "").trim();
      if (!heard) { setReactor("idle"); return; }
      askInput.value = "";
      await ask(heard, true);
    } catch (err) {
      showAnswer("Δεν έγινε η μεταγραφή: " + err.message);
      setReactor("idle");
    }
  }

  let speakAudioCtx = null;
  function drawWaveform(audioEl) {
    const ctx2d = reactorWave.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    reactorWave.width = 84 * dpr;
    reactorWave.height = 84 * dpr;
    ctx2d.scale(dpr, dpr);
    speakAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const source = speakAudioCtx.createMediaElementSource(audioEl);
    const analyser = speakAudioCtx.createAnalyser();
    analyser.fftSize = 128;
    source.connect(analyser);
    analyser.connect(speakAudioCtx.destination);
    const data = new Uint8Array(analyser.frequencyBinCount);
    let raf = null;
    function tick() {
      if (audioEl.paused || audioEl.ended) { ctx2d.clearRect(0, 0, 84, 84); return; }
      analyser.getByteTimeDomainData(data);
      ctx2d.clearRect(0, 0, 84, 84);
      ctx2d.fillStyle = "rgba(6,10,24,0.35)";
      ctx2d.fillRect(0, 0, 84, 84);
      ctx2d.strokeStyle = "#eaf3ff";
      ctx2d.lineWidth = 1.6;
      ctx2d.beginPath();
      const step = 84 / data.length;
      for (let i = 0; i < data.length; i++) {
        const v = (data[i] - 128) / 128; // -1..1
        const x = i * step;
        const y = 42 + v * 34;
        if (i === 0) ctx2d.moveTo(x, y); else ctx2d.lineTo(x, y);
      }
      ctx2d.stroke();
      raf = requestAnimationFrame(tick);
    }
    tick();
    return () => { if (raf) cancelAnimationFrame(raf); };
  }

  async function maybeSpeak(text) {
    if (!text) { setReactor("idle"); return; }
    try {
      setReactor("speaking");
      const response = await api("/api/voice/tts", { method: "POST", body: JSON.stringify({ text }) });
      const blob = await response.blob();
      const audio = new Audio(URL.createObjectURL(blob));
      let stopWave = null;
      audio.onplay = () => { stopWave = drawWaveform(audio); };
      const cleanup = () => {
        if (stopWave) stopWave();
        if (speakAudioCtx) { speakAudioCtx.close(); speakAudioCtx = null; }
        setReactor("idle");
      };
      // The mic must go deaf while ATHENA talks, or it transcribes itself and
      // loops forever. There is no mic capture running here, so nothing to
      // silence explicitly — this comment documents the invariant on purpose.
      audio.onended = cleanup;
      audio.onerror = cleanup;
      await audio.play();
    } catch (err) {
      // TTS is optional (needs ElevenLabs or local Piper configured) — a
      // missing voice backend must never block the text answer already shown.
      setReactor("idle");
    }
  }

  micButton.addEventListener("click", startListening);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && recording) stopListening(true);
  });
  refreshButton.addEventListener("click", loadGraph);

  // wakeword.js calls this once it hears "Αθηνά" — same precise-capture path the
  // mic button uses, so STT quality/silence-detection is identical either way.
  window.ATHENA_startListening = startListening;

  setReactor("idle");
  loadGraph().catch((err) => showAnswer("Δεν φορτώθηκε ο γράφος: " + err.message));
})();
