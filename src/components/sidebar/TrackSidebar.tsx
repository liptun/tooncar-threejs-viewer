import { ChevronRight } from 'lucide-react'
import type { Track } from '../../tracks'

type Props = {
  tracks: Track[]
  selectedTrackId: string
  collapsed: boolean
  onCollapsedChange: (collapsed: boolean) => void
  onSelectTrack: (trackId: string) => void
}

export function TrackSidebar({ tracks, selectedTrackId, collapsed, onCollapsedChange, onSelectTrack }: Props) {
  return (
    <>
      <div
        onMouseLeave={() => onCollapsedChange(true)}
        className={`fixed inset-y-0 left-0 z-40 w-[340px] transition-transform duration-300 max-md:w-[188px] ${collapsed === true ? 'pointer-events-none -translate-x-full' : 'translate-x-0'}`}
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
                {tracks.map((track) => {
                  const active = track.id === selectedTrackId
                  return (
                    <button
                      key={track.id}
                      type="button"
                      onClick={() => onSelectTrack(track.id)}
                      className={`group relative flex min-h-20 w-full cursor-pointer items-stretch gap-2 overflow-hidden border-l-4 p-0 text-left transition-all ${active === true ? 'border-l-[#f3ad00] bg-gradient-to-r from-[#f3ad00]/18 to-[#7892e4]/8' : 'border-l-transparent hover:bg-[#7892e4]/8'}`}
                    >
                      <span className="relative w-24 shrink-0 overflow-hidden max-md:w-16">
                        <img src={track.thumbnailUrl} alt="" className="size-full object-cover transition duration-300 group-hover:scale-105" />
                      </span>
                      <span className="min-w-0 flex-1 self-center max-md:hidden">
                        <span className={`block truncate text-sm font-bold ${active === true ? 'text-white' : 'text-white/65'}`}>{track.name}</span>
                        <span className="mt-0.5 block truncate text-[10px] uppercase tracking-wider text-white/30">{track.originalName}</span>
                      </span>
                    </button>
                  )
                })}
              </div>
            </section>
          </div>
        </aside>
      </div>

      {collapsed === true && (
        <>
          <div className="fixed inset-y-0 left-0 z-[60] w-2" onMouseEnter={() => onCollapsedChange(false)} aria-hidden="true" />
          <button
            type="button"
            className="fixed left-0 top-1/2 z-50 flex h-36 w-10 -translate-y-1/2 cursor-pointer flex-col items-center justify-center gap-2 rounded-r-xl border-y border-r border-white/10 bg-[#10162d]/90 text-white/70 shadow-lg backdrop-blur-sm transition-colors hover:text-white"
            onClick={() => onCollapsedChange(false)}
            aria-label="Pokaż listę tras"
          >
            <ChevronRight size={14} aria-hidden="true" />
            <span className="text-[9px] font-bold uppercase tracking-[.14em] [text-shadow:0_1px_3px_rgba(0,0,0,0.95)] [writing-mode:vertical-rl]">Wybierz trasę</span>
            <ChevronRight size={14} aria-hidden="true" />
          </button>
        </>
      )}
    </>
  )
}
