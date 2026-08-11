import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { LightProbeGenerator } from "three/examples/jsm/lights/LightProbeGenerator.js";
import { EffectComposer } from "three/examples/jsm/postprocessing/EffectComposer.js";
import { GTAOPass } from "three/examples/jsm/postprocessing/GTAOPass.js";
import { OutputPass } from "three/examples/jsm/postprocessing/OutputPass.js";
import { RenderPass } from "three/examples/jsm/postprocessing/RenderPass.js";
import { SSRPass } from "three/examples/jsm/postprocessing/SSRPass.js";
import { UnrealBloomPass } from "three/examples/jsm/postprocessing/UnrealBloomPass.js";
import { createIndependentLoopClips } from "@/lib/three/animations";
import { createLitMaterial, createUnlitMaterial } from "@/lib/three/materials";
import type { Track } from "@/tracks";
import {
  drawTextureAnimationFrame,
  loadCubeTexture,
  loadImage,
  loadJson,
  resolveRelativeUrl,
  type RuntimeManifest,
  type SkyboxMetadata,
  type TextureAnimationIndex,
  type TextureAnimationMetadata,
  type TextureAnimator,
} from "@/lib/three/runtimeAssets";

type Props = {
  track: Track;
  onProgress: (progress: number) => void;
  onReady: (animations: number) => void;
  onError: (message: string) => void;
  onSkyboxReady: () => void;
};

type MobileMovement = {
  forward: number;
  sideways: number;
  vertical: number;
  lookX: number;
  lookY: number;
};

function getSkyboxHorizonColor(texture: THREE.CubeTexture) {
  const faces = texture.image as Array<
    CanvasImageSource & {
      width?: number;
      height?: number;
      naturalWidth?: number;
      naturalHeight?: number;
    }
  >;
  const sideFaces = [faces[0], faces[1], faces[4], faces[5]];
  const canvas = document.createElement("canvas");
  canvas.width = 16;
  canvas.height = 8;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (context === null || sideFaces.some((face) => face === undefined) === true) return null;

  let red = 0;
  let green = 0;
  let blue = 0;
  let pixelCount = 0;

  for (const face of sideFaces) {
    const width = face.naturalWidth ?? face.width ?? 0;
    const height = face.naturalHeight ?? face.height ?? 0;
    if (width === 0 || height === 0) continue;
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.drawImage(
      face,
      0,
      Math.round(height * 0.35),
      width,
      Math.max(1, Math.round(height * 0.3)),
      0,
      0,
      canvas.width,
      canvas.height,
    );
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    for (let index = 0; index < pixels.length; index += 4) {
      red += pixels[index];
      green += pixels[index + 1];
      blue += pixels[index + 2];
      pixelCount += 1;
    }
  }

  if (pixelCount === 0) return null;
  return new THREE.Color().setRGB(
    red / pixelCount / 255,
    green / pixelCount / 255,
    blue / pixelCount / 255,
    THREE.SRGBColorSpace,
  );
}

function readCameraSnapshot() {
  const params = new URLSearchParams(window.location.search);
  const position = params.get("p")?.split(",").map(Number);
  const rotation = params.get("r")?.split(",").map(Number);
  const fov = Number(params.get("fov"));
  if (
    position === undefined ||
    rotation === undefined ||
    position.length !== 3 ||
    rotation.length !== 3 ||
    position.every(Number.isFinite) === false ||
    rotation.every(Number.isFinite) === false
  )
    return null;
  return { position, rotation, fov: Number.isFinite(fov) === true ? fov : 75 };
}

async function copyTextToClipboard(text: string) {
  if (window.isSecureContext === true && navigator.clipboard !== undefined) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // The selection-based fallback below also works on local HTTP addresses.
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.readOnly = true;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  textarea.setSelectionRange(0, text.length);
  const copied = document.execCommand("copy");
  textarea.remove();
  return copied;
}

export function useTrackViewer({ track, onProgress, onReady, onError, onSkyboxReady }: Props) {
  const runtimeUrl = `/tracks/${track.id}/runtime.json`;
  const [, setSearchParams] = useSearchParams();
  const mountRef = useRef<HTMLDivElement>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const [moveSpeed, setMoveSpeed] = useState(60);
  const [showSpeedIndicator, setShowSpeedIndicator] = useState(false);
  const [snapshotCopied, setSnapshotCopied] = useState(false);
  const [cameraNotice, setCameraNotice] = useState<string | null>(null);
  const [enhancedGraphics, setEnhancedGraphics] = useState(false);
  const speedIndicatorTimeoutRef = useRef<number | null>(null);
  const cameraNoticeTimeoutRef = useRef<number | null>(null);
  const moveSpeedRef = useRef(60);
  const cameraFovRef = useRef(75);
  const enhancedGraphicsRef = useRef(false);
  const applyEnhancedGraphicsRef = useRef<(enabled: boolean) => void>(() => undefined);
  const mobileMovementRef = useRef<MobileMovement>({
    forward: 0,
    sideways: 0,
    vertical: 0,
    lookX: 0,
    lookY: 0,
  });

  const setMobileMove = useCallback((sideways: number, forward: number) => {
    mobileMovementRef.current.sideways = sideways;
    mobileMovementRef.current.forward = forward;
  }, []);

  const setMobileLook = useCallback((lookX: number, lookY: number) => {
    mobileMovementRef.current.lookX = lookX;
    mobileMovementRef.current.lookY = lookY;
  }, []);

  const setMobileVertical = useCallback((vertical: number) => {
    mobileMovementRef.current.vertical = vertical;
  }, []);

  const toggleEnhancedGraphics = useCallback(() => {
    setEnhancedGraphics((enabled) => {
      const nextEnabled = enabled === false;
      enhancedGraphicsRef.current = nextEnabled;
      applyEnhancedGraphicsRef.current(nextEnabled);
      return nextEnabled;
    });
  }, []);

  const updateMoveSpeed = (value: number) => {
    const nextSpeed = THREE.MathUtils.clamp(Math.round(value), 5, 200);
    moveSpeedRef.current = nextSpeed;
    setMoveSpeed(nextSpeed);
  };

  const updateCameraFov = (value: number) => {
    const nextFov = THREE.MathUtils.clamp(Math.round(value), 40, 110);
    cameraFovRef.current = nextFov;
  };

  const showCameraNotice = (message: string) => {
    if (cameraNoticeTimeoutRef.current !== null)
      window.clearTimeout(cameraNoticeTimeoutRef.current);
    setCameraNotice(message);
    cameraNoticeTimeoutRef.current = window.setTimeout(() => {
      setCameraNotice(null);
      cameraNoticeTimeoutRef.current = null;
    }, 1800);
  };

  const createCameraSnapshot = async () => {
    const camera = cameraRef.current;
    if (camera === null) return;
    const formatPosition = (value: number) => value.toFixed(4);
    const formatRotation = (value: number) => value.toFixed(6);
    const params = new URLSearchParams(window.location.search);
    params.set("p", camera.position.toArray().map(formatPosition).join(","));
    params.set(
      "r",
      [camera.rotation.x, camera.rotation.y, camera.rotation.z].map(formatRotation).join(","),
    );
    params.set("fov", String(Math.round(camera.fov)));
    setSearchParams(params, { replace: true });

    const url = new URL(window.location.href);
    url.search = params.toString();
    const copied = await copyTextToClipboard(url.toString());
    if (copied === true) {
      setSnapshotCopied(true);
      showCameraNotice("Skopiowano link do obecnego widoku");
      window.setTimeout(() => setSnapshotCopied(false), 1800);
      return;
    }
    setSnapshotCopied(false);
  };

  const resetCamera = () => {
    const camera = cameraRef.current;
    const initialView = track.cameraStart;
    if (camera === null) return;
    camera.position.fromArray(initialView.position);
    camera.rotation.set(
      initialView.rotation[0],
      initialView.rotation[1],
      initialView.rotation[2],
      "YXZ",
    );
    cameraFovRef.current = initialView.fov;
    camera.fov = initialView.fov;
    camera.updateProjectionMatrix();

    const params = new URLSearchParams(window.location.search);
    params.delete("p");
    params.delete("r");
    params.delete("fov");
    setSearchParams(params, { replace: true });
    showCameraNotice("Przywrócono początkową pozycję kamery");
  };

  useEffect(() => {
    const mount = mountRef.current;
    if (mount === null) return;

    let disposed = false;
    let frame = 0;
    let mixer: THREE.AnimationMixer | undefined;
    let animationElapsed = 0;
    let baseMoveSpeed = 10;
    const pressedKeys = new Set<string>();
    const movement = new THREE.Vector3();
    const forward = new THREE.Vector3();
    const right = new THREE.Vector3();
    const worldUp = new THREE.Vector3(0, 1, 0);
    const sunOffset = new THREE.Vector3(50, 75, 37.5);
    const textureAnimators: TextureAnimator[] = [];
    const runtimeTextures: THREE.Texture[] = [];
    const aoExcludedMeshes = new Set<THREE.Mesh>();
    const waterMeshes: THREE.Mesh[] = [];
    const materialVariants = new Map<
      THREE.Mesh,
      { unlit: THREE.MeshBasicMaterial[]; lit: THREE.MeshLambertMaterial[]; usesArray: boolean }
    >();
    let runtimeManifest: RuntimeManifest | undefined;
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(cameraFovRef.current, 1, 0.1, 10000);
    cameraRef.current = camera;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    const maximumAnisotropy = Math.min(renderer.capabilities.getMaxAnisotropy(), 8);
    const maximumPixelRatio = window.matchMedia("(pointer: coarse)").matches === true ? 1.5 : 2;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, maximumPixelRatio));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1;
    renderer.shadowMap.enabled = false;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    mount.appendChild(renderer.domElement);

    const skyIntensity = track.lighting?.skyIntensity ?? 0.08;
    const environmentIntensity = track.lighting?.environmentIntensity ?? 0.08;
    const skyLight = new THREE.HemisphereLight(0xffffff, 0x35405a, skyIntensity);
    const environmentLight = new THREE.LightProbe();
    environmentLight.intensity = environmentIntensity;
    const enhancedFog = new THREE.Fog(0x74808f, 50, 400);
    const sun = new THREE.DirectionalLight(0xfff1d2, track.lighting?.sunIntensity ?? 1.6);
    sun.castShadow = true;
    sun.shadow.mapSize.set(4096, 4096);
    sun.shadow.radius = 1.5;
    scene.add(skyLight, environmentLight, sun, sun.target);

    const applyEnhancedGraphics = (enabled: boolean) => {
      renderer.shadowMap.enabled = enabled;
      scene.fog = enabled === true ? enhancedFog : null;
      materialVariants.forEach(({ unlit, lit, usesArray }, mesh) => {
        const current = (
          Array.isArray(mesh.material) === true ? mesh.material : [mesh.material]
        ) as Array<THREE.MeshBasicMaterial | THREE.MeshLambertMaterial>;
        const target = enabled === true ? lit : unlit;
        target.forEach((material, index) => {
          const source = current[index];
          if (source === undefined) return;
          material.map = source.map;
          material.alphaMap = source.alphaMap;
          material.needsUpdate = true;
        });
        mesh.material = usesArray === true ? target : target[0];
      });
    };
    applyEnhancedGraphicsRef.current = applyEnhancedGraphics;

    const composer = new EffectComposer(renderer);
    composer.setPixelRatio(renderer.getPixelRatio());
    const renderPass = new RenderPass(scene, camera);
    const gtaoPass = new GTAOPass(scene, camera, 1, 1);
    gtaoPass.updateGtaoMaterial({
      radius: 0.22,
      distanceExponent: 1.5,
      thickness: 1,
      distanceFallOff: 1,
      scale: 1,
      samples: 16,
      screenSpaceRadius: true,
    });
    gtaoPass.updatePdMaterial({ radius: 3, rings: 2, samples: 8 });
    gtaoPass.blendIntensity = 0.35;
    const renderGtao = gtaoPass.render.bind(gtaoPass);
    gtaoPass.render = (...args: Parameters<GTAOPass["render"]>) => {
      const visibility = new Map<THREE.Mesh, boolean>();
      aoExcludedMeshes.forEach((mesh) => {
        visibility.set(mesh, mesh.visible);
        mesh.visible = false;
      });
      try {
        renderGtao(...args);
      } finally {
        visibility.forEach((visible, mesh) => {
          mesh.visible = visible;
        });
      }
    };
    const outputPass = new OutputPass();
    const bloomPass = new UnrealBloomPass(
      new THREE.Vector2(1, 1),
      track.bloom?.strength ?? 0.28,
      0.35,
      track.bloom?.threshold ?? 0.82,
    );
    const ssrPass = new SSRPass({
      renderer,
      scene,
      camera,
      width: 1,
      height: 1,
      selects: waterMeshes,
      groundReflector: null,
    });
    ssrPass.opacity = 0.4;
    ssrPass.maxDistance = 80;
    ssrPass.thickness = 0.025;
    ssrPass.blur = true;
    ssrPass.distanceAttenuation = true;
    ssrPass.fresnel = true;
    ssrPass.resolutionScale = 0.65;
    ssrPass.enabled = false;
    composer.addPass(renderPass);
    composer.addPass(ssrPass);
    composer.addPass(gtaoPass);
    composer.addPass(bloomPass);
    composer.addPass(outputPass);

    let isDragging = false;
    camera.rotation.order = "YXZ";

    const stopDragging = () => {
      if (document.pointerLockElement === renderer.domElement) document.exitPointerLock();
      isDragging = false;
      renderer.domElement.style.cursor = "grab";
    };
    const onWindowBlur = () => {
      stopDragging();
      pressedKeys.clear();
    };
    const onPointerDown = (event: PointerEvent) => {
      if (event.button !== 0 || event.pointerType !== "mouse") return;
      renderer.domElement.requestPointerLock();
    };
    const onPointerLockChange = () => {
      isDragging = document.pointerLockElement === renderer.domElement;
      renderer.domElement.style.cursor = isDragging === true ? "none" : "grab";
    };
    const onPointerMove = (event: MouseEvent) => {
      if (isDragging === false || document.pointerLockElement !== renderer.domElement) return;
      const sensitivity = 0.006;
      camera.rotation.y -= event.movementX * sensitivity;
      camera.rotation.x = THREE.MathUtils.clamp(
        camera.rotation.x - event.movementY * sensitivity,
        -Math.PI / 2 + 0.01,
        Math.PI / 2 - 0.01,
      );
    };
    const onPointerUp = (event: MouseEvent) => {
      if (event.button === 0) stopDragging();
    };
    const onContextMenu = (event: MouseEvent) => event.preventDefault();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement === true) return;
      pressedKeys.add(event.code);
    };
    const onKeyUp = (event: KeyboardEvent) => pressedKeys.delete(event.code);
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      updateMoveSpeed(moveSpeedRef.current + (event.deltaY < 0 ? 5 : -5));
      setShowSpeedIndicator(true);
      if (speedIndicatorTimeoutRef.current !== null)
        window.clearTimeout(speedIndicatorTimeoutRef.current);
      if (cameraNoticeTimeoutRef.current !== null)
        window.clearTimeout(cameraNoticeTimeoutRef.current);
      speedIndicatorTimeoutRef.current = window.setTimeout(() => {
        setShowSpeedIndicator(false);
        speedIndicatorTimeoutRef.current = null;
      }, 1200);
    };
    renderer.domElement.style.cursor = "grab";
    renderer.domElement.addEventListener("pointerdown", onPointerDown);
    renderer.domElement.addEventListener("contextmenu", onContextMenu);
    renderer.domElement.addEventListener("wheel", onWheel, { passive: false });
    document.addEventListener("mousemove", onPointerMove);
    document.addEventListener("mouseup", onPointerUp);
    document.addEventListener("pointerlockchange", onPointerLockChange);
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onWindowBlur);

    const prepareSkybox = async () => {
      try {
        runtimeManifest = await loadJson<RuntimeManifest>(runtimeUrl);
        if (runtimeManifest.skybox === undefined || runtimeManifest.skybox.length === 0) return;

        const skyboxUrl = resolveRelativeUrl(runtimeUrl, runtimeManifest.skybox);
        const skybox = await loadJson<SkyboxMetadata>(skyboxUrl);
        const faceUrl = (face: keyof SkyboxMetadata["faces"]) =>
          resolveRelativeUrl(skyboxUrl, skybox.faces[face].file);
        const cubeTexture = await loadCubeTexture(
          [
            faceUrl("RT"),
            faceUrl("LF"),
            faceUrl("UP"),
            faceUrl("DN"),
            faceUrl("FR"),
            faceUrl("BK"),
          ],
          [0, 1, 4, 5],
        );
        if (disposed === true) return cubeTexture.dispose();
        cubeTexture.colorSpace = THREE.SRGBColorSpace;
        environmentLight.copy(LightProbeGenerator.fromCubeTexture(cubeTexture));
        environmentLight.intensity = environmentIntensity;
        const horizonColor = getSkyboxHorizonColor(cubeTexture);
        if (horizonColor !== null) enhancedFog.color.copy(horizonColor);
        runtimeTextures.push(cubeTexture);
        if (scene.background instanceof THREE.Texture === true) scene.background.dispose();
        scene.background = cubeTexture;
        scene.environment = cubeTexture;
        onSkyboxReady();
      } catch (skyboxError) {
        console.warn("Nie udało się wczytać skyboxa przed modelem.", skyboxError);
      }
    };

    const setupRuntimeAssets = async (model: THREE.Object3D) => {
      try {
        const runtime = runtimeManifest ?? (await loadJson<RuntimeManifest>(runtimeUrl));

        if (runtime.textureAnimations !== undefined && runtime.textureAnimations.length > 0) {
          const indexUrl = resolveRelativeUrl(runtimeUrl, runtime.textureAnimations);
          const index = await loadJson<TextureAnimationIndex>(indexUrl);

          for (const entry of index.animations) {
            const metadataUrl = resolveRelativeUrl(indexUrl, entry.metadata);
            const metadata = await loadJson<TextureAnimationMetadata>(metadataUrl);
            const atlasImage = await loadImage(
              resolveRelativeUrl(metadataUrl, metadata.atlas.file),
            );
            if (disposed === true) return;
            const canvas = document.createElement("canvas");
            canvas.width = metadata.atlas.cellWidth;
            canvas.height = metadata.atlas.cellHeight;
            const context = canvas.getContext("2d")!;
            const frameTexture = new THREE.CanvasTexture(canvas);
            frameTexture.colorSpace = THREE.SRGBColorSpace;
            frameTexture.flipY = false;
            frameTexture.wrapS = THREE.RepeatWrapping;
            frameTexture.wrapT = THREE.RepeatWrapping;
            frameTexture.generateMipmaps = true;
            frameTexture.minFilter = THREE.LinearMipmapLinearFilter;
            frameTexture.magFilter = THREE.LinearFilter;
            frameTexture.anisotropy = maximumAnisotropy;
            runtimeTextures.push(frameTexture);

            const materialName = `mat_${String(metadata.materialIndex).padStart(3, "0")}`;
            let matchedMaterialCount = 0;
            model.traverse((object) => {
              if (object instanceof THREE.Mesh === false) return;
              const materials =
                Array.isArray(object.material) === true ? object.material : [object.material];
              materials.forEach((material) => {
                if (
                  material.name !== materialName ||
                  (material instanceof THREE.MeshBasicMaterial === false &&
                    material instanceof THREE.MeshLambertMaterial === false)
                )
                  return;
                material.map = frameTexture;
                material.needsUpdate = true;
                matchedMaterialCount += 1;
              });
            });

            if (matchedMaterialCount > 0) {
              const animator: TextureAnimator = {
                texture: frameTexture,
                context,
                atlasImage,
                cellWidth: metadata.atlas.cellWidth,
                cellHeight: metadata.atlas.cellHeight,
                currentFrame: -1,
                tickRateHz: metadata.tickRateHz,
                cycleTicks: metadata.cycleTicks,
                tickFrames: metadata.tickFrames,
                frames: metadata.frames,
              };
              drawTextureAnimationFrame(animator, metadata.tickFrames[0] ?? 0);
              textureAnimators.push(animator);
            }
          }
        }
      } catch (runtimeError) {
        console.warn("Nie udało się uruchomić zasobów ToonCar runtime.", runtimeError);
      }
    };

    const loadModel = () =>
      new GLTFLoader().load(
        track.modelUrl,
        (gltf) => {
          if (disposed === true) return;
          const model = gltf.scene;
          model.traverse((object) => {
            if (object instanceof THREE.Mesh === true) {
              const sourceMaterials =
                Array.isArray(object.material) === true ? object.material : [object.material];
              const unlitMaterials = sourceMaterials.map((material) =>
                createUnlitMaterial(material, maximumAnisotropy),
              );
              const litMaterials = unlitMaterials.map(createLitMaterial);
              const usesWaterTexture = unlitMaterials.some(
                (material) => material.map?.name.toLowerCase().includes("agua") === true,
              );
              const usesArray = Array.isArray(object.material) === true;
              materialVariants.set(object, {
                unlit: unlitMaterials,
                lit: litMaterials,
                usesArray,
              });
              const initialMaterials =
                enhancedGraphicsRef.current === true ? litMaterials : unlitMaterials;
              object.material = usesArray === true ? initialMaterials : initialMaterials[0];
              if (
                unlitMaterials.some(
                  (material) => material.transparent === true || material.alphaTest > 0,
                ) === true
              )
                aoExcludedMeshes.add(object);
              if (usesWaterTexture === true) waterMeshes.push(object);
              sourceMaterials.forEach((material) => material.dispose());
              object.castShadow = true;
              object.receiveShadow = true;
            }
          });
          scene.add(model);
          ssrPass.enabled = waterMeshes.length > 0;
          void setupRuntimeAssets(model);

          const box = new THREE.Box3().setFromObject(model);
          const center = box.getCenter(new THREE.Vector3());
          const size = box.getSize(new THREE.Vector3());
          const largestDimension = Math.max(size.x, size.y, size.z);
          const radius = largestDimension > 0 ? largestDimension : 10;
          enhancedFog.near = radius * 0.15;
          enhancedFog.far = radius;
          ssrPass.maxDistance = Math.max(radius * 0.3, 40);
          sunOffset.set(radius * 0.75, radius * 0.65, radius * 0.5);
          sun.position.copy(camera.position).add(sunOffset);
          sun.target.position.copy(camera.position);
          const shadowRange = radius * 0.65;
          sun.shadow.camera.left = -shadowRange;
          sun.shadow.camera.right = shadowRange;
          sun.shadow.camera.top = shadowRange;
          sun.shadow.camera.bottom = -shadowRange;
          sun.shadow.camera.near = Math.max(radius * 0.01, 0.1);
          sun.shadow.camera.far = radius * 4;
          sun.shadow.bias = -0.0004;
          sun.shadow.normalBias = 0.002;
          sun.shadow.camera.updateProjectionMatrix();
          applyEnhancedGraphics(enhancedGraphicsRef.current);
          baseMoveSpeed = Math.max(radius * 0.15, 1);
          camera.position
            .copy(center)
            .add(new THREE.Vector3(radius * 0.3, radius * 0.15, radius * 0.3));
          camera.lookAt(center);
          camera.rotation.order = "YXZ";
          const initialView = readCameraSnapshot() ?? track.cameraStart;
          camera.position.fromArray(initialView.position);
          camera.rotation.set(
            initialView.rotation[0],
            initialView.rotation[1],
            initialView.rotation[2],
            "YXZ",
          );
          updateCameraFov(initialView.fov);
          camera.fov = cameraFovRef.current;
          camera.near = Math.max(radius / 1000, 0.1);
          camera.far = radius * 20;
          camera.updateProjectionMatrix();

          try {
            const loopClips = createIndependentLoopClips(gltf.animations);
            mixer = new THREE.AnimationMixer(model);
            loopClips.forEach((clip) => {
              mixer?.clipAction(clip).setLoop(THREE.LoopRepeat, Infinity).play();
            });
            onProgress(100);
            onReady(loopClips.length);
          } catch (animationError) {
            console.error(animationError);
            onError(
              animationError instanceof Error
                ? animationError.message
                : "Nie udało się przygotować animacji trasy.",
            );
          }
        },
        (event) => onProgress(event.total > 0 ? Math.round((event.loaded / event.total) * 100) : 0),
        () => onError("Nie udało się wczytać modelu trasy."),
      );

    void prepareSkybox().finally(() => {
      if (disposed === false) loadModel();
    });

    const resize = () => {
      const { clientWidth, clientHeight } = mount;
      renderer.setSize(clientWidth, clientHeight, false);
      composer.setSize(clientWidth, clientHeight);
      camera.aspect = clientWidth / Math.max(clientHeight, 1);
      camera.updateProjectionMatrix();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(mount);
    resize();

    const clock = new THREE.Clock();
    const render = () => {
      frame = requestAnimationFrame(render);
      const delta = Math.min(clock.getDelta(), 0.1);
      const mobileMovement = mobileMovementRef.current;
      if (camera.fov !== cameraFovRef.current) {
        camera.fov = cameraFovRef.current;
        camera.updateProjectionMatrix();
      }
      animationElapsed += delta;
      mixer?.update(delta);
      if (mobileMovement.lookX !== 0 || mobileMovement.lookY !== 0) {
        const mobileLookSpeed = 3.2;
        camera.rotation.y -= mobileMovement.lookX * mobileLookSpeed * delta;
        camera.rotation.x = THREE.MathUtils.clamp(
          camera.rotation.x - mobileMovement.lookY * mobileLookSpeed * delta,
          -Math.PI / 2 + 0.01,
          Math.PI / 2 - 0.01,
        );
      }
      if (
        pressedKeys.size > 0 ||
        mobileMovement.forward !== 0 ||
        mobileMovement.sideways !== 0 ||
        mobileMovement.vertical !== 0
      ) {
        const sprintMultiplier =
          pressedKeys.has("ShiftLeft") === true || pressedKeys.has("ShiftRight") === true ? 2 : 1;
        const distance = baseMoveSpeed * (moveSpeedRef.current / 50) * sprintMultiplier * delta;
        movement.set(0, 0, 0);
        camera.getWorldDirection(forward);
        right.set(1, 0, 0).applyQuaternion(camera.quaternion);
        if (pressedKeys.has("KeyW") === true) movement.add(forward);
        if (pressedKeys.has("KeyS") === true) movement.sub(forward);
        if (pressedKeys.has("KeyA") === true) movement.sub(right);
        if (pressedKeys.has("KeyD") === true) movement.add(right);
        if (pressedKeys.has("KeyQ") === true) movement.sub(worldUp);
        if (pressedKeys.has("KeyE") === true) movement.add(worldUp);
        movement.addScaledVector(forward, mobileMovement.forward);
        movement.addScaledVector(right, mobileMovement.sideways);
        movement.addScaledVector(worldUp, mobileMovement.vertical);
        if (movement.lengthSq() > 0) {
          const inputStrength = Math.min(movement.length(), 1);
          camera.position.addScaledVector(movement.normalize(), distance * inputStrength);
        }
      }
      textureAnimators.forEach((animator) => {
        const tick = Math.floor(animationElapsed * animator.tickRateHz) % animator.cycleTicks;
        drawTextureAnimationFrame(animator, animator.tickFrames[tick] ?? 0);
      });
      if (enhancedGraphicsRef.current === true) {
        sun.position.copy(camera.position).add(sunOffset);
        sun.target.position.copy(camera.position);
        sun.target.updateMatrixWorld();
      }
      if (enhancedGraphicsRef.current === true) composer.render(delta);
      else renderer.render(scene, camera);
    };
    render();

    return () => {
      disposed = true;
      if (speedIndicatorTimeoutRef.current !== null)
        window.clearTimeout(speedIndicatorTimeoutRef.current);
      cameraRef.current = null;
      if (document.pointerLockElement === renderer.domElement) document.exitPointerLock();
      cancelAnimationFrame(frame);
      observer.disconnect();
      renderer.domElement.removeEventListener("pointerdown", onPointerDown);
      renderer.domElement.removeEventListener("contextmenu", onContextMenu);
      renderer.domElement.removeEventListener("wheel", onWheel);
      document.removeEventListener("mousemove", onPointerMove);
      document.removeEventListener("mouseup", onPointerUp);
      document.removeEventListener("pointerlockchange", onPointerLockChange);
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onWindowBlur);
      mixer?.stopAllAction();
      scene.traverse((object) => {
        if (object instanceof THREE.Mesh === true) {
          object.geometry?.dispose();
        }
      });
      materialVariants.forEach(({ unlit, lit }) => {
        unlit.forEach((material) => material.dispose());
        lit.forEach((material) => material.dispose());
      });
      if (scene.background instanceof THREE.Texture === true) scene.background.dispose();
      runtimeTextures.forEach((texture) => texture.dispose());
      gtaoPass.dispose();
      ssrPass.dispose();
      bloomPass.dispose();
      renderPass.dispose();
      outputPass.dispose();
      composer.dispose();
      renderer.dispose();
      renderer.domElement.remove();
      applyEnhancedGraphicsRef.current = () => undefined;
      mobileMovementRef.current = {
        forward: 0,
        sideways: 0,
        vertical: 0,
        lookX: 0,
        lookY: 0,
      };
    };
  }, [track, onError, onProgress, onReady, onSkyboxReady]);

  return {
    mountRef,
    moveSpeed,
    showSpeedIndicator,
    snapshotCopied,
    cameraNotice,
    enhancedGraphics,
    createCameraSnapshot,
    resetCamera,
    toggleEnhancedGraphics,
    setMobileMove,
    setMobileLook,
    setMobileVertical,
  };
}
