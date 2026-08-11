import { Sparkles } from 'lucide-react'

type Props = {
  progress: number
  skyboxReady: boolean
  error: string
}

export function TrackLoadingOverlay({ progress, skyboxReady, error }: Props) {
  if (error.length > 0) {
    return <div className="pointer-events-none absolute inset-0 grid place-items-center bg-[#0c1015] text-sm text-red-300">{error}</div>
  }

  return (
    <div className={`pointer-events-none absolute inset-0 grid place-items-center transition-colors duration-300 ${skyboxReady === true ? 'bg-transparent' : 'bg-[#0c1015]'}`}>
      <div className="w-64 text-center">
        <div className="mx-auto mb-5 grid size-12 place-items-center rounded-2xl border border-[#f3ad00]/35 bg-[#f3ad00]/10 text-[#ffd455]">
          <Sparkles className="animate-pulse" size={22} />
        </div>
        <p className="text-xs font-bold uppercase tracking-[.2em] text-white/55">Wczytywanie trasy</p>
        <div className="mt-4 h-1 overflow-hidden rounded-full bg-white/8">
          <div className="h-full rounded-full bg-[#f3ad00] transition-[width] duration-300" style={{ width: `${Math.max(progress, 4)}%` }} />
        </div>
        <p className="mt-2 font-mono text-[10px] text-white/25">{progress > 0 ? progress : '…'}%</p>
      </div>
    </div>
  )
}
