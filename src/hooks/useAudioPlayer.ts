import { useEffect, useRef, useState } from 'react'

const DEFAULT_VOLUME = 0.55

export function useAudioPlayer(musicUrl: string) {
  const audioRef = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  const [volume, setVolume] = useState(DEFAULT_VOLUME)

  useEffect(() => {
    const audio = audioRef.current
    if (!audio) return
    audio.load()
    if (playing) void audio.play().catch(() => setPlaying(false))
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

  const toggleMuted = () => setVolume((current) => current > 0 ? 0 : DEFAULT_VOLUME)

  return { audioRef, playing, volume, setVolume, togglePlayback, toggleMuted }
}
