import { Gauge } from 'lucide-react'

type Props = {
  speed: number
  visible: boolean
}

export function SpeedIndicator({ speed, visible }: Props) {
  return (
    <div className={`pointer-events-none absolute bottom-3 left-3 z-20 w-44 bg-[#10162d]/45 px-3 py-2 shadow-[0_8px_24px_rgba(0,0,0,.12)] backdrop-blur-sm transition-opacity duration-300 ${visible ? 'opacity-100' : 'opacity-0'}`} aria-hidden={!visible}>
      <div className="mb-1.5 flex items-center justify-between text-[9px] font-bold uppercase tracking-[.1em] text-white/55">
        <span className="flex items-center gap-1"><Gauge size={10} /> Prędkość lotu</span>
        <span className="font-mono text-[#ffd455]">{speed}</span>
      </div>
      <div className="h-1 overflow-hidden bg-white/10">
        <div className="h-full bg-[#f3ad00] transition-[width] duration-150" style={{ width: `${((speed - 5) / 195) * 100}%` }} />
      </div>
    </div>
  )
}
