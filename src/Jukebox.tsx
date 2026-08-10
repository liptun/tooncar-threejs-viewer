import { useEffect, useRef, useState } from 'react'
import { Pause, Play, Volume2, VolumeX } from 'lucide-react'

type Props = {
  musicUrl: string
}

export function Jukebox({ musicUrl }: Props) {
  const audioRef = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  const [volume, setVolume] = useState(0.55)
  const audioFileName = musicUrl.split('/').pop() ?? musicUrl

  useEffect(() => {
    const audio = audioRef.current
    if (!audio) return
    audio.load()
    if (playing) {
      void audio.play().catch(() => setPlaying(false))
    }
  }, [musicUrl])

  useEffect(() => {
    if (audioRef.current) audioRef.current.volume = volume
  }, [volume])

  const togglePlayback = () => {
    const audio = audioRef.current
    if (!audio) return
    if (playing) {
      audio.pause()
      setPlaying(false)
      return
    }
    void audio.play().then(() => setPlaying(true)).catch(() => setPlaying(false))
  }

  return (
    <div className="pointer-events-auto absolute bottom-4 right-4 z-30 flex items-end gap-2">
      <audio ref={audioRef} src={musicUrl} loop preload="none" />
      <button type="button" onClick={togglePlayback} className="group flex h-10 w-10 cursor-pointer items-center overflow-hidden whitespace-nowrap rounded-xl border border-white/10 bg-black/25 p-0 text-white/65 shadow-lg backdrop-blur-sm transition-[width,background-color,color] duration-300 hover:w-36 hover:bg-[#10162d]/90 hover:text-white" aria-label={playing ? `Wstrzymaj ${audioFileName}` : `Odtwórz ${audioFileName}`}>
        <span className="grid h-10 w-[38px] shrink-0 place-items-center">
          {playing ? <Pause size={14} fill="currentColor" /> : <Play size={14} fill="currentColor" />}
        </span>
        <span className="ml-0 max-w-0 overflow-hidden font-mono text-[10px] font-bold opacity-0 [text-shadow:0_1px_3px_rgba(0,0,0,0.95)] transition-[max-width,margin,opacity] duration-300 group-hover:ml-1 group-hover:max-w-24 group-hover:opacity-100">
          {audioFileName}
        </span>
      </button>
      <div className="group flex h-10 w-10 flex-col justify-end overflow-hidden rounded-xl border border-white/10 bg-black/25 shadow-lg backdrop-blur-sm transition-[height,background-color] duration-300 hover:h-36 hover:bg-[#10162d]/90 focus-within:h-36 focus-within:bg-[#10162d]/90">
        <div className="pointer-events-none flex h-24 shrink-0 items-center justify-center opacity-0 transition-opacity duration-200 group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100">
          <input type="range" min="0" max="1" step="0.05" value={volume} onChange={(event) => setVolume(Number(event.target.value))} className="h-20 w-4 cursor-pointer accent-[#f3ad00] [direction:rtl] [writing-mode:vertical-lr]" aria-label="Głośność muzyki" />
        </div>
        <button type="button" onClick={() => setVolume((current) => current > 0 ? 0 : 0.55)} className="grid size-10 shrink-0 cursor-pointer place-items-center bg-transparent text-white/65 transition-colors hover:text-white" aria-label={volume > 0 ? 'Wycisz muzykę' : 'Włącz dźwięk'}>
          {volume > 0 ? <Volume2 size={16} /> : <VolumeX size={16} />}
        </button>
      </div>
    </div>
  )
}
