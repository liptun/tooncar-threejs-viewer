import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { createIndependentLoopClips } from "@/lib/three/animations";
import { createUnlitMaterial } from "@/lib/three/materials";
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

type TransformAnimator = {
  target: THREE.Object3D;
  property: "position" | "quaternion" | "scale";
  duration: number;
  track: THREE.KeyframeTrack;
};

type MobileMovement = {
  forward: number;
  sideways: number;
  vertical: number;
  lookX: number;
  lookY: number;
};

const quaternionFrom = new THREE.Quaternion();
const quaternionTo = new THREE.Quaternion();

function applyTransformFrame(animator: TransformAnimator, elapsed: number) {
  const { target, property, duration, track } = animator;
  const time = elapsed % duration;
  const times = track.times;
  const values = track.values;
  let low = 0;
  let high = times.length - 1;

  while (low + 1 < high) {
    const middle = (low + high) >> 1;
    if (times[middle] <= time) low = middle;
    else high = middle;
  }

  const fromIndex = low;
  const toIndex = Math.min(low + 1, times.length - 1);
  const interval = times[toIndex] - times[fromIndex];
  const alpha = interval > 0 ? (time - times[fromIndex]) / interval : 0;
  const valueSize = track.getValueSize();
  const fromOffset = fromIndex * valueSize;
  const toOffset = toIndex * valueSize;

  if (property === "quaternion") {
    quaternionFrom.fromArray(values, fromOffset);
    quaternionTo.fromArray(values, toOffset);
    target.quaternion.slerpQuaternions(quaternionFrom, quaternionTo, alpha);
    return;
  }

  target[property].set(
    THREE.MathUtils.lerp(values[fromOffset], values[toOffset], alpha),
    THREE.MathUtils.lerp(values[fromOffset + 1], values[toOffset + 1], alpha),
    THREE.MathUtils.lerp(values[fromOffset + 2], values[toOffset + 2], alpha),
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
  const speedIndicatorTimeoutRef = useRef<number | null>(null);
  const cameraNoticeTimeoutRef = useRef<number | null>(null);
  const moveSpeedRef = useRef(60);
  const cameraFovRef = useRef(75);
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
    const transformAnimators: TransformAnimator[] = [];
    let animationElapsed = 0;
    let baseMoveSpeed = 10;
    const pressedKeys = new Set<string>();
    const movement = new THREE.Vector3();
    const forward = new THREE.Vector3();
    const right = new THREE.Vector3();
    const worldUp = new THREE.Vector3(0, 1, 0);
    const textureAnimators: TextureAnimator[] = [];
    const runtimeTextures: THREE.Texture[] = [];
    let runtimeManifest: RuntimeManifest | undefined;
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(cameraFovRef.current, 1, 0.1, 10000);
    cameraRef.current = camera;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    const maximumPixelRatio = window.matchMedia("(pointer: coarse)").matches === true ? 1.5 : 2;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, maximumPixelRatio));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1;
    renderer.shadowMap.enabled = false;
    mount.appendChild(renderer.domElement);

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
            frameTexture.generateMipmaps = false;
            frameTexture.minFilter = THREE.LinearFilter;
            frameTexture.magFilter = THREE.LinearFilter;
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
                  material instanceof THREE.MeshBasicMaterial === false
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
          const objectsByName = new Map<string, THREE.Object3D>();
          model.traverse((object) => {
            if (object.name.length > 0) objectsByName.set(object.name, object);
            if (object instanceof THREE.Mesh === true) {
              const sourceMaterials =
                Array.isArray(object.material) === true ? object.material : [object.material];
              const unlitMaterials = sourceMaterials.map(createUnlitMaterial);
              object.material =
                Array.isArray(object.material) === true ? unlitMaterials : unlitMaterials[0];
              sourceMaterials.forEach((material) => material.dispose());
              object.castShadow = false;
              object.receiveShadow = false;
            }
          });
          scene.add(model);
          void setupRuntimeAssets(model);

          const box = new THREE.Box3().setFromObject(model);
          const center = box.getCenter(new THREE.Vector3());
          const size = box.getSize(new THREE.Vector3());
          const largestDimension = Math.max(size.x, size.y, size.z);
          const radius = largestDimension > 0 ? largestDimension : 10;
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

          const loopClips = createIndependentLoopClips(gltf.animations);
          loopClips.forEach((clip) => {
            const trackName = clip.tracks[0]?.name ?? "";
            const propertySeparator = trackName.lastIndexOf(".");
            const objectName =
              propertySeparator > 0 ? trackName.slice(0, propertySeparator) : trackName;
            const animationRoot = objectsByName.get(objectName);
            if (animationRoot === undefined) return;

            const propertyName =
              propertySeparator >= 0 ? trackName.slice(propertySeparator + 1) : trackName;
            if (
              propertyName !== "position" &&
              propertyName !== "quaternion" &&
              propertyName !== "scale"
            )
              return;
            const animationTrack = clip.tracks[0];
            transformAnimators.push({
              target: animationRoot,
              property: propertyName,
              duration: clip.duration,
              track: animationTrack,
            });
          });
          onProgress(100);
          onReady(loopClips.length);
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
      transformAnimators.forEach((animator) => applyTransformFrame(animator, animationElapsed));
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
      renderer.render(scene, camera);
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
      scene.traverse((object) => {
        if (object instanceof THREE.Mesh === true) {
          object.geometry?.dispose();
          const materials =
            Array.isArray(object.material) === true ? object.material : [object.material];
          materials.forEach((material) => material.dispose());
        }
      });
      if (scene.background instanceof THREE.Texture === true) scene.background.dispose();
      runtimeTextures.forEach((texture) => texture.dispose());
      renderer.dispose();
      renderer.domElement.remove();
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
    createCameraSnapshot,
    resetCamera,
    setMobileMove,
    setMobileLook,
    setMobileVertical,
  };
}
