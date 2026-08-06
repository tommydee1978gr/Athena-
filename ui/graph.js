/* Force-directed graph on Canvas. Not SVG — SVG needs a DOM node per element
 * and stalls well before the graph gets interesting; canvas stays smooth.
 * Repulsion uses a spatial grid with a distance cutoff so cost stays near
 * linear instead of the naive O(n^2) all-pairs check. */
(function () {
  const TYPE_COLOR = { project: "#73a9ff", platform: "#f7c948", memory: "#9c7fe0" };
  const REPULSION_CUTOFF = 160;
  const REPULSION_STRENGTH = 2600;
  const SPRING_LENGTH = 110;
  const SPRING_STRENGTH = 0.02;
  const CENTER_PULL = 0.002;
  const DAMPING = 0.86;
  const WARM_START_TICKS = 220;

  class AthenaGraph {
    constructor(canvas, { onFocus, onHover } = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.onFocus = onFocus || (() => {});
      this.onHover = onHover || (() => {});
      this.nodes = [];
      this.edges = [];
      this.byId = new Map();
      this.hoverNode = null;
      this.focusNode = null;
      this.pathNodeA = null;
      this.pathSet = null; // Set of node ids on the traced shortest path
      this.typeVisible = { project: true, platform: true, memory: true };
      this.camera = { x: 0, y: 0, zoom: 1 };
      this.dragNode = null;
      this.panning = false;
      this.lastPointer = { x: 0, y: 0 };
      this.idlePulse = null;
      this._resize();
      window.addEventListener("resize", () => this._resize());
      this._bindPointer();
      requestAnimationFrame(() => this._frame());
      setInterval(() => this._maybeStartIdlePulse(), 2600);
    }

    setData(nodes, edges) {
      const w = this.canvas.width / (window.devicePixelRatio || 1);
      const h = this.canvas.height / (window.devicePixelRatio || 1);
      this.nodes = nodes.map((n) => {
        const existing = this.byId.get(n.id);
        return {
          ...n,
          x: existing ? existing.x : w / 2 + (Math.random() - 0.5) * 200,
          y: existing ? existing.y : h / 2 + (Math.random() - 0.5) * 200,
          vx: 0,
          vy: 0,
          radius: 8 + Math.min(22, Math.sqrt(1 + (n.connections || 0)) * 6),
        };
      });
      this.byId = new Map(this.nodes.map((n) => [n.id, n]));
      this.edges = edges
        .map((e) => ({ source: this.byId.get(e.source), target: this.byId.get(e.target) }))
        .filter((e) => e.source && e.target);
      this.adjacency = new Map(this.nodes.map((n) => [n.id, []]));
      for (const e of this.edges) {
        this.adjacency.get(e.source.id).push(e.target.id);
        this.adjacency.get(e.target.id).push(e.source.id);
      }
      for (let i = 0; i < WARM_START_TICKS; i++) this._tick(true);
    }

    setTypeVisible(type, visible) {
      this.typeVisible[type] = visible;
    }

    focusNodeById(id) {
      this.focusNode = this.byId.get(id) || null;
      this.onFocus(this.focusNode);
    }

    topHubs(n = 5) {
      return [...this.nodes].sort((a, b) => (b.connections || 0) - (a.connections || 0)).slice(0, n);
    }

    _resize() {
      const dpr = window.devicePixelRatio || 1;
      this.canvas.width = window.innerWidth * dpr;
      this.canvas.height = window.innerHeight * dpr;
      this.canvas.style.width = window.innerWidth + "px";
      this.canvas.style.height = window.innerHeight + "px";
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    _visibleNodes() {
      return this.nodes.filter((n) => this.typeVisible[n.type] !== false);
    }

    // --- physics -----------------------------------------------------------
    _tick(warm) {
      const nodes = this.nodes;
      if (!nodes.length) return;
      const cell = REPULSION_CUTOFF;
      const grid = new Map();
      const key = (x, y) => `${Math.floor(x / cell)}:${Math.floor(y / cell)}`;
      for (const n of nodes) {
        const k = key(n.x, n.y);
        if (!grid.has(k)) grid.set(k, []);
        grid.get(k).push(n);
      }
      for (const n of nodes) {
        let fx = 0, fy = 0;
        const gx = Math.floor(n.x / cell), gy = Math.floor(n.y / cell);
        for (let dx = -1; dx <= 1; dx++) {
          for (let dy = -1; dy <= 1; dy++) {
            const bucket = grid.get(`${gx + dx}:${gy + dy}`);
            if (!bucket) continue;
            for (const other of bucket) {
              if (other === n) continue;
              let ddx = n.x - other.x, ddy = n.y - other.y;
              let dist2 = ddx * ddx + ddy * ddy;
              if (dist2 > REPULSION_CUTOFF * REPULSION_CUTOFF) continue;
              if (dist2 < 4) { ddx = Math.random() - 0.5; ddy = Math.random() - 0.5; dist2 = 4; }
              const dist = Math.sqrt(dist2);
              const force = REPULSION_STRENGTH / dist2;
              fx += (ddx / dist) * force;
              fy += (ddy / dist) * force;
            }
          }
        }
        n._fx = fx;
        n._fy = fy;
      }
      for (const e of this.edges) {
        const dx = e.target.x - e.source.x, dy = e.target.y - e.source.y;
        const dist = Math.max(1, Math.sqrt(dx * dx + dy * dy));
        const stretch = (dist - SPRING_LENGTH) * SPRING_STRENGTH;
        const fx = (dx / dist) * stretch, fy = (dy / dist) * stretch;
        e.source._fx += fx; e.source._fy += fy;
        e.target._fx -= fx; e.target._fy -= fy;
      }
      const w = this.canvas.width / (window.devicePixelRatio || 1);
      const h = this.canvas.height / (window.devicePixelRatio || 1);
      for (const n of nodes) {
        if (n === this.dragNode) { n.vx = 0; n.vy = 0; continue; }
        n._fx += (w / 2 - n.x) * CENTER_PULL;
        n._fy += (h / 2 - n.y) * CENTER_PULL;
        n.vx = (n.vx + n._fx) * DAMPING;
        n.vy = (n.vy + n._fy) * DAMPING;
        n.x += n.vx * (warm ? 1 : 0.6);
        n.y += n.vy * (warm ? 1 : 0.6);
      }
    }

    _maybeStartIdlePulse() {
      if (!this.edges.length || this.idlePulse) return;
      const e = this.edges[Math.floor(Math.random() * this.edges.length)];
      this.idlePulse = { edge: e, t: 0 };
    }

    // --- render --------------------------------------------------------------
    _frame() {
      this._tick(false);
      this._time = (this._time || 0) + 1;
      this._render();
      requestAnimationFrame(() => this._frame());
    }

    _render() {
      const ctx = this.ctx;
      const w = this.canvas.width / (window.devicePixelRatio || 1);
      const h = this.canvas.height / (window.devicePixelRatio || 1);
      ctx.clearRect(0, 0, w, h);
      this._renderDust(ctx, w, h);
      ctx.save();
      ctx.translate(this.camera.x, this.camera.y);
      ctx.scale(this.camera.zoom, this.camera.zoom);

      const visible = new Set(this._visibleNodes().map((n) => n.id));
      const litSet = this._litSet();

      // edges — drawn as organic glowing synapses, not straight ruled lines:
      // a slight bow through a midpoint that breathes with time, real glow via
      // shadowBlur, and a soft core-to-edge gradient stroke.
      for (const e of this.edges) {
        if (!visible.has(e.source.id) || !visible.has(e.target.id)) continue;
        const lit = !litSet || (litSet.has(e.source.id) && litSet.has(e.target.id));
        this._drawSynapse(ctx, e, lit);
      }

      // idle pulse traveling a random link, as a small glowing spark
      if (this.idlePulse) {
        const { edge, t } = this.idlePulse;
        const [mx, my] = this._edgeMid(edge);
        const p = this._bezierPoint(edge.source, { x: mx, y: my }, edge.target, t);
        ctx.save();
        ctx.globalAlpha = 0.95;
        ctx.shadowColor = "#bcd6ff";
        ctx.shadowBlur = 14;
        ctx.fillStyle = "#eaf3ff";
        ctx.beginPath();
        ctx.arc(p.x, p.y, 2.2, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
        this.idlePulse.t += 0.01;
        if (this.idlePulse.t >= 1) this.idlePulse = null;
      }

      // nodes — soft radial-glow orbs (bloom) instead of flat filled circles
      const placedLabels = [];
      const drawOrder = [...this._visibleNodes()].sort((a, b) => (b.connections || 0) - (a.connections || 0));
      for (const n of drawOrder) {
        const lit = !litSet || litSet.has(n.id);
        this._drawNode(ctx, n, lit);
        if (lit) {
          const label = n.label || "";
          ctx.font = "12px system-ui, sans-serif";
          const metrics = ctx.measureText(label);
          const r = n === this.hoverNode ? n.radius * 1.15 : n.radius;
          const box = { x: n.x - metrics.width / 2 - 2, y: n.y + r + 4, w: metrics.width + 4, h: 14 };
          const collides = placedLabels.some((p) => !(box.x + box.w < p.x || box.x > p.x + p.w || box.y + box.h < p.y || box.y > p.y + p.h));
          if (!collides) {
            placedLabels.push(box);
            ctx.save();
            ctx.fillStyle = "#ecf1ff";
            ctx.globalAlpha = 0.95;
            ctx.textAlign = "center";
            ctx.shadowColor = "#000";
            ctx.shadowBlur = 4;
            ctx.fillText(label, n.x, n.y + r + 14);
            ctx.restore();
          }
        }
      }
      ctx.restore();
    }

    _edgeMid(e) {
      // Perpendicular bow so the connection reads as an organic dendrite
      // rather than a ruled line — offset direction is stable per edge (hashed
      // from endpoint ids) so it doesn't flip every frame, magnitude breathes
      // gently with _time.
      const dx = e.target.x - e.source.x, dy = e.target.y - e.source.y;
      const len = Math.max(1, Math.hypot(dx, dy));
      const nx = -dy / len, ny = dx / len;
      const hash = (e.source.id + e.target.id).split("").reduce((a, c) => a + c.charCodeAt(0), 0);
      const sign = hash % 2 === 0 ? 1 : -1;
      const breathe = Math.sin((this._time || 0) * 0.01 + hash) * 4;
      const bow = sign * (len * 0.08 + breathe);
      return [(e.source.x + e.target.x) / 2 + nx * bow, (e.source.y + e.target.y) / 2 + ny * bow];
    }

    _bezierPoint(a, mid, b, t) {
      const x = (1 - t) * (1 - t) * a.x + 2 * (1 - t) * t * mid.x + t * t * b.x;
      const y = (1 - t) * (1 - t) * a.y + 2 * (1 - t) * t * mid.y + t * t * b.y;
      return { x, y };
    }

    _drawSynapse(ctx, e, lit) {
      const [mx, my] = this._edgeMid(e);
      ctx.save();
      ctx.globalAlpha = lit ? 0.85 : 0.06;
      ctx.shadowColor = "#5f8fe6";
      ctx.shadowBlur = lit ? 10 : 0;
      const grad = ctx.createLinearGradient(e.source.x, e.source.y, e.target.x, e.target.y);
      grad.addColorStop(0, TYPE_COLOR[e.source.type] || "#5f8fe6");
      grad.addColorStop(1, TYPE_COLOR[e.target.type] || "#5f8fe6");
      ctx.strokeStyle = grad;
      ctx.lineWidth = lit ? 1.6 : 1;
      ctx.beginPath();
      ctx.moveTo(e.source.x, e.source.y);
      ctx.quadraticCurveTo(mx, my, e.target.x, e.target.y);
      ctx.stroke();
      ctx.restore();
    }

    _drawNode(ctx, n, lit) {
      const color = TYPE_COLOR[n.type] || "#888";
      const r = n === this.hoverNode ? n.radius * 1.15 : n.radius;
      const alpha = lit ? 1 : 0.12;
      ctx.save();
      ctx.globalAlpha = alpha;
      // outer bloom
      const glow = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, r * 2.6);
      glow.addColorStop(0, color + "aa");
      glow.addColorStop(0.4, color + "33");
      glow.addColorStop(1, color + "00");
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(n.x, n.y, r * 2.6, 0, Math.PI * 2);
      ctx.fill();
      // core with a hot-white center for a lit-from-within feel
      const core = ctx.createRadialGradient(n.x - r * 0.3, n.y - r * 0.3, 0, n.x, n.y, r);
      core.addColorStop(0, "#ffffff");
      core.addColorStop(0.35, color);
      core.addColorStop(1, color);
      ctx.shadowColor = color;
      ctx.shadowBlur = lit ? 16 : 0;
      ctx.fillStyle = core;
      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.fill();
      if (n === this.focusNode || (this.pathSet && this.pathSet.has(n.id))) {
        ctx.shadowBlur = 0;
        ctx.strokeStyle = "#fff";
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      ctx.restore();
    }

    _renderDust(ctx, w, h) {
      // Faint drifting particle field behind the graph, echoing the brain
      // artwork's starfield so the two visuals read as the same world.
      if (!this._dust) {
        this._dust = Array.from({ length: 90 }, () => ({ x: Math.random() * w, y: Math.random() * h, r: Math.random() * 1.4 + 0.3, s: Math.random() * 0.15 + 0.03, p: Math.random() * Math.PI * 2 }));
      }
      ctx.save();
      for (const d of this._dust) {
        d.y -= d.s;
        if (d.y < -4) d.y = h + 4;
        const twinkle = 0.35 + 0.35 * Math.sin((this._time || 0) * 0.02 + d.p);
        ctx.globalAlpha = twinkle;
        ctx.fillStyle = "#bcd6ff";
        ctx.beginPath();
        ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    }

    _litSet() {
      if (this.pathSet) return this.pathSet;
      if (this.hoverNode) {
        const s = new Set([this.hoverNode.id, ...(this.adjacency.get(this.hoverNode.id) || [])]);
        return s;
      }
      return null;
    }

    // --- interaction ---------------------------------------------------------
    _screenToWorld(clientX, clientY) {
      const rect = this.canvas.getBoundingClientRect();
      const x = (clientX - rect.left - this.camera.x) / this.camera.zoom;
      const y = (clientY - rect.top - this.camera.y) / this.camera.zoom;
      return { x, y };
    }

    _nodeAt(clientX, clientY) {
      const { x, y } = this._screenToWorld(clientX, clientY);
      for (const n of this._visibleNodes()) {
        const dx = n.x - x, dy = n.y - y;
        if (dx * dx + dy * dy <= (n.radius + 4) * (n.radius + 4)) return n;
      }
      return null;
    }

    _shortestPath(aId, bId) {
      const queue = [[aId]];
      const seen = new Set([aId]);
      while (queue.length) {
        const path = queue.shift();
        const last = path[path.length - 1];
        if (last === bId) return path;
        for (const next of this.adjacency.get(last) || []) {
          if (seen.has(next)) continue;
          seen.add(next);
          queue.push([...path, next]);
        }
      }
      return null;
    }

    _bindPointer() {
      const c = this.canvas;
      c.addEventListener("mousedown", (ev) => {
        const node = this._nodeAt(ev.clientX, ev.clientY);
        if (node) {
          this.dragNode = node;
        } else {
          this.panning = true;
          c.classList.add("dragging");
        }
        this.lastPointer = { x: ev.clientX, y: ev.clientY };
      });
      window.addEventListener("mousemove", (ev) => {
        if (this.dragNode) {
          const { x, y } = this._screenToWorld(ev.clientX, ev.clientY);
          this.dragNode.x = x;
          this.dragNode.y = y;
          this.dragNode.vx = 0;
          this.dragNode.vy = 0;
        } else if (this.panning) {
          this.camera.x += ev.clientX - this.lastPointer.x;
          this.camera.y += ev.clientY - this.lastPointer.y;
          this.lastPointer = { x: ev.clientX, y: ev.clientY };
        } else {
          const hovered = this._nodeAt(ev.clientX, ev.clientY);
          if (hovered !== this.hoverNode) {
            this.hoverNode = hovered;
            this.onHover(hovered);
          }
        }
      });
      window.addEventListener("mouseup", (ev) => {
        if (this.dragNode) {
          this.dragNode = null;
        } else if (this.panning) {
          this.panning = false;
          c.classList.remove("dragging");
        }
      });
      c.addEventListener("click", (ev) => {
        const node = this._nodeAt(ev.clientX, ev.clientY);
        if (!node) return;
        if (ev.shiftKey && this.pathNodeA && this.pathNodeA !== node) {
          const path = this._shortestPath(this.pathNodeA.id, node.id);
          this.pathSet = path ? new Set(path) : new Set();
          this.pathNodeA = null;
        } else if (ev.shiftKey) {
          this.pathNodeA = node;
          this.pathSet = null;
        } else {
          this.pathSet = null;
          this.pathNodeA = null;
          this.focusNode = node;
          this.onFocus(node);
        }
      });
      c.addEventListener("wheel", (ev) => {
        ev.preventDefault();
        const factor = ev.deltaY < 0 ? 1.1 : 0.9;
        const rect = c.getBoundingClientRect();
        const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
        this.camera.x = mx - (mx - this.camera.x) * factor;
        this.camera.y = my - (my - this.camera.y) * factor;
        this.camera.zoom = Math.min(3, Math.max(0.25, this.camera.zoom * factor));
      }, { passive: false });
    }
  }

  window.AthenaGraph = AthenaGraph;
})();
