import { Pause, Play, Volume2, VolumeX } from 'lucide-react'
import { useAudioPlayer } from '../../hooks/useAudioPlayer'

type Props = {
  musicUrl: string
}

export function Jukebox({ musicUrl }: Props) {
  const player = useAudioPlayer(musicUrl)
  const audioFileName = musicUrl.split('/').pop() ?? musicUrl

  return (
    <div className="pointer-events-auto absolute bottom-4 right-4 z-30 flex items-end gap-2">
      <audio ref={player.audioRef} src={musicUrl} loop preload="none" />
      <button type="button" onClick={player.togglePlayback} className="group flex h-10 w-10 cursor-pointer items-center overflow-hidden whitespace-nowrap rounded-xl border border-white/10 bg-black/25 p-0 text-white/65 shadow-lg backdrop-blur-sm transition-[width,background-color,color] duration-300 hover:w-36 hover:bg-[#10162d]/90 hover:text-white" aria-label={player.playing === true ? `Wstrzymaj ${audioFileName}` : `Odtwórz ${audioFileName}`}>
        <span className="grid h-10 w-[38px] shrink-0 place-items-center">
          {player.playing === true ? <Pause size={14} fill="currentColor" /> : <Play size={14} fill="currentColor" />}
        </span>
        <span className="ml-0 max-w-0 overflow-hidden font-mono text-[10px] font-bold opacity-0 [text-shadow:0_1px_3px_rgba(0,0,0,0.95)] transition-[max-width,margin,opacity] duration-300 group-hover:ml-1 group-hover:max-w-24 group-hover:opacity-100">
          {audioFileName}
        </span>
      </button>
      <div className="group flex h-10 w-10 flex-col justify-end overflow-hidden rounded-xl border border-white/10 bg-black/25 shadow-lg backdrop-blur-sm transition-[height,background-color] duration-300 hover:h-36 hover:bg-[#10162d]/90 focus-within:h-36 focus-within:bg-[#10162d]/90">
        <div className="pointer-events-none flex h-24 shrink-0 items-center justify-center opacity-0 transition-opacity duration-200 group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100">
          <input type="range" min="0" max="1" step="0.05" value={player.volume} onChange={(event) => player.setVolume(Number(event.target.value))} className="h-20 w-4 cursor-pointer accent-[#f3ad00] [direction:rtl] [writing-mode:vertical-lr]" aria-label="Głośność muzyki" />
        </div>
        <button type="button" onClick={player.toggleMuted} className="grid size-10 shrink-0 cursor-pointer place-items-center bg-transparent text-white/65 transition-colors hover:text-white" aria-label={player.volume > 0 ? 'Wycisz muzykę' : 'Włącz dźwięk'}>
          {player.volume > 0 ? <Volume2 size={16} /> : <VolumeX size={16} />}
        </button>
      </div>
    </div>
  )
}
