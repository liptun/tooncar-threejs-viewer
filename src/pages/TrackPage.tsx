import { useState } from 'react'
import { Navigate, useNavigate, useParams } from 'react-router-dom'
import { Jukebox } from '../components/audio/Jukebox'
import { TrackSidebar } from '../components/sidebar/TrackSidebar'
import { TrackLoadingOverlay } from '../components/viewer/TrackLoadingOverlay'
import { TrackViewer } from '../components/viewer/TrackViewer'
import { useTrackLoading } from '../hooks/useTrackLoading'
import { tracks } from '../tracks'

export function TrackPage() {
  const { trackId } = useParams()
  const navigate = useNavigate()
  const [sidebarCollapsed, setSidebarCollapsed] = useState(true)
  const track = tracks.find((item) => item.id === trackId)
  const loading = useTrackLoading(track?.id ?? tracks[0].id)

  if (!track) return <Navigate to={`/track/${tracks[0].id}`} replace />

  return (
    <main className="relative flex h-dvh w-full overflow-hidden bg-[#080b18] text-white">
      <TrackSidebar
        tracks={tracks}
        selectedTrackId={track.id}
        collapsed={sidebarCollapsed}
        onCollapsedChange={setSidebarCollapsed}
        onSelectTrack={(id) => navigate(`/track/${id}`)}
      />

      <section className="relative min-w-0 flex-1 overflow-hidden bg-[#10151c]">
        <TrackViewer
          track={track}
          onProgress={loading.handleProgress}
          onReady={loading.handleReady}
          onError={loading.handleError}
          onSkyboxReady={loading.handleSkyboxReady}
        />
        <Jukebox musicUrl={track.musicUrl} />
        {!loading.ready && (
          <TrackLoadingOverlay progress={loading.progress} skyboxReady={loading.skyboxReady} error={loading.error} />
        )}
      </section>
    </main>
  )
}
