import * as THREE from "three";

type TexturedMaterial = THREE.Material & {
  map?: THREE.Texture | null;
  alphaMap?: THREE.Texture | null;
  color?: THREE.Color;
  opacity?: number;
  alphaTest?: number;
  vertexColors?: boolean;
};

export function createUnlitMaterial(source: THREE.Material) {
  const material = source as TexturedMaterial;
  const unlit = new THREE.MeshBasicMaterial({
    name: source.name,
    map: material.map ?? null,
    alphaMap: material.alphaMap ?? null,
    color: material.color?.clone() ?? new THREE.Color(0xffffff),
    opacity: material.opacity ?? 1,
    alphaTest: material.alphaTest ?? 0,
    transparent: source.transparent,
    side: source.side,
    vertexColors: material.vertexColors ?? false,
    depthTest: source.depthTest,
    depthWrite: source.depthWrite,
    blending: source.blending,
  });

  unlit.toneMapped = false;
  unlit.alphaHash = source.alphaHash;
  unlit.polygonOffset = source.polygonOffset;
  unlit.polygonOffsetFactor = source.polygonOffsetFactor;
  unlit.polygonOffsetUnits = source.polygonOffsetUnits;
  return unlit;
}
