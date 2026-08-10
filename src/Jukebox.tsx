import { useEffect, useRef, useState } from 'react'
import { Pause, Play, Volume2, VolumeX } from 'lucide-react'

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
    <div className="pointer-events-auto absolute bottom-4 right-4 z-30 flex items-end gap-2">
      <audio ref={audioRef} src={musicUrl} loop preload="none" />
      <button type="button" onClick={togglePlayback} className="grid size-10 cursor-pointer place-items-center rounded-xl border border-white/10 bg-black/25 text-white/65 shadow-lg backdrop-blur-sm transition hover:bg-white/10 hover:text-white" aria-label={playing ? 'Wstrzymaj muzykę' : 'Odtwórz muzykę'}>
        {playing ? <Pause size={14} fill="currentColor" /> : <Play size={14} className="ml-0.5" fill="currentColor" />}
      </button>
      <div className="group relative">
        <div className="pointer-events-none absolute bottom-10 left-0 flex h-24 w-10 items-center justify-center opacity-0 transition-opacity duration-200 group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100">
          <input type="range" min="0" max="1" step="0.05" value={volume} onChange={(event) => setVolume(Number(event.target.value))} className="h-20 w-4 cursor-pointer accent-[#f3ad00] [direction:rtl] [writing-mode:vertical-lr]" aria-label="Głośność muzyki" />
        </div>
        <button type="button" onClick={() => setVolume((current) => current > 0 ? 0 : 0.55)} className="grid size-10 cursor-pointer place-items-center rounded-xl border border-white/10 bg-black/25 text-white/65 shadow-lg backdrop-blur-sm transition hover:bg-white/10 hover:text-white" aria-label={volume > 0 ? 'Wycisz muzykę' : 'Włącz dźwięk'}>
          {volume > 0 ? <Volume2 size={16} /> : <VolumeX size={16} />}
        </button>
      </div>
    </div>
  )
}
