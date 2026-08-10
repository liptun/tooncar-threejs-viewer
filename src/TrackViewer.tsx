import { useEffect, useRef } from 'react'
import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import type { Track } from './tracks'

type Props = {
  track: Track
  onProgress: (progress: number) => void
  onReady: (animations: number) => void
  onError: (message: string) => void
}

type TexturedMaterial = THREE.Material & {
  map?: THREE.Texture | null
  alphaMap?: THREE.Texture | null
  color?: THREE.Color
  opacity?: number
  alphaTest?: number
  vertexColors?: boolean
}

function createUnlitMaterial(source: THREE.Material) {
  const material = source as TexturedMaterial
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
  })

  // ToonCar używał tekstur bez wpływu świateł i korekcji ekspozycji.
  unlit.toneMapped = false
  unlit.alphaHash = source.alphaHash
  unlit.polygonOffset = source.polygonOffset
  unlit.polygonOffsetFactor = source.polygonOffsetFactor
  unlit.polygonOffsetUnits = source.polygonOffsetUnits
  return unlit
}

const ANIMATION_EPSILON = 1e-4

function nearlyEqual(a: number, b: number) {
  return Math.abs(a - b) <= ANIMATION_EPSILON * Math.max(1, Math.abs(a), Math.abs(b))
}

function trackFramesEqual(track: THREE.KeyframeTrack, a: number, b: number) {
  const valueSize = track.getValueSize()
  for (let component = 0; component < valueSize; component += 1) {
    if (!nearlyEqual(track.values[a * valueSize + component], track.values[b * valueSize + component])) {
      return false
    }
  }
  return true
}

function isConstantTrack(track: THREE.KeyframeTrack) {
  for (let frame = 1; frame < track.times.length; frame += 1) {
    if (!trackFramesEqual(track, 0, frame)) return false
  }
  return true
}

function findTrackPeriod(track: THREE.KeyframeTrack) {
  const frameCount = track.times.length
  for (let period = 1; period < frameCount - 1; period += 1) {
    let repeats = true
    for (let frame = period; frame < frameCount; frame += 1) {
      if (!trackFramesEqual(track, frame, frame % period)) {
        repeats = false
        break
      }
    }
    if (repeats) return period
  }
  return frameCount - 1
}

function createIndependentLoopClips(sourceClips: THREE.AnimationClip[]) {
  const clips: THREE.AnimationClip[] = []

  sourceClips.forEach((sourceClip) => {
    sourceClip.tracks.forEach((sourceTrack, index) => {
      if (sourceTrack.times.length < 2 || isConstantTrack(sourceTrack)) return

      const period = findTrackPeriod(sourceTrack)
      const track = sourceTrack.clone()
      const frameCount = period + 1
      const valueCount = frameCount * track.getValueSize()
      track.times = track.times.slice(0, frameCount)
      track.values = track.values.slice(0, valueCount)

      const startTime = track.times[0]
      if (startTime !== 0) {
        for (let frame = 0; frame < track.times.length; frame += 1) track.times[frame] -= startTime
      }

      const duration = track.times[track.times.length - 1]
      clips.push(new THREE.AnimationClip(`${sourceClip.name}:${track.name}:${index}`, duration, [track]))
    })
  })

  return clips
}

export function TrackViewer({ track, onProgress, onReady, onError }: Props) {
  const mountRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const mount = mountRef.current
    if (!mount) return

    let disposed = false
    let frame = 0
    let mixer: THREE.AnimationMixer | undefined
    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 10000)
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.outputColorSpace = THREE.SRGBColorSpace
    renderer.toneMapping = THREE.ACESFilmicToneMapping
    renderer.toneMappingExposure = 1
    renderer.shadowMap.enabled = false
    mount.appendChild(renderer.domElement)

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.06
    controls.screenSpacePanning = true

    const textureLoader = new THREE.TextureLoader()
    textureLoader.load(track.skyboxUrl, (texture) => {
      if (disposed) return texture.dispose()
      texture.mapping = THREE.EquirectangularReflectionMapping
      texture.colorSpace = THREE.SRGBColorSpace
      scene.background = texture
      scene.environment = texture
    }, undefined, () => onError('Nie udało się wczytać skyboxa.'))

    new GLTFLoader().load(
      track.modelUrl,
      (gltf) => {
        if (disposed) return
        const model = gltf.scene
        model.traverse((object) => {
          if (object instanceof THREE.Mesh) {
            const sourceMaterials = Array.isArray(object.material) ? object.material : [object.material]
            const unlitMaterials = sourceMaterials.map(createUnlitMaterial)
            object.material = Array.isArray(object.material) ? unlitMaterials : unlitMaterials[0]
            sourceMaterials.forEach((material) => material.dispose())
            object.castShadow = false
            object.receiveShadow = false
          }
        })
        scene.add(model)

        const box = new THREE.Box3().setFromObject(model)
        const center = box.getCenter(new THREE.Vector3())
        const size = box.getSize(new THREE.Vector3())
        const radius = Math.max(size.x, size.y, size.z) || 10
        controls.target.copy(center)
        camera.position.copy(center).add(new THREE.Vector3(radius * 0.85, radius * 0.55, radius * 0.85))
        camera.near = Math.max(radius / 1000, 0.1)
        camera.far = radius * 20
        camera.updateProjectionMatrix()
        controls.update()

        mixer = new THREE.AnimationMixer(model)
        const loopClips = createIndependentLoopClips(gltf.animations)
        loopClips.forEach((clip) => mixer!.clipAction(clip).setLoop(THREE.LoopRepeat, Infinity).play())
        onProgress(100)
        onReady(loopClips.length)
      },
      (event) => onProgress(event.total ? Math.round((event.loaded / event.total) * 100) : 0),
      () => onError('Nie udało się wczytać modelu trasy.'),
    )

    const resize = () => {
      const { clientWidth, clientHeight } = mount
      renderer.setSize(clientWidth, clientHeight, false)
      camera.aspect = clientWidth / Math.max(clientHeight, 1)
      camera.updateProjectionMatrix()
    }
    const observer = new ResizeObserver(resize)
    observer.observe(mount)
    resize()

    const clock = new THREE.Clock()
    const render = () => {
      frame = requestAnimationFrame(render)
      mixer?.update(Math.min(clock.getDelta(), 0.1))
      controls.update()
      renderer.render(scene, camera)
    }
    render()

    return () => {
      disposed = true
      cancelAnimationFrame(frame)
      observer.disconnect()
      controls.dispose()
      mixer?.stopAllAction()
      scene.traverse((object) => {
        if (object instanceof THREE.Mesh) {
          object.geometry?.dispose()
          const materials = Array.isArray(object.material) ? object.material : [object.material]
          materials.forEach((material) => material.dispose())
        }
      })
      if (scene.background instanceof THREE.Texture) scene.background.dispose()
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [track, onError, onProgress, onReady])

  return <div ref={mountRef} className="absolute inset-0" aria-label={`Widok 3D trasy ${track.name}`} />
}
