# Thư viện vendor (không dùng CDN lúc chạy)

| Thư viện | Version | Nguồn | File |
|---|---|---|---|
| three.js | 0.186.1 (r186) | https://cdn.jsdelivr.net/npm/three@0.186.1/ | build/three.module.js, build/three.core.js, examples/jsm/{environments/RoomEnvironment, capabilities/WebGL, loaders/GLTFLoader, utils/BufferGeometryUtils, utils/SkeletonUtils}.js |
| marked | 15.0.12 | https://cdn.jsdelivr.net/npm/marked@15.0.12/ | lib/marked.esm.js |
| DOMPurify | 3.2.6 | https://cdn.jsdelivr.net/npm/dompurify@3.2.6/ | dist/purify.es.mjs |

Nâng cấp: tải lại đúng các file trên ở version mới (giữ nguyên cấu trúc thư mục three/), đọc
https://github.com/mrdoob/three.js/wiki/Migration-Guide, chạy lại test + kiểm tra trang /assistant.
Nhân vật VRM sau này: thêm @pixiv/three-vrm (ESM) vào vendor/ và importmap.
