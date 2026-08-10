import { useCallback, useEffect, useState } from 'react'
import { Navigate, Route, Routes, useNavigate, useParams } from 'react-router-dom'
import { ChevronRight, CircleHelp, Map, Maximize, MousePointer2, Sparkles } from 'lucide-react'
import { TrackViewer } from './TrackViewer'
import { Jukebox } from './Jukebox'
import { tracks } from './tracks'

function TrackPage() {
  const { trackId } = useParams()
  const navigate = useNavigate()
  const [progress, setProgress] = useState(0)
  const [ready, setReady] = useState(false)
  const [error, setError] = useState('')
  const requestedTrack = tracks.find((item) => item.id === trackId)
  const track = requestedTrack ?? tracks[0]
  const selectedId = track.id

  const handleReady = useCallback(() => setReady(true), [])
  const handleError = useCallback((message: string) => setError(message), [])
  const handleProgress = useCallback((value: number) => setProgress(value), [])

  useEffect(() => {
    setProgress(0)
    setReady(false)
    setError('')
  }, [track.id])

  const selectTrack = (id: string) => {
    navigate(`/track/${id}`)
  }

  if (!requestedTrack) return <Navigate to={`/track/${tracks[0].id}`} replace />

  return (
    <main className="flex h-dvh w-full overflow-hidden bg-[#080b18] text-white">
      <aside className="z-10 flex w-[300px] shrink-0 flex-col border-r border-[#7892e4]/15 bg-[#10162d]/95 shadow-2xl max-md:w-[108px]">
        <header className="flex items-center border-b border-[#7892e4]/15 bg-gradient-to-br from-[#202c59]/55 to-transparent px-5 py-3 max-md:justify-center max-md:px-2">
          <img src="/tooncar-logo.png" alt="ToonCar" className="h-20 w-24 shrink-0 object-contain drop-shadow-[0_0_18px_rgba(243,173,0,.22)] max-md:size-20" />
          <div className="ml-2 max-md:hidden">
            <p className="text-[9px] font-bold uppercase tracking-[.24em] text-[#ffd455]">Oryginalne trasy</p>
            <h1 className="mt-0.5 text-base font-extrabold tracking-tight">Przeglądarka tras</h1>
          </div>
        </header>

        <section className="flex-1 overflow-y-auto py-6">
          <div className="mb-3 px-3 text-center md:text-left">
            <span className="text-[10px] font-bold uppercase tracking-[.2em] text-white/35 max-md:hidden">Wybierz trasę</span>
          </div>
          <div className="space-y-1">
            {tracks.map((item) => {
              const active = item.id === selectedId
              return (
                <button
                  key={item.id}
                  onClick={() => selectTrack(item.id)}
                  disabled={!item.available}
                  className={`group relative flex min-h-20 w-full cursor-pointer items-stretch gap-2 overflow-hidden border-l-4 px-0 py-0 text-left transition-all disabled:cursor-not-allowed disabled:opacity-40 ${active ? 'border-l-[#f3ad00] bg-gradient-to-r from-[#f3ad00]/18 to-[#7892e4]/8' : 'border-l-transparent hover:bg-[#7892e4]/8'}`}
                >
                  <span className="relative w-24 shrink-0 overflow-hidden max-md:w-16">
                    <img src={item.thumbnailUrl} alt="" className="size-full object-cover transition duration-300 group-hover:scale-105" />
                  </span>
                  <span className="min-w-0 flex-1 self-center max-md:hidden">
                    <span className={`block truncate text-sm font-bold ${active ? 'text-white' : 'text-white/65'}`}>{item.name}</span>
                    <span className="mt-0.5 block truncate text-[10px] uppercase tracking-wider text-white/30">{item.world}</span>
                  </span>
                  <ChevronRight size={15} className={`mr-3 self-center max-md:hidden ${active ? 'text-[#ffd455]' : 'text-white/15'}`} />
                </button>
              )
            })}
          </div>
        </section>

        <footer className="border-t border-white/8 p-5 max-md:p-3">
          <Jukebox musicUrl={track.musicUrl} />
          <div className="mt-4 flex items-center gap-2 text-[11px] text-white/30 max-md:mt-0 max-md:justify-center">
            <CircleHelp size={14} />
            <span className="max-md:hidden">Przytrzymaj LPM, aby sterować kamerą</span>
          </div>
        </footer>
      </aside>

      <section className="relative min-w-0 flex-1 overflow-hidden bg-[#10151c]">
        <TrackViewer track={track} onProgress={handleProgress} onReady={handleReady} onError={handleError} />
        <div className="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-between bg-gradient-to-b from-black/55 to-transparent p-7 max-sm:p-4">
          <div>
            <div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[.24em] text-white/45">
              <Map size={13} className="text-[#f3ad00]" /> Trasa aktywna
            </div>
            <h2 className="mt-2 text-3xl font-black tracking-[-.04em] drop-shadow-lg max-sm:text-2xl">{track.name}</h2>
            <p className="mt-1 text-xs font-medium uppercase tracking-[.16em] text-white/40">{track.world}</p>
          </div>
          <button onClick={() => document.documentElement.requestFullscreen?.()} className="pointer-events-auto grid size-10 place-items-center rounded-xl border border-white/10 bg-black/25 text-white/60 backdrop-blur-md transition hover:bg-white/10 hover:text-white" aria-label="Tryb pełnoekranowy">
            <Maximize size={17} />
          </button>
        </div>

        {!ready && !error && (
          <div className="absolute inset-0 grid place-items-center bg-[#0c1015]">
            <div className="w-64 text-center">
              <div className="mx-auto mb-5 grid size-12 place-items-center rounded-2xl border border-[#f3ad00]/35 bg-[#f3ad00]/10 text-[#ffd455]">
                <Sparkles className="animate-pulse" size={22} />
              </div>
              <p className="text-xs font-bold uppercase tracking-[.2em] text-white/55">Wczytywanie trasy</p>
              <div className="mt-4 h-1 overflow-hidden rounded-full bg-white/8">
                <div className="h-full rounded-full bg-[#f3ad00] transition-[width] duration-300" style={{ width: `${Math.max(progress, 4)}%` }} />
              </div>
              <p className="mt-2 font-mono text-[10px] text-white/25">{progress || '…'}%</p>
            </div>
          </div>
        )}
        {error && <div className="absolute inset-0 grid place-items-center bg-[#0c1015] text-sm text-red-300">{error}</div>}

        {ready && (
          <div className="pointer-events-none absolute inset-x-0 bottom-0 flex items-end justify-end bg-gradient-to-t from-black/55 to-transparent p-7 max-sm:p-4">
            <div className="flex items-center gap-2 text-[10px] font-medium text-white/35 max-sm:hidden">
              <MousePointer2 size={13} /> Mysz: rozglądanie · WASD: lot · Q/E: dół/góra · Shift: sprint ×2
            </div>
          </div>
        )}
      </section>
    </main>
  )
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to={`/track/${tracks[0].id}`} replace />} />
      <Route path="/track/:trackId" element={<TrackPage />} />
      <Route path="*" element={<Navigate to={`/track/${tracks[0].id}`} replace />} />
    </Routes>
  )
}
