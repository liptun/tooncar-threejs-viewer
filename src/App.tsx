import { useCallback, useEffect, useState } from 'react'
import { Navigate, Route, Routes, useNavigate, useParams } from 'react-router-dom'
import { ChevronRight, Sparkles } from 'lucide-react'
import { TrackViewer } from './TrackViewer'
import { Jukebox } from './Jukebox'
import { tracks } from './tracks'

function TrackPage() {
  const { trackId } = useParams()
  const navigate = useNavigate()
  const [progress, setProgress] = useState(0)
  const [ready, setReady] = useState(false)
  const [skyboxReady, setSkyboxReady] = useState(false)
  const [error, setError] = useState('')
  const [sidebarCollapsed, setSidebarCollapsed] = useState(true)
  const requestedTrack = tracks.find((item) => item.id === trackId)
  const track = requestedTrack ?? tracks[0]
  const selectedId = track.id

  const handleReady = useCallback(() => setReady(true), [])
  const handleSkyboxReady = useCallback(() => setSkyboxReady(true), [])
  const handleError = useCallback((message: string) => setError(message), [])
  const handleProgress = useCallback((value: number) => setProgress(value), [])

  useEffect(() => {
    setProgress(0)
    setReady(false)
    setSkyboxReady(false)
    setError('')
  }, [track.id])

  const selectTrack = (id: string) => {
    navigate(`/track/${id}`)
  }

  if (!requestedTrack) return <Navigate to={`/track/${tracks[0].id}`} replace />

  return (
    <main className="relative flex h-dvh w-full overflow-hidden bg-[#080b18] text-white">
      <div
        onMouseLeave={() => setSidebarCollapsed(true)}
        className={`fixed inset-y-0 left-0 z-40 w-[340px] transition-transform duration-300 max-md:w-[188px] ${sidebarCollapsed ? 'pointer-events-none -translate-x-full' : 'translate-x-0'}`}
      >
        <aside className="h-full w-[260px] overflow-hidden border-r border-[#7892e4]/20 bg-[#10162d]/70 shadow-2xl backdrop-blur-md max-md:w-[108px]">
        <div className="flex h-full w-[260px] flex-col max-md:w-[108px]">
        <header className="flex flex-col items-center justify-center border-b border-[#7892e4]/15 bg-gradient-to-br from-[#202c59]/55 to-transparent px-2 py-1 text-center max-md:px-1">
          <img src="/tooncar-logo.png" alt="ToonCar" className="h-auto w-48 shrink-0 object-contain drop-shadow-[0_0_18px_rgba(243,173,0,.22)] max-md:w-24" />
          <div className="mb-2 flex w-full items-center gap-2 px-4 max-md:hidden">
            <span className="h-px flex-1 bg-gradient-to-r from-transparent to-[#f3ad00]/45" />
            <h1 className="text-[11px] font-black uppercase tracking-[.18em] text-[#ffd455]">Wybierz trasę</h1>
            <span className="h-px flex-1 bg-gradient-to-l from-transparent to-[#f3ad00]/45" />
          </div>
        </header>

        <section className="flex-1 overflow-y-auto">
          <div className="space-y-1">
            {tracks.map((item) => {
              const active = item.id === selectedId
              return (
                <button
                  key={item.id}
                  onClick={() => selectTrack(item.id)}
                  className={`group relative flex min-h-20 w-full cursor-pointer items-stretch gap-2 overflow-hidden border-l-4 px-0 py-0 text-left transition-all ${active ? 'border-l-[#f3ad00] bg-gradient-to-r from-[#f3ad00]/18 to-[#7892e4]/8' : 'border-l-transparent hover:bg-[#7892e4]/8'}`}
                >
                  <span className="relative w-24 shrink-0 overflow-hidden max-md:w-16">
                    <img src={item.thumbnailUrl} alt="" className="size-full object-cover transition duration-300 group-hover:scale-105" />
                  </span>
                  <span className="min-w-0 flex-1 self-center max-md:hidden">
                    <span className={`block truncate text-sm font-bold ${active ? 'text-white' : 'text-white/65'}`}>{item.name}</span>
                    <span className="mt-0.5 block truncate text-[10px] uppercase tracking-wider text-white/30">{item.originalName}</span>
                  </span>
                </button>
              )
            })}
          </div>
        </section>

        </div>
        </aside>
      </div>

      {sidebarCollapsed && (
        <>
          <div
            className="fixed inset-y-0 left-0 z-[60] w-2"
            onMouseEnter={() => setSidebarCollapsed(false)}
            aria-hidden="true"
          />
          <button
            type="button"
            className="fixed left-0 top-1/2 z-50 flex h-36 w-10 -translate-y-1/2 cursor-pointer flex-col items-center justify-center gap-2 rounded-r-xl border-y border-r border-white/10 bg-[#10162d]/90 text-white/70 shadow-lg backdrop-blur-sm transition-colors hover:text-white"
            onClick={() => setSidebarCollapsed(false)}
            aria-label="Pokaż listę tras"
          >
            <ChevronRight size={14} className="shrink-0" aria-hidden="true" />
            <span className="text-[9px] font-bold uppercase tracking-[.14em] [text-shadow:0_1px_3px_rgba(0,0,0,0.95)] [writing-mode:vertical-rl]">Wybierz trasę</span>
            <ChevronRight size={14} className="shrink-0" aria-hidden="true" />
          </button>
        </>
      )}

      <section className="relative min-w-0 flex-1 overflow-hidden bg-[#10151c]">
        <TrackViewer track={track} onProgress={handleProgress} onReady={handleReady} onError={handleError} onSkyboxReady={handleSkyboxReady} />
        <Jukebox musicUrl={track.musicUrl} />
        {!ready && !error && (
          <div className={`absolute inset-0 grid place-items-center transition-colors duration-300 ${skyboxReady ? 'bg-transparent' : 'bg-[#0c1015]'}`}>
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
