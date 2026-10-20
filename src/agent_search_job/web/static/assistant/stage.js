// Three.js stage cho /assistant — 1 canvas toàn màn hình, 1 renderer, vẽ từng scene vào vùng
// của #orb-slot và #avatar-slot bằng scissor (threejs.org/manual "Multiple Canvases, Multiple Scenes").
// Nhân vật 3D (character.glb — thỏ mascot công chúa dựng, không kèm clip animation): nạp qua
// setCharacter(), chuyển động procedural theo trạng thái (GLB có clip "idle" thì ưu tiên clip đó):
// - idle: thở nhẹ + đung đưa + xoay chậm
// - thinking: nghiêng người suy nghĩ, lắc chậm
// - speaking: gật đầu theo nhịp token về (speak()) + rung nhẹ
// - poke()/rotate(): công chúa chạm / kéo chuột xoay — nhân vật nhảy vui / quay theo tay
// - tay: idle đung đưa, nghĩ thì giơ 1 tay, nói thì khoát tay theo nhịp, wave() vẫy chào (xuất hiện / trả lời xong)
// Muốn VRM: vendor @pixiv/three-vrm, thêm vào importmap, đăng ký VRMLoaderPlugin trong GLTFLoader
// và gọi vrm.update(dt) mỗi frame (biểu cảm aa/ih/ou/ee/oh dùng cho lip-sync).
import * as THREE from "three";
import WebGL from "three/addons/capabilities/WebGL.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { mergeVertices } from "three/addons/utils/BufferGeometryUtils.js";

const STATES = {
  idle: { scale: 1, noise: 0, hue: 0.03, pulse: 0.02 },
  thinking: { scale: 0.96, noise: 0.06, hue: 0.35, pulse: 0.01 },
  speaking: { scale: 1.04, noise: 0.02, hue: 0.08, pulse: 0.05 },
};
const VERT = `
varying vec3 vPos; varying vec3 vNormal; varying vec3 vView;
void main() {
  vPos = position;
  vNormal = normalize(normalMatrix * normal);
  vec4 mv = modelViewMatrix * vec4(position, 1.0);
  vView = normalize(-mv.xyz);
  gl_Position = projectionMatrix * mv;
}`;
// lõi màu hồng -> tím -> xanh ngọc như ảnh mẫu; vỏ kính khúc xạ lõi này
const CORE_FRAG = `
uniform float uHue; varying vec3 vPos; varying vec3 vNormal; varying vec3 vView;
void main() {
  float y = clamp(vPos.y * 0.55 + 0.5, 0.0, 1.0);
  vec3 pink = vec3(0.96, 0.43, 0.76), lilac = vec3(0.88, 0.64, 0.98), cyan = vec3(0.37, 0.86, 0.84);
  vec3 col = y > 0.45 ? mix(lilac, pink, (y - 0.45) / 0.55) : mix(cyan, lilac, y / 0.45);
  col += pow(1.0 - max(dot(vNormal, vView), 0.0), 2.0) * 0.25;
  // xoay màu quanh trục xám (Rodrigues) theo trạng thái
  const vec3 k = vec3(0.57735);
  float ca = cos(uHue);
  col = col * ca + cross(k, col) * sin(uHue) + k * dot(k, col) * (1.0 - ca);
  gl_FragColor = vec4(col, 1.0);
  #include <colorspace_fragment>
}`;
// vỏ rẻ cho máy yếu: viền fresnel hồng/xanh + điểm sáng, trong suốt
// (màu không nhân thêm f: alpha blending đã nhân alpha rồi, nhân nữa thì viền bị xám trên nền sáng)
const SHELL_FRAG = `
varying vec3 vPos; varying vec3 vNormal; varying vec3 vView;
void main() {
  float f = pow(1.0 - max(dot(vNormal, vView), 0.0), 2.5);
  vec3 rim = mix(vec3(1.0, 0.55, 0.8), vec3(0.45, 0.95, 0.95), 0.5 + 0.5 * vNormal.y);
  float spec = pow(max(dot(reflect(-vView, vNormal), normalize(vec3(-0.4, 0.6, 0.7))), 0.0), 40.0);
  gl_FragColor = vec4(mix(rim, vec3(1.0), spec), clamp(f * 0.9 + spec, 0.0, 1.0));
  #include <colorspace_fragment>
}`;

export function createStage(canvas, { orbSlot, avatarSlot }) {
  if (!WebGL.isWebGL2Available()) return null;
  const lowTier = matchMedia("(pointer: coarse)").matches || (navigator.hardwareConcurrency || 8) <= 4;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, lowTier ? 1.5 : 2));
  renderer.setClearColor(0x000000, 0);
  renderer.setSize(canvas.clientWidth, canvas.clientHeight, false);
  renderer.transmissionResolutionScale = 0.5; // transmission vẽ lại scene vào buffer phụ -> giảm độ phân giải

  // --- Orb: lõi màu (đục) + vỏ kính ---
  const orbScene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  const env = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  pmrem.dispose();
  orbScene.environment = env;
  orbScene.environmentIntensity = 1.3; // r186: material.envMapIntensity bị bỏ qua khi dùng scene.environment
  const orbCam = new THREE.PerspectiveCamera(30, 1, 0.1, 50);
  orbCam.position.set(0, 0, 4.6);
  const coreGeo = new THREE.SphereGeometry(0.8, 64, 32);
  const coreMat = new THREE.ShaderMaterial({ uniforms: { uHue: { value: 0 } }, vertexShader: VERT, fragmentShader: CORE_FRAG });
  const core = new THREE.Mesh(coreGeo, coreMat);
  // Icosahedron là geometry không index -> computeVertexNormals() cho pháp tuyến phẳng từng mặt (điểm sáng bị lốm đốm khi vỏ biến dạng).
  // Bỏ uv (khác nhau ở đường nối) rồi gộp đỉnh trùng để có pháp tuyến mượt; số đỉnh cũng giảm ~6 lần cho vòng lặp nhiễu mỗi frame.
  const shellSrc = new THREE.IcosahedronGeometry(1, lowTier ? 12 : 24);
  shellSrc.deleteAttribute("uv");
  const shellGeo = mergeVertices(shellSrc);
  const basePos = shellGeo.attributes.position.array.slice();
  const shellMat = lowTier
    ? new THREE.ShaderMaterial({ vertexShader: VERT, fragmentShader: SHELL_FRAG, transparent: true, depthWrite: false })
    : new THREE.MeshPhysicalMaterial({
      color: 0xffffff, transmission: 1, thickness: 0.35, ior: 1.45, roughness: 0.04,
      iridescence: 1, iridescenceIOR: 1.3, iridescenceThicknessRange: [100, 800],
      clearcoat: 1, clearcoatRoughness: 0.04, attenuationColor: new THREE.Color(0xf7a8c8),
      attenuationDistance: 2.5,
    });
  const shell = new THREE.Mesh(shellGeo, shellMat);
  const orb = new THREE.Group();
  orb.add(core, shell);
  orbScene.add(orb);

  // --- Ô nhân vật: scene có camera + đèn, hiện trống tới khi setCharacter() ---
  const avatarScene = new THREE.Scene();
  avatarScene.environment = env; // PBR của mascot cần env cho highlight mềm
  avatarScene.environmentIntensity = 0.9;
  avatarScene.add(new THREE.HemisphereLight(0xffffff, 0xf7b7cb, 1.5));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(1.2, 2.4, 2.6);
  avatarScene.add(sun);
  const rimLight = new THREE.DirectionalLight(0xf9b9d9, 1.1); // đèn viền hồng từ sau để mascot nổi trên nền pastel
  rimLight.position.set(-1.6, 1.4, -2.2);
  avatarScene.add(rimLight);
  const avatarCam = new THREE.PerspectiveCamera(30, 1, 0.1, 50);
  avatarCam.position.set(0, 1.15, 4.1);
  avatarCam.lookAt(0, 0.92, 0);
  let character = null, charRoot = null, mixer = null, lost = false, procedural = false;
  let charState = "idle", speakPulse = 0, spinY = 0, spinTarget = 0, hopT = -1, waveT = -1;
  const uArm = { value: new THREE.Vector2() }; // góc giơ tay (rad, > 0 là giơ lên): x = tay trái, y = tay phải

  const cur = { ...STATES.idle };
  let target = STATES.idle, hue = 0;
  const timer = new THREE.Timer();
  timer.connect(document);
  // orbSlot không bắt buộc: widget thỏ ở các trang khác chỉ có nhân vật (canvas nhỏ riêng, avatarSlot = chính canvas)
  const views = [[orbSlot, orbScene, orbCam], [avatarSlot, avatarScene, avatarCam]].filter(([slot]) => slot);

  const frame = () => {
    if (lost) return; // sau khi mất context thì dừng hẳn: không vẽ lại kể cả khi three tự khôi phục context
    timer.update();
    const dt = Math.min(timer.getDelta(), 0.1), t = timer.getElapsed();
    const still = reduced.matches; // giảm chuyển động: chỉ đổi màu theo trạng thái
    if (orbSlot) { // không có orb (widget thỏ) thì khỏi tính biến dạng vỏ mỗi frame
      for (const k of ["scale", "noise", "pulse", "hue"]) cur[k] = still ? target[k] : THREE.MathUtils.damp(cur[k], target[k], 4, dt);
      hue = still ? target.hue * 2 : hue + cur.hue * dt;
      coreMat.uniforms.uHue.value = Math.sin(hue) * 0.35;
      orb.scale.setScalar(cur.scale * (1 + (still ? 0 : cur.pulse * Math.sin(t * 2.4))));
      orb.rotation.y += still ? 0 : dt * 0.15;
      if (!still && cur.noise > 0.002) {
        // biến dạng vỏ khi "đang nghĩ": dời đỉnh theo nhiễu sin theo pháp tuyến
        const p = shellGeo.attributes.position.array;
        for (let i = 0; i < p.length; i += 3) {
          const x = basePos[i], y = basePos[i + 1], z = basePos[i + 2];
          const n = 1 + cur.noise * Math.sin(x * 3 + t * 2) * Math.sin(y * 3 + t * 2.6) * Math.sin(z * 3 + t * 1.4);
          p[i] = x * n; p[i + 1] = y * n; p[i + 2] = z * n;
        }
        shellGeo.attributes.position.needsUpdate = true;
        shellGeo.computeVertexNormals();
      }
    }
    mixer?.update(dt);
    if (!still && charRoot && procedural) {
      // Chuyển động procedural (GLB không có clip): biên độ nhỏ để giữ dáng mascot của công chúa.
      // charRoot là group pivot ở chân nhân vật -> nghiêng/nhảy tự nhiên như đứng trên sàn.
      if (hopT >= 0) hopT += dt;
      const hop = hopT >= 0 && hopT < 0.55 ? Math.sin((hopT / 0.55) * Math.PI) * 0.22 : 0; // poke(): nhảy 1 nhịp 0.55s
      if (hopT >= 0.55) hopT = -1;
      spinY = THREE.MathUtils.damp(spinY, spinTarget, 8, dt);
      const s = charState;
      const breathe = Math.sin(t * (s === "thinking" ? 1.1 : 1.6)) * 0.012; // thở
      const sway = Math.sin(t * (s === "thinking" ? 0.7 : 1.0)) * (s === "speaking" ? 0.05 : 0.03); // lắc lư
      let lean = Math.sin(t * 0.9) * 0.03;
      let nod = 0;
      if (s === "thinking") lean = 0.1 + Math.sin(t * 2.2) * 0.04; // nghiêng người suy nghĩ
      if (s === "speaking") {
        speakPulse = Math.max(0, speakPulse - dt * 3.2);
        nod = speakPulse * 0.1 + Math.sin(t * 6.0) * 0.02; // gật theo nhịp token + rung như đang nói
      }
      charRoot.position.y = breathe + hop;
      charRoot.rotation.set(nod, spinY + Math.sin(t * 0.5) * 0.06, lean + sway * 0.35);
      // tay: đung đưa ngược pha; nghĩ -> giơ tay phải lên; nói -> khoát tay theo nhịp token; wave() -> vẫy 1.8s
      let liftL = 0.05 + Math.sin(t * 1.4) * 0.05, liftR = 0.05 - Math.sin(t * 1.4) * 0.05;
      if (s === "thinking") liftR = 0.5 + Math.sin(t * 2.2) * 0.06;
      if (s === "speaking") {
        liftL = 0.1 + speakPulse * 0.25;
        liftR = 0.15 + Math.sin(t * 5.0) * 0.1 + speakPulse * 0.15;
      }
      if (waveT >= 0) {
        waveT += dt;
        const env = Math.max(0, Math.min(1, waveT / 0.25, (1.8 - waveT) / 0.3)); // giơ lên 0.25s, hạ xuống 0.3s cuối
        // đầu chibi rất to: giơ quá ~0.7 rad là tay lọt vào trong khối đầu, không thấy -> vẫy trong khoảng 0.1..0.7
        liftR = liftR * (1 - env) + env * (0.4 + Math.sin(waveT * 12) * 0.3);
        if (waveT >= 1.8) waveT = -1;
      }
      uArm.value.set(liftL, liftR);
    }
    const c = canvas.getBoundingClientRect(); // toạ độ ô tính theo canvas: canvas toàn trang (trang Trợ lý) hoặc nhỏ (widget)
    renderer.setScissorTest(false);
    renderer.clear();
    renderer.setScissorTest(true);
    for (const [slot, scene, cam] of views) {
      const r = slot.getBoundingClientRect();
      if (r.width < 2 || r.height < 2 || (scene === avatarScene && !character)) continue;
      cam.aspect = r.width / r.height;
      cam.updateProjectionMatrix();
      renderer.setViewport(r.left - c.left, c.bottom - r.bottom, r.width, r.height);
      renderer.setScissor(r.left - c.left, c.bottom - r.bottom, r.width, r.height);
      renderer.render(scene, cam);
    }
  };
  const play = () => renderer.setAnimationLoop(document.hidden || reduced.matches ? null : frame);
  // Quan sát cả 2 ô: orb co lại khi bắt đầu chat (CSS transition 0.4s, hoặc đổi tức thì khi giảm chuyển động -> không có
  // transitionend) và ô nhân vật ẩn/hiện theo breakpoint; ở chế độ giảm chuyển động không có vòng lặp nên phải vẽ lại tại đây.
  // setSize xoá canvas -> vẽ ngay sau đó để không trống 1 nhịp (ResizeObserver chạy sau rAF).
  const ro = new ResizeObserver((entries) => {
    const resized = entries.some((e) => e.target === canvas);
    if (resized) renderer.setSize(canvas.clientWidth, canvas.clientHeight, false);
    if (resized || reduced.matches) frame();
  });
  for (const node of [canvas, orbSlot, avatarSlot]) if (node) ro.observe(node);
  const ac = new AbortController(); // gỡ mọi listener một lượt (dispose() hoặc mất context)
  document.addEventListener("visibilitychange", play, { signal: ac.signal });
  reduced.addEventListener("change", () => { play(); frame(); }, { signal: ac.signal });
  // Mất context là kết thúc cho phiên trang này: three tự preventDefault + khởi tạo lại khi webglcontextrestored, nhưng env PMREM
  // (render target) không được dựng lại -> không được để visibilitychange / đổi reduced-motion bật lại vòng lặp.
  canvas.addEventListener("webglcontextlost", () => {
    lost = true;
    renderer.setAnimationLoop(null);
    ro.disconnect();
    ac.abort(); // gỡ visibilitychange + reduced.change (+ chính listener này)
    document.body.classList.remove("webgl-on", "has-character"); // hiện lại orb CSS
    canvas.hidden = true; // canvas mất context bị Chrome vẽ thành ô trắng đục phủ cả trang ở lần repaint kế tiếp
  }, { signal: ac.signal });

  async function setCharacter(url) {
    const { GLTFLoader } = await import("three/addons/loaders/GLTFLoader.js");
    const gltf = await new GLTFLoader().loadAsync(url);
    if (character) { // nạp lại nhân vật khác: dọn cái cũ
      avatarScene.remove(charRoot);
      const dead = [];
      character.traverse((o) => {
        o.geometry?.dispose();
        for (const m of Array.isArray(o.material) ? o.material : o.material ? [o.material] : []) {
          for (const t of [m.map, m.normalMap, m.metalnessMap, m.roughnessMap, m.aoMap, m.emissiveMap]) t?.dispose?.();
          dead.push(m);
        }
      });
      for (const m of dead) m.dispose();
    }
    mixer?.stopAllAction();
    character = gltf.scene;
    // Chuẩn hoá: GLB có thể lệch gốc / khác tỉ lệ (character.glb lượng tử hoá về khối 0..1, gốc ở góc) ->
    // đưa về cao 1.75 (vừa khung camera, chừa chỗ nhảy 0.22), rộng ≤ 1.4, căn giữa x/z, chân tại y=0 làm pivot.
    const box = new THREE.Box3().setFromObject(character);
    const size = box.getSize(new THREE.Vector3());
    character.scale.multiplyScalar(Math.min(1.75 / size.y, 1.4 / Math.max(size.x, size.z)));
    box.setFromObject(character);
    const center = box.getCenter(new THREE.Vector3());
    character.position.x -= center.x;
    character.position.y -= box.min.y;
    character.position.z -= center.z;
    charRoot = new THREE.Group(); // pivot ở chân: nghiêng/ngảy quanh chân chứ không quanh giữa bụng
    charRoot.add(character);
    avatarScene.add(charRoot);
    const idle = gltf.animations.find((a) => /idle/i.test(a.name)) || gltf.animations[0];
    procedural = !idle; // GLB có clip idle -> chơi clip của artist, bỏ procedural
    mixer = idle ? new THREE.AnimationMixer(character) : null;
    mixer?.clipAction(idle).play();
    if (procedural) {
      // Tay cử động được dù GLB là 1 khối liền (không xương/skin): đỉnh nằm ngoài thân trong dải cao của tay được gán
      // trọng số (armW: + tay phải x > 0, - tay trái) rồi vertex shader xoay chúng quanh vai theo uArm. Ngưỡng đo trên
      // character.glb sau chuẩn hoá ở trên: thân rộng ±0.38, tay chìa ra tới ±0.7 ở cao 0.2–0.5, vai ~(±0.40, 0.46);
      // thay nhân vật khác dáng thì chỉnh lại các số này.
      character.updateMatrixWorld(true); // charRoot mới tạo, chưa xoay -> matrixWorld = toạ độ đã chuẩn hoá
      const v = new THREE.Vector3(), { smoothstep } = THREE.MathUtils;
      character.traverse((o) => {
        if (!o.isMesh) return;
        const pos = o.geometry.attributes.position, armW = new Float32Array(pos.count);
        for (let i = 0; i < pos.count; i++) {
          v.fromBufferAttribute(pos, i).applyMatrix4(o.matrixWorld);
          const w = smoothstep(Math.abs(v.x), 0.38, 0.46) * smoothstep(v.y, 0.17, 0.24) * (1 - smoothstep(v.y, 0.5, 0.55));
          armW[i] = v.x > 0 ? w : -w;
        }
        o.geometry.setAttribute("armW", new THREE.BufferAttribute(armW, 1));
        const toLocal = o.matrixWorld.clone().invert(); // shader làm việc trong toạ độ geometry (chỉ khác tỉ lệ đều + dời)
        const shoulderL = new THREE.Vector3(-0.4, 0.46, 0).applyMatrix4(toLocal);
        const shoulderR = new THREE.Vector3(0.4, 0.46, 0).applyMatrix4(toLocal);
        o.material.onBeforeCompile = (sh) => {
          Object.assign(sh.uniforms, { uArm, uShoulderL: { value: shoulderL }, uShoulderR: { value: shoulderR } });
          sh.vertexShader = "attribute float armW;\nuniform vec2 uArm;\nuniform vec3 uShoulderL, uShoulderR;\n" + sh.vertexShader
            .replace("#include <beginnormal_vertex>", `#include <beginnormal_vertex>
              float armA = armW * (armW > 0.0 ? uArm.y : uArm.x); // xoay quanh trục z: tay phải +, tay trái - là giơ lên
              vec3 armP = armW > 0.0 ? uShoulderR : uShoulderL;
              float armC = cos(armA), armS = sin(armA);
              objectNormal = vec3(armC * objectNormal.x - armS * objectNormal.y, armS * objectNormal.x + armC * objectNormal.y, objectNormal.z);`)
            .replace("#include <begin_vertex>", `#include <begin_vertex>
              vec3 armD = transformed - armP;
              transformed = armP + vec3(armC * armD.x - armS * armD.y, armS * armD.x + armC * armD.y, armD.z);`);
        };
      });
      waveT = 0.0001; // vừa xuất hiện: vẫy chào công chúa
    }
    document.body.classList.add("has-character");
    if (reduced.matches) frame();
  }

  function dispose() {
    renderer.setAnimationLoop(null);
    ro.disconnect();
    ac.abort();
    timer.dispose();
    for (const o of [coreGeo, coreMat, shellGeo, shellMat, env]) o.dispose();
    renderer.dispose();
    canvas.hidden = true; // canvas còn giữ frame cuối -> ẩn đi để orb CSS không bị vẽ đè
    document.body.classList.remove("webgl-on", "has-character");
  }

  document.body.classList.add("webgl-on");
  play();
  frame();
  return {
    setState(state) {
      target = STATES[state] || STATES.idle;
      charState = state in STATES ? state : "idle";
      if (state === "speaking") speakPulse = 1;
      if (reduced.matches) frame();
    },
    speak() { speakPulse = 1; }, // 1 token về -> nhân vật gật 1 nhịp
    rotate(dragX) { spinTarget = THREE.MathUtils.clamp(spinTarget + dragX * 0.01, -1.2, 1.2); }, // kéo chuột xoay
    poke() { if (hopT < 0) hopT = 0.0001; }, // chạm -> nhảy 1 nhịp vui
    wave() { if (waveT < 0) waveT = 0.0001; }, // vẫy tay chào (trả lời xong)
    setCharacter,
    dispose,
  };
}
