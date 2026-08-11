import * as THREE from "three";

type TexturedMaterial = THREE.Material & {
  map?: THREE.Texture | null;
  alphaMap?: THREE.Texture | null;
  color?: THREE.Color;
  opacity?: number;
  alphaTest?: number;
  vertexColors?: boolean;
};

type AlphaMode = "opaque" | "clip" | "blend";

function classifyTextureAlpha(texture: THREE.Texture | null): AlphaMode | null {
  const image = texture?.image as CanvasImageSource | undefined;
  if (image === undefined) return null;

  const dimensions = image as CanvasImageSource & { width?: number; height?: number };
  const width = dimensions.width ?? 0;
  const height = dimensions.height ?? 0;
  if (width === 0 || height === 0) return null;

  try {
    const sampleSize = 64;
    const scale = Math.min(sampleSize / width, sampleSize / height, 1);
    const sampleWidth = Math.max(1, Math.round(width * scale));
    const sampleHeight = Math.max(1, Math.round(height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = sampleWidth;
    canvas.height = sampleHeight;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (context === null) return null;

    context.drawImage(image, 0, 0, sampleWidth, sampleHeight);
    const pixels = context.getImageData(0, 0, sampleWidth, sampleHeight).data;
    let transparentPixels = 0;
    let intermediatePixels = 0;

    for (let index = 3; index < pixels.length; index += 4) {
      const alpha = pixels[index];
      if (alpha <= 15) transparentPixels += 1;
      else if (alpha < 240) intermediatePixels += 1;
    }

    const pixelCount = pixels.length / 4;
    if (transparentPixels === 0 && intermediatePixels === 0) return "opaque";
    return intermediatePixels / pixelCount >= 0.1 ? "blend" : "clip";
  } catch {
    return null;
  }
}

function configureTextureFiltering(texture: THREE.Texture | null, maximumAnisotropy: number) {
  if (texture === null) return;
  texture.anisotropy = maximumAnisotropy;
  texture.needsUpdate = true;
}

export function createUnlitMaterial(source: THREE.Material, maximumAnisotropy: number) {
  const material = source as TexturedMaterial;
  configureTextureFiltering(material.map ?? null, maximumAnisotropy);
  configureTextureFiltering(material.alphaMap ?? null, maximumAnisotropy);
  const usesAlpha = source.transparent === true || source.alphaHash === true;
  const detectedAlphaMode = classifyTextureAlpha(material.alphaMap ?? material.map ?? null);
  const alphaMode: AlphaMode =
    usesAlpha === false
      ? "opaque"
      : material.opacity !== undefined && material.opacity < 1
        ? "blend"
        : (detectedAlphaMode ?? "clip");
  const usesBlend = alphaMode === "blend";
  const usesClip = alphaMode === "clip";
  const unlit = new THREE.MeshBasicMaterial({
    name: source.name,
    map: material.map ?? null,
    alphaMap: material.alphaMap ?? null,
    color: material.color?.clone() ?? new THREE.Color(0xffffff),
    opacity: material.opacity ?? 1,
    alphaTest: usesClip === true ? Math.max(material.alphaTest ?? 0, 0.5) : 0,
    transparent: usesBlend,
    side: source.side,
    vertexColors: material.vertexColors ?? false,
    depthTest: source.depthTest,
    depthWrite: usesClip === true ? true : usesBlend === true ? false : source.depthWrite,
    blending: source.blending,
  });

  unlit.toneMapped = false;
  unlit.alphaHash = false;
  unlit.forceSinglePass = usesBlend;
  unlit.polygonOffset = source.polygonOffset;
  unlit.polygonOffsetFactor = source.polygonOffsetFactor;
  unlit.polygonOffsetUnits = source.polygonOffsetUnits;
  return unlit;
}

export function createLitMaterial(source: THREE.MeshBasicMaterial) {
  const lit = new THREE.MeshLambertMaterial({
    name: source.name,
    map: source.map,
    alphaMap: source.alphaMap,
    color: source.color.clone(),
    opacity: source.opacity,
    alphaTest: source.alphaTest,
    transparent: source.transparent,
    side: source.side,
    vertexColors: source.vertexColors,
    depthTest: source.depthTest,
    depthWrite: source.depthWrite,
    blending: source.blending,
  });

  lit.toneMapped = false;
  lit.forceSinglePass = source.forceSinglePass;
  lit.polygonOffset = source.polygonOffset;
  lit.polygonOffsetFactor = source.polygonOffsetFactor;
  lit.polygonOffsetUnits = source.polygonOffsetUnits;
  return lit;
}
