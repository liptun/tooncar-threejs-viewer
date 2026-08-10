export type Track = {
  id: string
  name: string
  world: string
  modelUrl: string
  skyboxUrl: string
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
    skyboxUrl: '/tracks/luna/environment.png',
    accent: '#ff5c35',
    available: true,
  },
  {
    id: 'venus',
    name: 'Venus',
    world: 'Venus Circuit',
    modelUrl: '/tracks/venus/Venus.glb',
    skyboxUrl: '/tracks/venus/environment.png',
    accent: '#c56cff',
    available: true,
  },
  {
    id: 'sahara',
    name: 'Sahara',
    world: 'Desert Circuit',
    modelUrl: '/tracks/sahara/Sahara.glb',
    skyboxUrl: '/tracks/sahara/environment.png',
    accent: '#e7a94f',
    available: true,
  },
  {
    id: 'vegas',
    name: 'Vegas',
    world: 'Neon Circuit',
    modelUrl: '/tracks/vegas/Vegas.glb',
    skyboxUrl: '/tracks/vegas/environment.png',
    accent: '#ff4fb8',
    available: true,
  },
]
