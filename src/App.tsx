import { useCallback, useEffect, useState } from 'react'
import { Navigate, Route, Routes, useNavigate, useParams } from 'react-router-dom'
import { ChevronRight, CircleHelp, Gauge, Map, Maximize, MousePointer2, Sparkles } from 'lucide-react'
import { TrackViewer } from './TrackViewer'
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
    <main className="flex h-dvh w-full overflow-hidden bg-[#080a0d] text-white">
      <aside className="z-10 flex w-[300px] shrink-0 flex-col border-r border-white/8 bg-[#0c0f13]/95 shadow-2xl max-md:w-[108px]">
        <header className="border-b border-white/8 px-6 py-7 max-md:px-4">
          <div className="flex items-center gap-3">
            <div className="grid size-10 shrink-0 place-items-center rounded-xl bg-[#ff5c35] text-white shadow-[0_0_24px_rgba(255,92,53,.25)]">
              <Gauge size={22} strokeWidth={2.4} />
            </div>
            <div className="max-md:hidden">
              <p className="text-[10px] font-bold uppercase tracking-[.26em] text-[#ff8669]">ToonCar</p>
              <h1 className="mt-0.5 text-lg font-extrabold tracking-tight">Track Viewer</h1>
            </div>
          </div>
        </header>

        <section className="flex-1 overflow-y-auto px-3 py-6">
          <div className="mb-3 flex items-center justify-between px-3 max-md:justify-center">
            <span className="text-[10px] font-bold uppercase tracking-[.2em] text-white/35 max-md:hidden">Wybierz trasę</span>
            <span className="rounded-full bg-white/6 px-2 py-0.5 text-[10px] font-semibold text-white/40">{tracks.length}</span>
          </div>
          <div className="space-y-2">
            {tracks.map((item, index) => {
              const active = item.id === selectedId
              return (
                <button
                  key={item.id}
                  onClick={() => selectTrack(item.id)}
                  disabled={!item.available}
                  className={`group relative flex w-full items-center gap-3 overflow-hidden rounded-xl border px-3 py-3 text-left transition-all ${active ? 'border-[#ff5c35]/35 bg-[#ff5c35]/10' : 'border-transparent hover:border-white/8 hover:bg-white/4'}`}
                >
                  {active && <span className="absolute inset-y-3 left-0 w-0.5 rounded-r bg-[#ff5c35]" />}
                  <span className={`grid size-11 shrink-0 place-items-center rounded-lg font-mono text-sm font-bold ${active ? 'bg-[#ff5c35] text-white' : 'bg-white/6 text-white/40'}`}>
                    {String(index + 1).padStart(2, '0')}
                  </span>
                  <span className="min-w-0 flex-1 max-md:hidden">
                    <span className={`block truncate text-sm font-bold ${active ? 'text-white' : 'text-white/65'}`}>{item.name}</span>
                    <span className="mt-0.5 block truncate text-[10px] uppercase tracking-wider text-white/30">{item.world}</span>
                  </span>
                  <ChevronRight size={15} className={`max-md:hidden ${active ? 'text-[#ff8669]' : 'text-white/15'}`} />
                </button>
              )
            })}
          </div>
        </section>

        <footer className="border-t border-white/8 p-5 max-md:p-3">
          <div className="flex items-center gap-2 text-[11px] text-white/30 max-md:justify-center">
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
              <Map size={13} className="text-[#ff6a47]" /> Trasa aktywna
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
              <div className="mx-auto mb-5 grid size-12 place-items-center rounded-2xl border border-[#ff5c35]/30 bg-[#ff5c35]/10 text-[#ff6a47]">
                <Sparkles className="animate-pulse" size={22} />
              </div>
              <p className="text-xs font-bold uppercase tracking-[.2em] text-white/55">Wczytywanie trasy</p>
              <div className="mt-4 h-1 overflow-hidden rounded-full bg-white/8">
                <div className="h-full rounded-full bg-[#ff5c35] transition-[width] duration-300" style={{ width: `${Math.max(progress, 4)}%` }} />
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
