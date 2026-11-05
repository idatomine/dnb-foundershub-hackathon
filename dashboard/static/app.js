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

  // A click only counts if the pointer barely moved, so orbiting does not select things.
  let down = null;
  renderer.domElement.addEventListener("pointerdown", e => { down = [e.clientX, e.clientY]; });
  renderer.domElement.addEventListener("pointerup", e => {
    if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
    onViewerClick(e);
  });
}

function render() { renderer.render(scene, camera); }

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
  camera.position.set(center.x, center.y - 9, center.z + 5);
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
    boxGroups.set(s.id, group);
    applyBoxGeometry(group, s.geometry);
    scene.add(group);
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

function select(id) {
  selectedId = id;
  rebuildBoxes();
  renderList();
  renderDetail();
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
    ? `Scan says: ${s.category} · ${cv.material ?? "unknown"} (${Math.round((cv.confidence ?? 0) * 100)}% confidence)`
    : "Placed by hand in the 3D view.";

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
    document.getElementById("detail").innerHTML = '<p class="muted empty">Select an object.</p>';
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

init();
