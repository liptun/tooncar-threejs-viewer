import { useCallback, useEffect, useState } from 'react'

export function useTrackLoading(trackId: string) {
  const [progress, setProgress] = useState(0)
  const [ready, setReady] = useState(false)
  const [skyboxReady, setSkyboxReady] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    setProgress(0)
    setReady(false)
    setSkyboxReady(false)
    setError('')
  }, [trackId])

  return {
    progress,
    ready,
    skyboxReady,
    error,
    handleProgress: useCallback((value: number) => setProgress(value), []),
    handleReady: useCallback(() => setReady(true), []),
    handleSkyboxReady: useCallback(() => setSkyboxReady(true), []),
    handleError: useCallback((message: string) => setError(message), []),
  }
}
