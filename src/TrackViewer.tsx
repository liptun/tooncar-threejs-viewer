import { useEffect, useRef, useState } from 'react'
import { Camera, Gauge, Maximize, RotateCcw } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import type { Track } from './tracks'

type Props = {
  track: Track
  onProgress: (progress: number) => void
  onReady: (animations: number) => void
  onError: (message: string) => void
  onSkyboxReady: () => void
}

type RuntimeManifest = {
  textureAnimations?: string
  skybox?: string
}

type TextureAnimationIndex = {
  animations: Array<{ animationIndex: number; metadata: string }>
}

type TextureAnimationMetadata = {
  animationIndex: number
  materialIndex: number
  tickRateHz: number
  cycleTicks: number
  tickFrames: number[]
  atlas: { file: string; cellWidth: number; cellHeight: number }
  frames: Array<{
    index: number
    column: number
    rowTop: number
    uvOffset: [number, number]
    uvScale: [number, number]
  }>
}

type SkyboxMetadata = {
  faces: Record<'RT' | 'LF' | 'UP' | 'DN' | 'FR' | 'BK', { file: string }>
}

type TextureAnimator = {
  texture: THREE.CanvasTexture
  context: CanvasRenderingContext2D
  atlasImage: HTMLImageElement
  cellWidth: number
  cellHeight: number
  currentFrame: number
  tickRateHz: number
  cycleTicks: number
  tickFrames: number[]
  frames: TextureAnimationMetadata['frames']
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

function resolveRelativeUrl(baseUrl: string, relativeUrl: string) {
  return new URL(relativeUrl, new URL(baseUrl, window.location.href)).toString()
}

async function loadJson<T>(url: string) {
  const response = await fetch(url)
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${url}`)
  return response.json() as Promise<T>
}

function loadImage(url: string) {
  return new Promise<HTMLImageElement>((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = reject
    image.src = url
  })
}

async function loadCubeTexture(urls: string[], mirroredFaces: number[]) {
  const images = await Promise.all(urls.map(loadImage))
  const faces = images.map((image, index) => {
    if (!mirroredFaces.includes(index)) return image
    const canvas = document.createElement('canvas')
    canvas.width = image.naturalWidth
    canvas.height = image.naturalHeight
    const context = canvas.getContext('2d')!
    context.translate(canvas.width, canvas.height)
    context.scale(-1, -1)
    context.drawImage(image, 0, 0)
    return canvas
  })
  const texture = new THREE.CubeTexture(faces)
  texture.needsUpdate = true
  return texture
}

function drawTextureAnimationFrame(animator: TextureAnimator, frameIndex: number) {
  if (frameIndex === animator.currentFrame) return
  const frame = animator.frames[frameIndex] ?? animator.frames[0]
  animator.context.clearRect(0, 0, animator.cellWidth, animator.cellHeight)
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
  )
  animator.currentFrame = frameIndex
  animator.texture.needsUpdate = true
}

function readCameraSnapshot() {
  const params = new URLSearchParams(window.location.search)
  const position = params.get('p')?.split(',').map(Number)
  const rotation = params.get('r')?.split(',').map(Number)
  const fov = Number(params.get('fov'))
  if (
    position?.length !== 3 || rotation?.length !== 3 ||
    !position.every(Number.isFinite) || !rotation.every(Number.isFinite)
  ) return null
  return { position, rotation, fov: Number.isFinite(fov) ? fov : 75 }
}

export function TrackViewer({ track, onProgress, onReady, onError, onSkyboxReady }: Props) {
  const runtimeUrl = `/tracks/${track.id}/runtime.json`
  const [, setSearchParams] = useSearchParams()
  const mountRef = useRef<HTMLDivElement>(null)
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null)
  const [moveSpeed, setMoveSpeed] = useState(60)
  const [showSpeedIndicator, setShowSpeedIndicator] = useState(false)
  const [snapshotCopied, setSnapshotCopied] = useState(false)
  const speedIndicatorTimeoutRef = useRef<number | null>(null)
  const moveSpeedRef = useRef(60)
  const cameraFovRef = useRef(75)

  const updateMoveSpeed = (value: number) => {
    const nextSpeed = THREE.MathUtils.clamp(Math.round(value), 5, 200)
    moveSpeedRef.current = nextSpeed
    setMoveSpeed(nextSpeed)
  }

  const updateCameraFov = (value: number) => {
    const nextFov = THREE.MathUtils.clamp(Math.round(value), 40, 110)
    cameraFovRef.current = nextFov
  }

  const createCameraSnapshot = async () => {
    const camera = cameraRef.current
    if (!camera) return
    const formatPosition = (value: number) => value.toFixed(4)
    const formatRotation = (value: number) => value.toFixed(6)
    const params = new URLSearchParams(window.location.search)
    params.set('p', camera.position.toArray().map(formatPosition).join(','))
    params.set('r', [camera.rotation.x, camera.rotation.y, camera.rotation.z].map(formatRotation).join(','))
    params.set('fov', String(Math.round(camera.fov)))
    setSearchParams(params, { replace: true })

    const url = new URL(window.location.href)
    url.search = params.toString()
    try {
      await navigator.clipboard.writeText(url.toString())
      setSnapshotCopied(true)
      window.setTimeout(() => setSnapshotCopied(false), 1800)
    } catch {
      setSnapshotCopied(false)
    }
  }

  const resetCamera = () => {
    const camera = cameraRef.current
    const initialView = track.cameraStart
    if (!camera || !initialView) return
    camera.position.fromArray(initialView.position)
    camera.rotation.set(initialView.rotation[0], initialView.rotation[1], initialView.rotation[2], 'YXZ')
    cameraFovRef.current = initialView.fov
    camera.fov = initialView.fov
    camera.updateProjectionMatrix()

    const params = new URLSearchParams(window.location.search)
    params.delete('p')
    params.delete('r')
    params.delete('fov')
    setSearchParams(params, { replace: true })
  }

  useEffect(() => {
    const mount = mountRef.current
    if (!mount) return

    let disposed = false
    let frame = 0
    let mixer: THREE.AnimationMixer | undefined
    let animationElapsed = 0
    let baseMoveSpeed = 10
    const pressedKeys = new Set<string>()
    const movement = new THREE.Vector3()
    const forward = new THREE.Vector3()
    const right = new THREE.Vector3()
    const worldUp = new THREE.Vector3(0, 1, 0)
    const textureAnimators: TextureAnimator[] = []
    const runtimeTextures: THREE.Texture[] = []
    let runtimeManifest: RuntimeManifest | undefined
    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(cameraFovRef.current, 1, 0.1, 10000)
    cameraRef.current = camera
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.outputColorSpace = THREE.SRGBColorSpace
    renderer.toneMapping = THREE.ACESFilmicToneMapping
    renderer.toneMappingExposure = 1
    renderer.shadowMap.enabled = false
    mount.appendChild(renderer.domElement)

    let isDragging = false
    camera.rotation.order = 'YXZ'

    const stopDragging = () => {
      if (document.pointerLockElement === renderer.domElement) document.exitPointerLock()
      isDragging = false
      renderer.domElement.style.cursor = 'grab'
    }
    const onWindowBlur = () => {
      stopDragging()
      pressedKeys.clear()
    }
    const onPointerDown = (event: PointerEvent) => {
      if (event.button !== 0) return
      renderer.domElement.requestPointerLock()
    }
    const onPointerLockChange = () => {
      isDragging = document.pointerLockElement === renderer.domElement
      renderer.domElement.style.cursor = isDragging ? 'none' : 'grab'
    }
    const onPointerMove = (event: MouseEvent) => {
      if (!isDragging || document.pointerLockElement !== renderer.domElement) return
      const sensitivity = 0.006
      camera.rotation.y -= event.movementX * sensitivity
      camera.rotation.x = THREE.MathUtils.clamp(
        camera.rotation.x - event.movementY * sensitivity,
        -Math.PI / 2 + 0.01,
        Math.PI / 2 - 0.01,
      )
    }
    const onPointerUp = (event: MouseEvent) => {
      if (event.button === 0) stopDragging()
    }
    const onContextMenu = (event: MouseEvent) => event.preventDefault()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement) return
      pressedKeys.add(event.code)
    }
    const onKeyUp = (event: KeyboardEvent) => pressedKeys.delete(event.code)
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      updateMoveSpeed(moveSpeedRef.current + (event.deltaY < 0 ? 5 : -5))
      setShowSpeedIndicator(true)
      if (speedIndicatorTimeoutRef.current !== null) window.clearTimeout(speedIndicatorTimeoutRef.current)
      speedIndicatorTimeoutRef.current = window.setTimeout(() => {
        setShowSpeedIndicator(false)
        speedIndicatorTimeoutRef.current = null
      }, 1200)
    }
    renderer.domElement.style.cursor = 'grab'
    renderer.domElement.addEventListener('pointerdown', onPointerDown)
    renderer.domElement.addEventListener('contextmenu', onContextMenu)
    renderer.domElement.addEventListener('wheel', onWheel, { passive: false })
    document.addEventListener('mousemove', onPointerMove)
    document.addEventListener('mouseup', onPointerUp)
    document.addEventListener('pointerlockchange', onPointerLockChange)
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('keyup', onKeyUp)
    window.addEventListener('blur', onWindowBlur)

    const prepareSkybox = async () => {
      try {
        runtimeManifest = await loadJson<RuntimeManifest>(runtimeUrl)
        if (!runtimeManifest.skybox) return

        const skyboxUrl = resolveRelativeUrl(runtimeUrl, runtimeManifest.skybox)
        const skybox = await loadJson<SkyboxMetadata>(skyboxUrl)
        const faceUrl = (face: keyof SkyboxMetadata['faces']) => resolveRelativeUrl(skyboxUrl, skybox.faces[face].file)
        const cubeTexture = await loadCubeTexture([
          faceUrl('RT'), faceUrl('LF'), faceUrl('UP'),
          faceUrl('DN'), faceUrl('FR'), faceUrl('BK'),
        ], [0, 1, 4, 5])
        if (disposed) return cubeTexture.dispose()
        cubeTexture.colorSpace = THREE.SRGBColorSpace
        runtimeTextures.push(cubeTexture)
        if (scene.background instanceof THREE.Texture) scene.background.dispose()
        scene.background = cubeTexture
        scene.environment = cubeTexture
        onSkyboxReady()
      } catch (skyboxError) {
        console.warn('Nie udało się wczytać skyboxa przed modelem.', skyboxError)
      }
    }

    const setupRuntimeAssets = async (model: THREE.Object3D) => {
      try {
        const runtime = runtimeManifest ?? await loadJson<RuntimeManifest>(runtimeUrl)

        if (runtime.textureAnimations) {
          const indexUrl = resolveRelativeUrl(runtimeUrl, runtime.textureAnimations)
          const index = await loadJson<TextureAnimationIndex>(indexUrl)

          for (const entry of index.animations) {
            const metadataUrl = resolveRelativeUrl(indexUrl, entry.metadata)
            const metadata = await loadJson<TextureAnimationMetadata>(metadataUrl)
            const atlasImage = await loadImage(resolveRelativeUrl(metadataUrl, metadata.atlas.file))
            if (disposed) return
            const canvas = document.createElement('canvas')
            canvas.width = metadata.atlas.cellWidth
            canvas.height = metadata.atlas.cellHeight
            const context = canvas.getContext('2d')!
            const frameTexture = new THREE.CanvasTexture(canvas)
            frameTexture.colorSpace = THREE.SRGBColorSpace
            frameTexture.flipY = false
            frameTexture.wrapS = THREE.RepeatWrapping
            frameTexture.wrapT = THREE.RepeatWrapping
            frameTexture.generateMipmaps = false
            frameTexture.minFilter = THREE.LinearFilter
            frameTexture.magFilter = THREE.LinearFilter
            runtimeTextures.push(frameTexture)

            const materialName = `mat_${String(metadata.materialIndex).padStart(3, '0')}`
            let matched = false
            model.traverse((object) => {
              if (!(object instanceof THREE.Mesh)) return
              const materials = Array.isArray(object.material) ? object.material : [object.material]
              materials.forEach((material) => {
                if (material.name !== materialName || !(material instanceof THREE.MeshBasicMaterial)) return
                material.map = frameTexture
                material.needsUpdate = true
                matched = true
              })
            })

            if (matched) {
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
              }
              drawTextureAnimationFrame(animator, metadata.tickFrames[0] ?? 0)
              textureAnimators.push(animator)
            }
          }
        }
      } catch (runtimeError) {
        console.warn('Nie udało się uruchomić zasobów ToonCar runtime.', runtimeError)
      }
    }

    const loadModel = () => new GLTFLoader().load(
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
        void setupRuntimeAssets(model)

        const box = new THREE.Box3().setFromObject(model)
        const center = box.getCenter(new THREE.Vector3())
        const size = box.getSize(new THREE.Vector3())
        const radius = Math.max(size.x, size.y, size.z) || 10
        baseMoveSpeed = Math.max(radius * 0.15, 1)
        camera.position.copy(center).add(new THREE.Vector3(radius * 0.3, radius * 0.15, radius * 0.3))
        camera.lookAt(center)
        camera.rotation.order = 'YXZ'
        const initialView = readCameraSnapshot() ?? track.cameraStart
        if (initialView) {
          camera.position.fromArray(initialView.position)
          camera.rotation.set(initialView.rotation[0], initialView.rotation[1], initialView.rotation[2], 'YXZ')
          updateCameraFov(initialView.fov)
          camera.fov = cameraFovRef.current
        }
        camera.near = Math.max(radius / 1000, 0.1)
        camera.far = radius * 20
        camera.updateProjectionMatrix()

        mixer = new THREE.AnimationMixer(model)
        const loopClips = createIndependentLoopClips(gltf.animations)
        loopClips.forEach((clip) => mixer!.clipAction(clip).setLoop(THREE.LoopRepeat, Infinity).play())
        onProgress(100)
        onReady(loopClips.length)
      },
      (event) => onProgress(event.total ? Math.round((event.loaded / event.total) * 100) : 0),
      () => onError('Nie udało się wczytać modelu trasy.'),
    )

    void prepareSkybox().finally(() => {
      if (!disposed) loadModel()
    })

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
      const delta = Math.min(clock.getDelta(), 0.1)
      if (camera.fov !== cameraFovRef.current) {
        camera.fov = cameraFovRef.current
        camera.updateProjectionMatrix()
      }
      animationElapsed += delta
      mixer?.update(delta)
      if (pressedKeys.size > 0) {
        const sprintMultiplier = pressedKeys.has('ShiftLeft') || pressedKeys.has('ShiftRight') ? 2 : 1
        const distance = baseMoveSpeed * (moveSpeedRef.current / 50) * sprintMultiplier * delta
        movement.set(0, 0, 0)
        camera.getWorldDirection(forward)
        right.set(1, 0, 0).applyQuaternion(camera.quaternion)
        if (pressedKeys.has('KeyW')) movement.add(forward)
        if (pressedKeys.has('KeyS')) movement.sub(forward)
        if (pressedKeys.has('KeyA')) movement.sub(right)
        if (pressedKeys.has('KeyD')) movement.add(right)
        if (pressedKeys.has('KeyQ')) movement.sub(worldUp)
        if (pressedKeys.has('KeyE')) movement.add(worldUp)
        if (movement.lengthSq() > 0) camera.position.addScaledVector(movement.normalize(), distance)
      }
      textureAnimators.forEach((animator) => {
        const tick = Math.floor(animationElapsed * animator.tickRateHz) % animator.cycleTicks
        drawTextureAnimationFrame(animator, animator.tickFrames[tick] ?? 0)
      })
      renderer.render(scene, camera)
    }
    render()

    return () => {
      disposed = true
      if (speedIndicatorTimeoutRef.current !== null) window.clearTimeout(speedIndicatorTimeoutRef.current)
      cameraRef.current = null
      if (document.pointerLockElement === renderer.domElement) document.exitPointerLock()
      cancelAnimationFrame(frame)
      observer.disconnect()
      renderer.domElement.removeEventListener('pointerdown', onPointerDown)
      renderer.domElement.removeEventListener('contextmenu', onContextMenu)
      renderer.domElement.removeEventListener('wheel', onWheel)
      document.removeEventListener('mousemove', onPointerMove)
      document.removeEventListener('mouseup', onPointerUp)
      document.removeEventListener('pointerlockchange', onPointerLockChange)
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('keyup', onKeyUp)
      window.removeEventListener('blur', onWindowBlur)
      mixer?.stopAllAction()
      scene.traverse((object) => {
        if (object instanceof THREE.Mesh) {
          object.geometry?.dispose()
          const materials = Array.isArray(object.material) ? object.material : [object.material]
          materials.forEach((material) => material.dispose())
        }
      })
      if (scene.background instanceof THREE.Texture) scene.background.dispose()
      runtimeTextures.forEach((texture) => texture.dispose())
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [track, onError, onProgress, onReady, onSkyboxReady])

  return (
    <>
      <div ref={mountRef} className="absolute inset-0" aria-label={`Widok 3D trasy ${track.name}`} />
      <div className="pointer-events-auto absolute right-7 top-7 z-20 flex items-center justify-end gap-2 max-sm:right-4 max-sm:top-4">
        <button type="button" onClick={resetCamera} className="group flex h-10 w-10 cursor-pointer items-center justify-center overflow-hidden rounded-xl border border-white/10 bg-black/30 px-2.5 whitespace-nowrap text-white/75 backdrop-blur-sm transition-[width,color,background-color] duration-300 hover:w-40 hover:bg-[#10162d]/90 hover:text-white" aria-label="Resetuj kamerę" title="Resetuj kamerę">
          <RotateCcw size={17} className="shrink-0" />
          <span className="ml-0 max-w-0 overflow-hidden text-[10px] font-bold uppercase tracking-[.08em] opacity-0 [text-shadow:0_1px_3px_rgba(0,0,0,.95)] transition-[max-width,margin,opacity] duration-300 group-hover:ml-2 group-hover:max-w-28 group-hover:opacity-100">Resetuj kamerę</span>
        </button>
        <button type="button" onClick={() => void createCameraSnapshot()} className={`group flex h-10 w-10 cursor-pointer items-center justify-center overflow-hidden rounded-xl border px-2.5 whitespace-nowrap backdrop-blur-sm transition-[width,color,background-color,border-color] duration-300 hover:w-36 ${snapshotCopied ? 'border-[#f3ad00]/60 bg-[#f3ad00]/25 text-[#ffd455]' : 'border-white/10 bg-black/30 text-white/75 hover:bg-[#10162d]/90 hover:text-white'}`} aria-label="Skopiuj link do widoku kamery" title={snapshotCopied ? 'Skopiowano link' : 'Skopiuj link do widoku'}>
          <Camera size={17} className="shrink-0" />
          <span className="ml-0 max-w-0 overflow-hidden text-[10px] font-bold uppercase tracking-[.08em] opacity-0 [text-shadow:0_1px_3px_rgba(0,0,0,.95)] transition-[max-width,margin,opacity] duration-300 group-hover:ml-2 group-hover:max-w-24 group-hover:opacity-100">{snapshotCopied ? 'Skopiowano' : 'Skopiuj widok'}</span>
        </button>
        <button type="button" onClick={() => document.documentElement.requestFullscreen?.()} className="group flex h-10 w-10 cursor-pointer items-center justify-center overflow-hidden rounded-xl border border-white/10 bg-black/30 px-2.5 whitespace-nowrap text-white/75 backdrop-blur-sm transition-[width,color,background-color] duration-300 hover:w-36 hover:bg-[#10162d]/90 hover:text-white" aria-label="Tryb pełnoekranowy" title="Pełny ekran">
          <Maximize size={17} className="shrink-0" />
          <span className="ml-0 max-w-0 overflow-hidden text-[10px] font-bold uppercase tracking-[.08em] opacity-0 [text-shadow:0_1px_3px_rgba(0,0,0,.95)] transition-[max-width,margin,opacity] duration-300 group-hover:ml-2 group-hover:max-w-24 group-hover:opacity-100">Pełny ekran</span>
        </button>
      </div>
      <div className={`pointer-events-none absolute bottom-3 left-3 z-20 w-44 bg-[#10162d]/45 px-3 py-2 shadow-[0_8px_24px_rgba(0,0,0,.12)] backdrop-blur-sm transition-opacity duration-300 ${showSpeedIndicator ? 'opacity-100' : 'opacity-0'}`} aria-hidden={!showSpeedIndicator}>
        <div className="mb-1.5 flex items-center justify-between text-[9px] font-bold uppercase tracking-[.1em] text-white/55">
          <span className="flex items-center gap-1"><Gauge size={10} /> Prędkość lotu</span>
          <span className="font-mono text-[#ffd455]">{moveSpeed}</span>
        </div>
        <div className="h-1 overflow-hidden bg-white/10">
          <div className="h-full bg-[#f3ad00] transition-[width] duration-150" style={{ width: `${((moveSpeed - 5) / 195) * 100}%` }} />
        </div>
      </div>
    </>
  )
}
