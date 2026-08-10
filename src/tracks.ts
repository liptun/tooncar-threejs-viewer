export type Track = {
  id: string
  name: string
  world: string
  modelUrl: string
  skyboxUrl?: string
  runtimeUrl?: string
  cameraStart?: {
    position: [number, number, number]
    rotation: [number, number, number]
    fov: number
  }
  accent: string
  available: boolean
}

// Kolejne trasy wystarczy dopisać tutaj i umieścić ich pliki w public/tracks/.
export const tracks: Track[] = [
  {
    id: 'luna',
    name: 'Luna',
    world: 'Moon Circuit',
    modelUrl: '/tracks/luna/Luna.glb',
    runtimeUrl: '/tracks/luna/runtime.json',
    cameraStart: {
      position: [-22.5001, -10.7354, 28.5046],
      rotation: [0.308163, 5.033398, 0],
      fov: 75,
    },
    accent: '#ff5c35',
    available: true,
  },
  {
    id: 'venus',
    name: 'Venus',
    world: 'Venus Circuit',
    modelUrl: '/tracks/venus/Venus.glb',
    runtimeUrl: '/tracks/venus/runtime.json',
    cameraStart: {
      position: [1.96, -3.5759, -23.1694],
      rotation: [-0.147837, 2.435398, 0],
      fov: 75,
    },
    accent: '#c56cff',
    available: true,
  },
  {
    id: 'alaska',
    name: 'Alaska',
    world: 'Frozen Circuit',
    modelUrl: '/tracks/alaska/Alaska.glb',
    runtimeUrl: '/tracks/alaska/runtime.json',
    cameraStart: {
      position: [29.2191, 0.937, 37.5901],
      rotation: [-0.129837, 13.283398, 0],
      fov: 75,
    },
    accent: '#79cfff',
    available: true,
  },
  {
    id: 'amazonia',
    name: 'Amazonia',
    world: 'Jungle Circuit',
    modelUrl: '/tracks/amazonia/Amazonia.glb',
    runtimeUrl: '/tracks/amazonia/runtime.json',
    cameraStart: {
      position: [36.2031, 0.2488, 24.1522],
      rotation: [0.110163, -10.908602, 0],
      fov: 75,
    },
    accent: '#57cf72',
    available: true,
  },
  {
    id: 'atolon',
    name: 'Atolon',
    world: 'Island Circuit',
    modelUrl: '/tracks/atolon/Atolon.glb',
    runtimeUrl: '/tracks/atolon/runtime.json',
    cameraStart: {
      position: [-9.6862, -0.8751, 8.6363],
      rotation: [-0.369837, 0.035398, 0],
      fov: 75,
    },
    accent: '#45d6d0',
    available: true,
  },
  {
    id: 'castilla',
    name: 'Castilla',
    world: 'Castle Circuit',
    modelUrl: '/tracks/castilla/Castilla.glb',
    runtimeUrl: '/tracks/castilla/runtime.json',
    cameraStart: {
      position: [-43.5395, -3.8008, 15.0055],
      rotation: [0.032163, 5.333398, 0],
      fov: 75,
    },
    accent: '#d29a64',
    available: true,
  },
  {
    id: 'japon',
    name: 'Japon',
    world: 'Sakura Circuit',
    modelUrl: '/tracks/japon/Japon.glb',
    runtimeUrl: '/tracks/japon/runtime.json',
    cameraStart: {
      position: [-5.8153, -3.3664, 37.3004],
      rotation: [-0.069837, -0.348602, 0],
      fov: 75,
    },
    accent: '#ff7ea8',
    available: true,
  },
  {
    id: 'sahara',
    name: 'Sahara',
    world: 'Desert Circuit',
    modelUrl: '/tracks/sahara/Sahara.glb',
    runtimeUrl: '/tracks/sahara/runtime.json',
    cameraStart: {
      position: [-4.3697, -2.0048, -16.5463],
      rotation: [0.068163, 9.227398, 0],
      fov: 75,
    },
    accent: '#e7a94f',
    available: true,
  },
  {
    id: 'vegas',
    name: 'Vegas',
    world: 'Neon Circuit',
    modelUrl: '/tracks/vegas/Vegas.glb',
    runtimeUrl: '/tracks/vegas/runtime.json',
    cameraStart: {
      position: [11.3943, -11.6969, 5.7675],
      rotation: [-0.015837, 0.611398, 0],
      fov: 75,
    },
    accent: '#ff4fb8',
    available: true,
  },
]
