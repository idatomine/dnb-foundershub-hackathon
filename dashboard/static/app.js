const BANDS = ["125", "250", "500", "1000", "2000", "4000"];
const BAND_LABELS = { "125": "125", "250": "250", "500": "500", "1000": "1k", "2000": "2k", "4000": "4k" };
const CATEGORY_COLORS = {
  wall: 0x94a3b8, floor: 0x94a3b8, ceiling: 0x94a3b8, window: 0x60a5fa,
  door: 0xd97706, furniture: 0x10b981, other: 0xc084fc,
};
const SELECT_COLOR = 0xf59e0b;
const SOURCE_LABELS = {
  unmeasured: "Unmeasured", estimated: "Estimated", manual: "Manual", rig_measured: "Rig-measured",
};
const SVG_NS = "http://www.w3.org/2000/svg";

let room = null;
let presets = [];
let selectedId = null;
let placeMode = false;
let hoveredId = null;

// ---------- 3D viewer ----------
const viewerEl = document.getElementById("viewer");
let scene, camera, renderer, controls, cloudPoints, raycaster;
const boxGroups = new Map(); // surface id -> THREE.Group

function setupViewer() {
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0d0f12);
  camera = new THREE.PerspectiveCamera(55, 1, 0.05, 100);
  camera.up.set(0, 0, 1);
  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  viewerEl.appendChild(renderer.domElement);
  controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.addEventListener("change", render);
  raycaster = new THREE.Raycaster();
  raycaster.params.Points.threshold = 0.05;

  new ResizeObserver(() => {
    const w = viewerEl.clientWidth, h = viewerEl.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    render();
  }).observe(viewerEl);

  let pending = null;
  renderer.domElement.addEventListener("pointermove", e => {
    if (pending) return;
    pending = requestAnimationFrame(() => { pending = null; updateHover(e); });
  });
  renderer.domElement.addEventListener("pointerleave", () => setHover(null));

  // A click only counts if the pointer barely moved, so orbiting does not select things.
  let down = null;
  renderer.domElement.addEventListener("pointerdown", e => { down = [e.clientX, e.clientY]; });
  renderer.domElement.addEventListener("pointerup", e => {
    if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
    onViewerClick(e);
  });
}

function render() { renderer.render(scene, camera); }

function updateHover(e) {
  if (e.buttons) { setHover(null); return; }
  const rect = renderer.domElement.getBoundingClientRect();
  raycaster.setFromCamera(new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1,
    -((e.clientY - rect.top) / rect.height) * 2 + 1), camera);
  const hit = raycaster.intersectObjects([...boxGroups.values()].map(g => g.children[0]), false)[0];
  const s = hit && room.surfaces.find(x => x.id === hit.object.userData.id);
  renderer.domElement.style.cursor = s && !placeMode ? "pointer" : "";
  setHover(s ? s.id : null);
}

// The hover card covers the editor without rebuilding it, so unsaved edits survive.
function setHover(id) {
  if (id === hoveredId) return;
  hoveredId = id;
  updateBoxVisibility();
  const card = document.getElementById("hover-card");
  const detail = document.getElementById("detail");
  const s = id && id !== selectedId && room.surfaces.find(x => x.id === id);
  if (!s) { card.hidden = true; detail.hidden = false; return; }
  fillHoverCard(card, s);
  card.hidden = false;
  detail.hidden = true;
}

function renderRecordings(container, s, withAudio) {
  container.replaceChildren();
  const recs = s.recordings || [];
  const h = document.createElement("h3");
  h.textContent = `Rig measurements (${recs.length})`;
  container.appendChild(h);
  if (!recs.length) {
    const p = document.createElement("p");
    p.className = "muted small";
    p.textContent = "No recordings linked to this object yet.";
    container.appendChild(p);
    return;
  }
  for (const r of recs) {
    const fig = document.createElement("figure");
    fig.className = "rec";
    // The photo the camera model judged. Without it there is no way to see
    // what the camera was actually pointed at when it made its guess.
    if (r.image) {
      const photo = document.createElement("img");
      photo.className = "rec-photo";
      photo.src = r.image;
      photo.alt = `Photo taken with ${r.id}`;
      photo.loading = "lazy";
      fig.appendChild(photo);
    }
    if (r.audio) {
      const canvas = document.createElement("canvas");
      canvas.className = "spectro";
      canvas.setAttribute("role", "img");
      canvas.setAttribute("aria-label", `Waveform and spectrogram of ${r.id}`);
      fig.appendChild(canvas);
      drawSpectrogram(canvas, r.audio);
    } else if (r.spectrogram) {
      const img = document.createElement("img");
      img.src = r.spectrogram; img.alt = `Spectrogram ${r.id}`; img.loading = "lazy";
      fig.appendChild(img);
    }
    const m = r.meta || {};
    const parts = [];
    if (m.material) parts.push(m.material);
    if (m.run != null) parts.push(`Run ${m.run}`);
    if (m.distance_mm != null) parts.push(`${m.distance_mm} mm`);
    if (m.sweep_f1 != null && m.sweep_f2 != null) parts.push(`sweep ${m.sweep_f1}–${m.sweep_f2} Hz`);
    if (m.peak_level != null) parts.push(`peak ${m.peak_level} dB`);
    if (m.recorded_at) parts.push(new Date(m.recorded_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
    const cap = document.createElement("figcaption");
    cap.className = "muted small";
    cap.textContent = parts.join(" · ") || r.id;
    if (r.mock) {
      const tag = document.createElement("span");
      tag.className = "mock-tag";
      tag.textContent = "mockup";
      cap.prepend(tag);
    }
    fig.appendChild(cap);
    // What each model made of this one measurement. The panel above shows only
    // the newest result per object, so without this a older measurement's own
    // numbers are invisible.
    const guesses = MODELS
      .map(m => [m, r.predictions?.[m.key]])
      .filter(([, probs]) => probs);
    if (guesses.length) {
      const line = document.createElement("p");
      line.className = "rec-guesses small";
      for (const [model, probs] of guesses) {
        const best = topMaterial(probs);
        const span = document.createElement("span");
        span.className = "rec-guess";
        span.textContent = `${model.key === "vision" ? "Camera" : "Sound"}: `
          + `${MATERIAL_LABELS[best] || best} ${Math.round(probs[best] * 100)}%`;
        line.appendChild(span);
      }
      fig.appendChild(line);
    }
    if (withAudio && r.audio) {
      const audio = document.createElement("audio");
      audio.controls = true; audio.preload = "none"; audio.src = r.audio;
      fig.appendChild(audio);
    }
    container.appendChild(fig);
  }
}

const MATERIAL_ORDER = ["textile", "wood", "plastic", "glass", "metal", "air"];
const MATERIAL_LABELS = { textile: "Textile", wood: "Wood", plastic: "Plastic", glass: "Glass", metal: "Metal", air: "Air" };
const MODELS = [
  { key: "vision", label: "Camera model", hint: "CLIP, from the photo" },
  { key: "sound", label: "Sound model", hint: "from the sweep recording" },
];

function topMaterial(probs) {
  return Object.entries(probs).sort((a, b) => b[1] - a[1])[0]?.[0];
}

function renderPredictions(container, s) {
  container.replaceChildren();
  const preds = s.predictions || {};
  for (const model of MODELS) {
    const block = document.createElement("div");
    block.className = "pred";
    const head = document.createElement("div");
    head.className = "pred-head";
    const name = document.createElement("span");
    name.textContent = model.label;
    const hint = document.createElement("span");
    hint.className = "muted";
    hint.textContent = model.hint;
    if (preds[model.key]?.mock) {
      const tag = document.createElement("span");
      tag.className = "mock-tag";
      tag.textContent = "mockup";
      name.append(" ", tag);
    }
    head.append(name, hint);
    block.appendChild(head);

    const probs = preds[model.key]?.probabilities;
    if (!probs) {
      const none = document.createElement("p");
      none.className = "muted small pred-none";
      none.textContent = "No output yet";
      block.appendChild(none);
    } else {
      const best = topMaterial(probs);
      const keys = [...MATERIAL_ORDER.filter(k => k in probs), ...Object.keys(probs).filter(k => !MATERIAL_ORDER.includes(k))];
      for (const k of keys) {
        const row = document.createElement("div");
        row.className = "pred-row" + (k === best ? " best" : "");
        const label = document.createElement("span");
        label.textContent = MATERIAL_LABELS[k] || k;
        const track = document.createElement("div");
        track.className = "pred-track";
        const bar = document.createElement("div");
        bar.className = "pred-bar";
        bar.style.width = `${Math.max(0, Math.min(1, probs[k])) * 100}%`;
        track.appendChild(bar);
        const pct = document.createElement("span");
        pct.className = "pred-pct";
        pct.textContent = `${Math.round(probs[k] * 100)}%`;
        row.append(label, track, pct);
        block.appendChild(row);
      }
    }
    container.appendChild(block);
  }

  const v = preds.vision?.probabilities, a = preds.sound?.probabilities;
  if (v && a) {
    const verdict = document.createElement("p");
    verdict.className = "pred-verdict";
    const [tv, ta] = [topMaterial(v), topMaterial(a)];
    verdict.textContent = tv === ta
      ? `Both models say ${MATERIAL_LABELS[tv] || tv}.`
      : `Models disagree: camera says ${MATERIAL_LABELS[tv] || tv}, sound says ${MATERIAL_LABELS[ta] || ta}.`;
    container.appendChild(verdict);
  }
}

function fillHoverCard(card, s) {
  card.replaceChildren();
  const title = document.createElement("h2");
  title.textContent = s.label;
  const sub = document.createElement("p");
  sub.className = "muted small";
  sub.textContent = s.category;
  const preds = document.createElement("div");
  preds.className = "preds";
  renderPredictions(preds, s);
  card.append(title, sub, preds);
}

async function loadCloud(meta) {
  const status = document.getElementById("viewer-status");
  if (!meta) { status.textContent = "No point cloud found. Run: python dashboard/prepare_cloud.py <file.ply>"; return; }
  status.textContent = "Loading point cloud…";
  const buf = await (await fetch("/api/cloud")).arrayBuffer();
  const n = meta.count;
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(buf, 0, n * 3), 3));
  geo.setAttribute("color", new THREE.BufferAttribute(new Uint8Array(buf, n * 12, n * 3), 3, true));
  cloudPoints = new THREE.Points(geo, new THREE.PointsMaterial({
    size: Number(document.getElementById("point-size").value), vertexColors: true,
    map: roundSprite(), alphaTest: 0.5, transparent: false }));
  scene.add(cloudPoints);

  const [mn, mx] = [meta.min, meta.max];
  const center = new THREE.Vector3((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, (mn[2] + mx[2]) / 2);
  controls.target.copy(center);
  // Start view: original offset (0, -9) rotated 270° in the xy plane → (-9, 0).
  camera.position.set(center.x - 9, center.y, center.z + 5);
  controls.update();
  status.textContent = `${n.toLocaleString()} points from ${meta.source}. Floor is near z = ${mn[2].toFixed(1)} m.`;
  render();
}

function roundSprite() {
  const c = document.createElement("canvas");
  c.width = c.height = 32;
  const g = c.getContext("2d");
  g.beginPath(); g.arc(16, 16, 15, 0, Math.PI * 2); g.fillStyle = "#fff"; g.fill();
  return new THREE.CanvasTexture(c);
}

function labelSprite(text) {
  const c = document.createElement("canvas");
  c.width = 256; c.height = 64;
  const g = c.getContext("2d");
  g.font = "bold 30px system-ui, sans-serif";
  g.textAlign = "center"; g.textBaseline = "middle";
  g.lineWidth = 6; g.strokeStyle = "#000"; g.strokeText(text, 128, 32);
  g.fillStyle = "#fff"; g.fillText(text, 128, 32);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), depthTest: false }));
  sprite.scale.set(1, 0.25, 1);
  sprite.renderOrder = 10;
  return sprite;
}

function rebuildBoxes() {
  for (const g of boxGroups.values()) scene.remove(g);
  boxGroups.clear();
  for (const s of room.surfaces) {
    if (s.geometry?.type !== "bbox") continue;
    const color = s.id === selectedId ? SELECT_COLOR : (CATEGORY_COLORS[s.category] ?? CATEGORY_COLORS.other);
    const group = new THREE.Group();
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1),
      new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.18, depthWrite: false }));
    mesh.userData.id = s.id;
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry),
      new THREE.LineBasicMaterial({ color, depthTest: false }));
    edges.renderOrder = 9;
    const label = labelSprite(s.label);
    label.position.set(0, 0, 0.5 + 0.3 / Math.max(s.geometry.size_m[2], 0.1));
    group.add(mesh, edges, label);
    group.userData.label = label;
    group.userData.edges = edges;
    boxGroups.set(s.id, group);
    applyBoxGeometry(group, s.geometry);
    scene.add(group);
  }
  updateBoxVisibility();
}

// Boxes stay invisible but still clickable; only the hovered and selected ones are drawn.
function updateBoxVisibility() {
  for (const [id, group] of boxGroups) {
    const show = id === selectedId || id === hoveredId;
    group.children[0].material.opacity = show ? 0.18 : 0;
    group.userData.edges.visible = show;
    group.userData.label.visible = show;
  }
  render();
}

function applyBoxGeometry(group, g) {
  group.position.set(...g.center_m);
  group.rotation.set(0, 0, THREE.MathUtils.degToRad(g.rotation_deg || 0));
  group.scale.set(...g.size_m);
  // Keep the label unstretched by the box scale.
  const label = group.userData.label;
  label.scale.set(1 / g.size_m[0], 0.25 / g.size_m[1], 1 / g.size_m[2]);
  label.position.set(0, 0, 0.5 + 0.2 / g.size_m[2]);
}

async function onViewerClick(e) {
  const rect = renderer.domElement.getBoundingClientRect();
  const ndc = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1,
                                -((e.clientY - rect.top) / rect.height) * 2 + 1);
  raycaster.setFromCamera(ndc, camera);

  if (placeMode) {
    const hit = cloudPoints && raycaster.intersectObject(cloudPoints)[0];
    if (!hit) return;
    const p = hit.point;
    const res = await fetch("/api/surfaces", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: "New object", category: "furniture",
        geometry: { center_m: [p.x, p.y, p.z], size_m: [0.5, 0.5, 0.5], rotation_deg: 0 } }),
    });
    if (!res.ok) return;
    setPlaceMode(false);
    await reloadRoom();
    select((await res.json()).id);
    return;
  }

  const hit = raycaster.intersectObjects([...boxGroups.values()].map(g => g.children[0]), false)[0];
  if (hit) select(hit.object.userData.id);
  else deselect();
}

function setPlaceMode(on) {
  placeMode = on;
  document.getElementById("place-btn").classList.toggle("active", on);
  document.getElementById("place-btn").textContent = on ? "Click the object in the 3D view…" : "+ Place object";
  viewerEl.classList.toggle("placing", on);
}

// ---------- data + list ----------
async function reloadRoom() {
  room = await (await fetch("/api/room")).json();
  rebuildBoxes();
  renderList();
  updateProgress();
}

async function init() {
  setupViewer();
  presets = (await (await fetch("/api/materials")).json()).presets || [];
  room = await (await fetch("/api/room")).json();
  document.getElementById("room-name").textContent = room.room?.name || "Room";
  document.getElementById("room-source").textContent = room.room?.source || "";
  document.getElementById("place-btn").addEventListener("click", () => setPlaceMode(!placeMode));
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    if (e.target.closest("input, textarea, select")) { e.target.blur(); return; }
    if (placeMode) setPlaceMode(false);
    else deselect();
  });
  document.getElementById("point-size").addEventListener("input", e => {
    if (cloudPoints) { cloudPoints.material.size = Number(e.target.value); render(); }
  });
  renderList();
  updateProgress();
  rebuildBoxes();
  await loadCloud(room.room?.cloud);
}

function renderList() {
  const ul = document.getElementById("object-list");
  ul.replaceChildren();
  if (!room.surfaces.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "No objects yet.";
    ul.appendChild(li);
  }
  for (const s of room.surfaces) {
    const li = document.createElement("li");
    if (s.id === selectedId) li.classList.add("active");
    const left = document.createElement("div");
    const name = document.createElement("div");
    name.textContent = s.label;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = s.enrichment.material || (s.cv_guess ? `Scan says: ${s.cv_guess.material}` : s.category);
    left.append(name, meta);
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.dataset.source = s.enrichment.source;
    badge.textContent = SOURCE_LABELS[s.enrichment.source] || s.enrichment.source;
    li.append(left, badge);
    li.addEventListener("click", () => select(s.id));
    ul.appendChild(li);
  }
}

function updateProgress() {
  const done = room.surfaces.filter(s => s.enrichment.source !== "unmeasured").length;
  document.getElementById("progress").textContent = `${done} / ${room.surfaces.length} objects enriched`;
}

const EMPTY_DETAIL = '<p class="muted empty">Hover over an object in the 3D view to see its data. Click it to edit.</p>';

function deselect() {
  if (!selectedId) return;
  selectedId = null;
  rebuildBoxes();
  renderList();
  document.getElementById("detail").innerHTML = EMPTY_DETAIL;
  document.getElementById("hover-card").hidden = true;
  document.getElementById("detail").hidden = false;
}

function select(id) {
  if (id === selectedId) { deselect(); return; }
  selectedId = id;
  rebuildBoxes();
  renderList();
  renderDetail();
  document.getElementById("hover-card").hidden = true;
  document.getElementById("detail").hidden = false;
}

// ---------- detail panel ----------
function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function renderDetail() {
  const s = room.surfaces.find(x => x.id === selectedId);
  const panel = document.getElementById("detail");
  panel.replaceChildren(document.getElementById("detail-template").content.cloneNode(true));
  const e = s.enrichment;

  panel.querySelector(".d-label").textContent = s.label;
  const badge = panel.querySelector(".d-source");
  badge.dataset.source = e.source;
  badge.textContent = SOURCE_LABELS[e.source] || e.source;
  const cv = s.cv_guess;
  panel.querySelector(".d-cv").textContent = cv
    ? `Scan says: ${s.category} · ${cv.material ?? "unknown"}` + (cv.confidence != null ? ` (${Math.round(cv.confidence * 100)}% confidence)` : "")
    : "Placed by hand in the 3D view.";

  renderRecordings(panel.querySelector(".d-recordings"), s, true);
  renderPredictions(panel.querySelector(".d-preds"), s);
  const form = panel.querySelector(".d-form");

  // Box geometry: edits move the box in the 3D view straight away.
  const geomFields = ["label", "category", "cx", "cy", "cz", "sx", "sy", "sz", "rot"];
  if (s.geometry?.type === "bbox") {
    const g = s.geometry;
    const r = v => Math.round(v * 1000) / 1000;
    form.elements.label.value = s.label;
    form.elements.category.value = s.category;
    [form.elements.cx.value, form.elements.cy.value, form.elements.cz.value] = g.center_m.map(r);
    [form.elements.sx.value, form.elements.sy.value, form.elements.sz.value] = g.size_m.map(r);
    form.elements.rot.value = g.rotation_deg || 0;
  }
  const readGeometry = () => {
    const n = name => Number(form.elements[name].value) || 0;
    return { type: "bbox", center_m: [n("cx"), n("cy"), n("cz")],
      size_m: [Math.max(n("sx"), 0.02), Math.max(n("sy"), 0.02), Math.max(n("sz"), 0.02)], rotation_deg: n("rot") };
  };
  const hand = !!s.user_placed;
  for (const f of geomFields) {
    form.elements[f].disabled = !hand;
    if (hand && !["label", "category"].includes(f)) {
      form.elements[f].addEventListener("input", () => {
        const group = boxGroups.get(s.id);
        if (group) { applyBoxGeometry(group, readGeometry()); render(); }
      });
    }
  }
  const delBtn = panel.querySelector(".d-delete");
  delBtn.hidden = !hand;
  delBtn.addEventListener("click", async () => {
    if (!confirm(`Delete "${s.label}"?`)) return;
    await fetch(`/api/surfaces/${encodeURIComponent(s.id)}`, { method: "DELETE" });
    selectedId = null;
    await reloadRoom();
    document.getElementById("detail").innerHTML = EMPTY_DETAIL;
  });

  const presetSel = form.elements.preset;
  for (const p of presets) {
    const opt = document.createElement("option");
    opt.value = p.key; opt.textContent = p.label;
    presetSel.appendChild(opt);
  }

  for (const f of ["material", "fabric", "friction_coeff", "density_kg_m3", "thickness_mm", "source", "notes"]) {
    form.elements[f].value = e[f] ?? "";
  }

  const grid = form.querySelector(".alpha-grid");
  for (const band of BANDS) {
    const label = document.createElement("label");
    label.textContent = `${BAND_LABELS[band]} Hz`;
    const input = document.createElement("input");
    Object.assign(input, { type: "number", step: "0.01", min: "0", max: "1", name: `alpha_${band}` });
    input.value = e.absorption_alpha[band] ?? "";
    input.addEventListener("input", () => drawAlphaChart(form));
    label.appendChild(input);
    grid.appendChild(label);
  }
  drawAlphaChart(form);

  presetSel.addEventListener("change", () => {
    const p = presets.find(x => x.key === presetSel.value);
    if (!p) return;
    form.elements.material.value = p.label;
    form.elements.friction_coeff.value = p.friction_coeff ?? "";
    form.elements.density_kg_m3.value = p.density_kg_m3 ?? "";
    form.elements.thickness_mm.value = p.thickness_mm ?? "";
    for (const band of BANDS) form.elements[`alpha_${band}`].value = p.absorption_alpha[band] ?? "";
    if (form.elements.source.value === "unmeasured") form.elements.source.value = "estimated";
    drawAlphaChart(form);
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const status = form.querySelector(".save-status");
    status.textContent = "Saving…";
    if (hand) {
      const res = await fetch(`/api/surfaces/${encodeURIComponent(s.id)}/geometry`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: form.elements.label.value, category: form.elements.category.value, geometry: readGeometry() }),
      });
      if (!res.ok) { status.textContent = "Save failed"; return; }
    }
    const num = v => (v === "" ? null : Number(v));
    const patch = {
      material: form.elements.material.value || null,
      fabric: form.elements.fabric.value || null,
      friction_coeff: num(form.elements.friction_coeff.value),
      density_kg_m3: num(form.elements.density_kg_m3.value),
      thickness_mm: num(form.elements.thickness_mm.value),
      source: form.elements.source.value,
      notes: form.elements.notes.value,
      absorption_alpha: Object.fromEntries(BANDS.map(b => [b, num(form.elements[`alpha_${b}`].value)])),
    };
    const res = await fetch(`/api/surfaces/${encodeURIComponent(s.id)}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch),
    });
    if (!res.ok) { status.textContent = "Save failed"; return; }
    await reloadRoom();
    const fresh = room.surfaces.find(x => x.id === s.id);
    panel.querySelector(".d-label").textContent = fresh.label;
    badge.dataset.source = fresh.enrichment.source;
    badge.textContent = SOURCE_LABELS[fresh.enrichment.source];
    form.querySelector(".save-status").textContent = `Saved ${new Date().toLocaleTimeString()}`;
  });
}

function drawAlphaChart(form) {
  const svg = form.querySelector(".alpha-chart");
  svg.replaceChildren();
  const W = 300, H = 120, padL = 24, padB = 18, padT = 6;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const plotH = H - padB - padT;
  const slot = (W - padL) / BANDS.length;
  for (const v of [0, 0.5, 1]) {
    const y = padT + plotH * (1 - v);
    svg.appendChild(svgEl("line", { x1: padL, x2: W, y1: y, y2: y, class: "axis" }));
    const t = svgEl("text", { x: padL - 4, y: y + 3, "text-anchor": "end" }); t.textContent = v; svg.appendChild(t);
  }
  BANDS.forEach((band, i) => {
    const raw = form.elements[`alpha_${band}`].value;
    const v = raw === "" ? null : Math.min(1, Math.max(0, Number(raw)));
    const x = padL + i * slot + slot * 0.2;
    if (v != null) {
      const h = plotH * v;
      svg.appendChild(svgEl("rect", { x, y: padT + plotH - h, width: slot * 0.6, height: h, rx: 2, class: "bar" }));
    }
    const t = svgEl("text", { x: x + slot * 0.3, y: H - 4, "text-anchor": "middle" });
    t.textContent = BAND_LABELS[band];
    svg.appendChild(t);
  });
}

/* ---- live panel: the newest measurement, no clicking required ---- */

// Bars for one model, reusing the same row markup as the per-object panel.
function liveModelBlock(model, probs) {
  const block = document.createElement("div");
  block.className = "pred";
  const head = document.createElement("div");
  head.className = "pred-head";
  const name = document.createElement("span");
  name.textContent = model.label;
  const best = document.createElement("span");
  best.className = "muted";
  const top = topMaterial(probs);
  best.textContent = `${MATERIAL_LABELS[top] || top} ${Math.round(probs[top] * 100)}%`;
  head.append(name, best);
  block.appendChild(head);
  const keys = [...MATERIAL_ORDER.filter(k => k in probs),
                ...Object.keys(probs).filter(k => !MATERIAL_ORDER.includes(k))];
  for (const k of keys) {
    const row = document.createElement("div");
    row.className = "pred-row" + (k === top ? " best" : "");
    const label = document.createElement("span");
    label.textContent = MATERIAL_LABELS[k] || k;
    const track = document.createElement("div");
    track.className = "pred-track";
    const bar = document.createElement("div");
    bar.className = "pred-bar";
    bar.style.width = `${Math.max(0, Math.min(1, probs[k])) * 100}%`;
    track.appendChild(bar);
    const pct = document.createElement("span");
    pct.className = "pred-pct";
    pct.textContent = `${Math.round(probs[k] * 100)}%`;
    row.append(label, track, pct);
    block.appendChild(row);
  }
  return block;
}

let liveShownId = null;

function renderLive(rec) {
  const body = document.getElementById("live-body");
  const status = document.getElementById("live-status");
  if (!rec || !rec.id) {
    body.replaceChildren();
    status.textContent = "no measurements yet";
    return;
  }
  // Redraw only on a new measurement, so the photo does not flicker on every poll.
  if (rec.id === liveShownId) return;
  liveShownId = rec.id;

  const m = rec.meta || {};
  status.textContent = m.recorded_at
    ? new Date(m.recorded_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : rec.id;

  body.replaceChildren();
  if (rec.image) {
    const img = document.createElement("img");
    img.className = "live-photo";
    img.src = rec.image;
    img.alt = "Photo from the latest measurement";
    body.appendChild(img);
  }

  const facts = [];
  if (rec.object) facts.push(rec.object);
  if (m.distance_mm != null) facts.push(`${m.distance_mm} mm`);
  if (m.peak_level != null) facts.push(`peak ${m.peak_level} dB`);
  if (facts.length) {
    const p = document.createElement("p");
    p.className = "muted small";
    p.textContent = facts.join(" · ");
    body.appendChild(p);
  }

  const preds = rec.predictions || {};
  const wrap = document.createElement("div");
  wrap.className = "preds";
  let any = false;
  for (const model of MODELS) {
    if (!preds[model.key]) continue;
    wrap.appendChild(liveModelBlock(model, preds[model.key]));
    any = true;
  }
  body.appendChild(wrap);

  if (any && preds.vision && preds.sound) {
    const verdict = document.createElement("p");
    verdict.className = "pred-verdict";
    const tv = topMaterial(preds.vision), ta = topMaterial(preds.sound);
    verdict.textContent = tv === ta
      ? `Both models say ${MATERIAL_LABELS[tv] || tv}.`
      : `Models disagree: camera says ${MATERIAL_LABELS[tv] || tv}, sound says ${MATERIAL_LABELS[ta] || ta}.`;
    body.appendChild(verdict);
  }
}

async function pollLive() {
  try {
    const rec = await (await fetch("/api/latest")).json();
    // Checked before renderLive, which updates liveShownId itself.
    const fresh = rec && rec.id && rec.id !== liveShownId;
    renderLive(rec);
    // A new measurement may also belong to an object; keep that panel current.
    if (fresh && rec.object) await reloadRoom();
  } catch {
    document.getElementById("live-status").textContent = "server unreachable";
  }
}

init();
pollLive();
setInterval(pollLive, 2000);

// ---------- spectrograms drawn from the WAV, so they follow the dark theme ----------
const MAGMA = [[0, 0, 4], [28, 16, 68], [79, 18, 123], [129, 37, 129], [181, 54, 122],
  [229, 80, 100], [251, 135, 97], [254, 194, 135], [252, 253, 191]];
const DB_FLOOR = -90;
const spectroCache = new Map();

function magma(t) {
  const x = Math.min(Math.max(t, 0), 1) * (MAGMA.length - 1);
  const i = Math.min(Math.floor(x), MAGMA.length - 2), f = x - i;
  return MAGMA[i].map((c, k) => c + (MAGMA[i + 1][k] - c) * f);
}

function parseWav(buf) {
  const v = new DataView(buf);
  let pos = 12, rate = 16000, bits = 16, channels = 1;
  while (pos + 8 <= v.byteLength) {
    const id = String.fromCharCode(v.getUint8(pos), v.getUint8(pos + 1), v.getUint8(pos + 2), v.getUint8(pos + 3));
    const size = v.getUint32(pos + 4, true);
    if (id === "fmt ") {
      channels = v.getUint16(pos + 10, true);
      rate = v.getUint32(pos + 12, true);
      bits = v.getUint16(pos + 22, true);
    } else if (id === "data") {
      if (bits !== 16) throw new Error("only 16-bit WAV");
      const n = Math.floor(Math.min(size, v.byteLength - pos - 8) / 2 / channels);
      const out = new Float32Array(n);
      for (let i = 0; i < n; i++) out[i] = v.getInt16(pos + 8 + i * 2 * channels, true) / 32768;
      return { samples: out, rate };
    }
    pos += 8 + size + (size % 2);
  }
  throw new Error("no data chunk");
}

function fftMag2(re, im) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) { [re[i], re[j]] = [re[j], re[i]]; [im[i], im[j]] = [im[j], im[i]]; }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = -2 * Math.PI / len, wr = Math.cos(ang), wi = Math.sin(ang);
    for (let i = 0; i < n; i += len) {
      let cr = 1, ci = 0;
      for (let k = 0; k < len / 2; k++) {
        const a = i + k, b = a + len / 2;
        const tr = re[b] * cr - im[b] * ci, ti = re[b] * ci + im[b] * cr;
        re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
        [cr, ci] = [cr * wr - ci * wi, cr * wi + ci * wr];
      }
    }
  }
  const out = new Float32Array(n / 2 + 1);
  for (let k = 0; k <= n / 2; k++) out[k] = re[k] * re[k] + im[k] * im[k];
  return out;
}

async function computeSpectrogram(url) {
  if (spectroCache.has(url)) return spectroCache.get(url);
  const job = (async () => {
    const { samples, rate } = parseWav(await (await fetch(url)).arrayBuffer());
    const nfft = 1024, hop = 128;
    const win = Float32Array.from({ length: nfft }, (_, i) => 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (nfft - 1)));
    const frames = Math.max(1, Math.floor((samples.length - nfft) / hop) + 1);
    const bins = nfft / 2 + 1;
    const db = new Float32Array(frames * bins);
    let top = -Infinity;
    for (let f = 0; f < frames; f++) {
      const re = new Float32Array(nfft), im = new Float32Array(nfft);
      for (let i = 0; i < nfft; i++) re[i] = (samples[f * hop + i] || 0) * win[i];
      const p = fftMag2(re, im);
      for (let k = 0; k < bins; k++) {
        const v = 10 * Math.log10(Math.max(p[k], 1e-20));
        db[f * bins + k] = v;
        if (v > top) top = v;
      }
    }
    const img = document.createElement("canvas");
    img.width = frames; img.height = bins;
    const ctx = img.getContext("2d");
    const data = ctx.createImageData(frames, bins);
    for (let f = 0; f < frames; f++) {
      for (let k = 0; k < bins; k++) {
        const [r, g, b] = magma((db[f * bins + k] - top - DB_FLOOR) / -DB_FLOOR);
        const o = ((bins - 1 - k) * frames + f) * 4;
        data.data[o] = r; data.data[o + 1] = g; data.data[o + 2] = b; data.data[o + 3] = 255;
      }
    }
    ctx.putImageData(data, 0, 0);
    return { img, samples, rate, duration: samples.length / rate };
  })();
  spectroCache.set(url, job);
  return job;
}

async function drawSpectrogram(canvas, url) {
  const W = 640, H = 300, padL = 40, padR = 8, padT = 8, waveH = 56, gap = 10, padB = 22;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = W * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const styles = getComputedStyle(document.documentElement);
  const muted = styles.getPropertyValue("--muted").trim() || "#9aa1ab";
  const accent = styles.getPropertyValue("--accent").trim() || "#60a5fa";
  const border = styles.getPropertyValue("--border").trim() || "#2b2f37";
  ctx.fillStyle = "#0d0f12";
  ctx.fillRect(0, 0, W, H);
  ctx.font = "10px system-ui, sans-serif";
  ctx.fillStyle = muted;
  ctx.fillText("Loading…", padL, padT + 14);

  let spec;
  try { spec = await computeSpectrogram(url); }
  catch (err) { ctx.fillStyle = "#0d0f12"; ctx.fillRect(0, 0, W, H); ctx.fillStyle = muted; ctx.fillText(`Could not read audio: ${err.message}`, padL, padT + 14); return; }
  ctx.fillStyle = "#0d0f12";
  ctx.fillRect(0, 0, W, H);
  const plotW = W - padL - padR;

  // Waveform as min/max per pixel column.
  const midY = padT + waveH / 2;
  ctx.strokeStyle = border; ctx.beginPath(); ctx.moveTo(padL, midY); ctx.lineTo(padL + plotW, midY); ctx.stroke();
  ctx.fillStyle = accent;
  const s = spec.samples, per = s.length / plotW;
  for (let x = 0; x < plotW; x++) {
    let lo = 0, hi = 0;
    for (let i = Math.floor(x * per); i < Math.floor((x + 1) * per); i++) { if (s[i] < lo) lo = s[i]; if (s[i] > hi) hi = s[i]; }
    ctx.fillRect(padL + x, midY - hi * waveH / 2, 1, Math.max(1, (hi - lo) * waveH / 2));
  }

  // Spectrogram.
  const specTop = padT + waveH + gap, specH = H - specTop - padB;
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(spec.img, padL, specTop, plotW, specH);

  ctx.fillStyle = muted;
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  const nyq = spec.rate / 2;
  for (let f = 0; f <= nyq; f += 2000) ctx.fillText(f === 0 ? "0" : `${f / 1000}k`, padL - 6, specTop + specH * (1 - f / nyq));
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  for (let t = 0; t <= spec.duration + 1e-6; t += 0.2) ctx.fillText(`${t.toFixed(1)}s`, padL + plotW * t / spec.duration, specTop + specH + 6);
  ctx.save();
  ctx.translate(10, specTop + specH / 2); ctx.rotate(-Math.PI / 2);
  ctx.textAlign = "center"; ctx.fillText("Hz", 0, 0);
  ctx.restore();
}
