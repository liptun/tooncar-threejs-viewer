export type Track = {
  id: string
  name: string
  originalName: string
  modelUrl: string
  thumbnailUrl: string
  musicUrl: string
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
const trackOrder = ['venus', 'luna', 'vegas', 'sahara', 'atolon', 'amazonia', 'castilla', 'japon', 'alaska']

export const tracks = ([
  {
    id: 'luna',
    name: 'Księżyc',
    originalName: 'Luna',
    modelUrl: '/tracks/luna/Luna.glb',
    thumbnailUrl: '/tracks/luna/thumbnail.jpg',
    musicUrl: '/music/Track00.mp3',
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
    name: 'Wenus',
    originalName: 'Venus',
    modelUrl: '/tracks/venus/Venus.glb',
    thumbnailUrl: '/tracks/venus/thumbnail.jpg',
    musicUrl: '/music/Track05.mp3',
    runtimeUrl: '/tracks/venus/runtime.json',
    cameraStart: {
      position: [1.5645, -4.8094, -21.5831],
      rotation: [0.110163, 8.639398, 0],
      fov: 75,
    },
    accent: '#c56cff',
    available: true,
  },
  {
    id: 'alaska',
    name: 'Alaska',
    originalName: 'Alaska',
    modelUrl: '/tracks/alaska/Alaska.glb',
    thumbnailUrl: '/tracks/alaska/thumbnail.jpg',
    musicUrl: '/music/Track00.mp3',
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
    originalName: 'Amazonia',
    modelUrl: '/tracks/amazonia/Amazonia.glb',
    thumbnailUrl: '/tracks/amazonia/thumbnail.jpg',
    musicUrl: '/music/Track01.mp3',
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
    name: 'Atol',
    originalName: 'Atolon',
    modelUrl: '/tracks/atolon/Atolon.glb',
    thumbnailUrl: '/tracks/atolon/thumbnail.jpg',
    musicUrl: '/music/Track02.mp3',
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
    name: 'Kastylia',
    originalName: 'Castilla',
    modelUrl: '/tracks/castilla/Castilla.glb',
    thumbnailUrl: '/tracks/castilla/thumbnail.jpg',
    musicUrl: '/music/Track03.mp3',
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
    name: 'Japonia',
    originalName: 'Japon',
    modelUrl: '/tracks/japon/Japon.glb',
    thumbnailUrl: '/tracks/japon/thumbnail.jpg',
    musicUrl: '/music/Track05.mp3',
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
    originalName: 'Sahara',
    modelUrl: '/tracks/sahara/Sahara.glb',
    thumbnailUrl: '/tracks/sahara/thumbnail.jpg',
    musicUrl: '/music/Track01.mp3',
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
    name: 'Las Vegas',
    originalName: 'Vegas',
    modelUrl: '/tracks/vegas/Vegas.glb',
    thumbnailUrl: '/tracks/vegas/thumbnail.jpg',
    musicUrl: '/music/Track03.mp3',
    runtimeUrl: '/tracks/vegas/runtime.json',
    cameraStart: {
      position: [11.3943, -11.6969, 5.7675],
      rotation: [-0.015837, 0.611398, 0],
      fov: 75,
    },
    accent: '#ff4fb8',
    available: true,
  },
] satisfies Track[]).sort((a, b) => trackOrder.indexOf(a.id) - trackOrder.indexOf(b.id))
