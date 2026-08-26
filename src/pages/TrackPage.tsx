import { useState } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { Jukebox } from "@/components/audio/Jukebox";
import { TouchJukebox } from "@/components/audio/TouchJukebox";
import { TrackSidebar } from "@/components/sidebar/TrackSidebar";
import { TrackLoadingOverlay } from "@/components/viewer/TrackLoadingOverlay";
import { TrackViewer } from "@/components/viewer/TrackViewer";
import { DesktopControlsTutorial } from "@/components/viewer/DesktopControlsTutorial";
import { ViewerWatermark } from "@/components/viewer/ViewerWatermark";
import { useTrackLoading } from "@/hooks/useTrackLoading";
import { useAudioPlayer } from "@/hooks/useAudioPlayer";
import { useDesktopControlsTutorial } from "@/hooks/useDesktopControlsTutorial";
import { tracks } from "@/tracks";

type Props = {
  backgroundMode?: boolean;
};

export function TrackPage({ backgroundMode = false }: Props) {
  const { trackId } = useParams();
  const navigate = useNavigate();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(true);
  const track = tracks.find((item) => item.id === trackId);
  const loading = useTrackLoading(track?.id ?? tracks[0].id);
  const player = useAudioPlayer(track?.musicUrl ?? tracks[0].musicUrl);
  const controlsTutorial = useDesktopControlsTutorial(
    backgroundMode === false && loading.ready === true,
  );

  if (track === undefined) return <Navigate to={`/track/${tracks[0].id}`} replace />;

  return (
    <main className="relative flex h-dvh w-full overflow-hidden bg-base text-white">
      {backgroundMode === false && (
        <TrackSidebar
          tracks={tracks}
          selectedTrackId={track.id}
          collapsed={sidebarCollapsed}
          onCollapsedChange={setSidebarCollapsed}
          onSelectTrack={(id) => navigate(`/track/${id}`)}
        />
      )}

      <section className="relative min-w-0 flex-1 overflow-hidden bg-base">
        <audio ref={player.audioRef} src={track.musicUrl} loop preload="none" />
        <TrackViewer
          track={track}
          controlsEnabled={backgroundMode === false}
          onProgress={loading.handleProgress}
          onReady={loading.handleReady}
          onError={loading.handleError}
          onSkyboxReady={loading.handleSkyboxReady}
          onShowControlsTutorial={controlsTutorial.show}
        />
        <ViewerWatermark />
        {backgroundMode === false && <Jukebox musicUrl={track.musicUrl} player={player} />}
        {backgroundMode === false && <TouchJukebox player={player} />}
        {backgroundMode === false && loading.ready === false && (
          <TrackLoadingOverlay
            progress={loading.progress}
            skyboxReady={loading.skyboxReady}
            error={loading.error}
          />
        )}
        {controlsTutorial.visible === true && (
          <DesktopControlsTutorial
            fadingOut={controlsTutorial.fadingOut}
            onDismiss={controlsTutorial.complete}
          />
        )}
      </section>
    </main>
  );
}
