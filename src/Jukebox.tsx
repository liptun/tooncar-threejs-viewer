import { useEffect, useRef, useState } from 'react'
import { Music2, Pause, Play, Volume2 } from 'lucide-react'

type Props = {
  musicUrl: string
}

export function Jukebox({ musicUrl }: Props) {
  const audioRef = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  const [volume, setVolume] = useState(0.55)

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
    <div className="border-b border-[#7892e4]/15 pb-4 max-md:border-0 max-md:pb-3">
      <audio ref={audioRef} src={musicUrl} loop preload="none" />
      <div className="mb-2 flex items-center gap-2 text-[10px] font-bold uppercase tracking-[.16em] text-[#ffd455] max-md:justify-center">
        <Music2 size={13} />
        <span className="max-md:hidden">Muzyka trasy</span>
      </div>
      <div className="flex items-center gap-2 max-md:flex-col">
        <button type="button" onClick={togglePlayback} className="grid size-8 cursor-pointer place-items-center bg-[#f3ad00] text-[#10162d] transition hover:bg-[#ffd455]" aria-label={playing ? 'Wstrzymaj muzykę' : 'Odtwórz muzykę'}>
          {playing ? <Pause size={14} fill="currentColor" /> : <Play size={14} fill="currentColor" />}
        </button>
        <div className="flex min-w-0 flex-1 items-center gap-1.5 max-md:hidden">
          <Volume2 size={12} className="shrink-0 text-white/35" />
          <input type="range" min="0" max="1" step="0.05" value={volume} onChange={(event) => setVolume(Number(event.target.value))} className="h-1 w-full cursor-pointer accent-[#f3ad00]" aria-label="Głośność muzyki" />
        </div>
      </div>
    </div>
  )
}
