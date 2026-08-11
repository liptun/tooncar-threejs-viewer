import * as THREE from "three";

export type RuntimeManifest = {
  textureAnimations?: string;
  skybox?: string;
};

export type TextureAnimationIndex = {
  animations: Array<{ animationIndex: number; metadata: string }>;
};

export type TextureAnimationMetadata = {
  animationIndex: number;
  materialIndex: number;
  tickRateHz: number;
  cycleTicks: number;
  tickFrames: number[];
  atlas: { file: string; cellWidth: number; cellHeight: number };
  frames: Array<{
    index: number;
    column: number;
    rowTop: number;
    uvOffset: [number, number];
    uvScale: [number, number];
  }>;
};

export type SkyboxMetadata = {
  faces: Record<"RT" | "LF" | "UP" | "DN" | "FR" | "BK", { file: string }>;
};

export type TextureAnimator = {
  texture: THREE.CanvasTexture;
  context: CanvasRenderingContext2D;
  atlasImage: HTMLImageElement;
  cellWidth: number;
  cellHeight: number;
  currentFrame: number;
  tickRateHz: number;
  cycleTicks: number;
  tickFrames: number[];
  frames: TextureAnimationMetadata["frames"];
};

export function resolveRelativeUrl(baseUrl: string, relativeUrl: string) {
  return new URL(relativeUrl, new URL(baseUrl, window.location.href)).toString();
}

export async function loadJson<T>(url: string) {
  const response = await fetch(url);
  if (response.ok === false) throw new Error(`HTTP ${response.status}: ${url}`);
  return response.json() as Promise<T>;
}

export function loadImage(url: string) {
  return new Promise<HTMLImageElement>((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = reject;
    image.src = url;
  });
}

export async function loadCubeTexture(urls: string[], mirroredFaces: number[]) {
  const images = await Promise.all(urls.map(loadImage));
  const faces = images.map((image, index) => {
    if (mirroredFaces.includes(index) === false) return image;
    const canvas = document.createElement("canvas");
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
    const context = canvas.getContext("2d")!;
    context.translate(canvas.width, canvas.height);
    context.scale(-1, -1);
    context.drawImage(image, 0, 0);
    return canvas;
  });
  const texture = new THREE.CubeTexture(faces);
  texture.needsUpdate = true;
  return texture;
}

export function drawTextureAnimationFrame(animator: TextureAnimator, frameIndex: number) {
  if (frameIndex === animator.currentFrame) return;
  const frame = animator.frames[frameIndex] ?? animator.frames[0];
  animator.context.clearRect(0, 0, animator.cellWidth, animator.cellHeight);
  animator.context.drawImage(
    animator.atlasImage,
    frame.column * animator.cellWidth,
    frame.rowTop * animator.cellHeight,
    animator.cellWidth,
    animator.cellHeight,
    0,
    0,
    animator.cellWidth,
    animator.cellHeight,
  );
  animator.currentFrame = frameIndex;
  animator.texture.needsUpdate = true;
}
