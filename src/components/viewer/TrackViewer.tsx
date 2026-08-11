import { SpeedIndicator } from "@/components/viewer/SpeedIndicator";
import { MobileControls } from "@/components/viewer/MobileControls";
import { TouchViewerControls } from "@/components/viewer/TouchViewerControls";
import { ViewerControls } from "@/components/viewer/ViewerControls";
import { useTrackViewer } from "@/hooks/useTrackViewer";
import type { Track } from "@/tracks";

type Props = {
  track: Track;
  onProgress: (progress: number) => void;
  onReady: (animations: number) => void;
  onError: (message: string) => void;
  onSkyboxReady: () => void;
};

export function TrackViewer(props: Props) {
  const viewer = useTrackViewer(props);

  return (
    <>
      <div
        ref={viewer.mountRef}
        className="absolute inset-0"
        aria-label={`Widok 3D trasy ${props.track.name}`}
      />
      <ViewerControls
        snapshotCopied={viewer.snapshotCopied}
        onResetCamera={viewer.resetCamera}
        onCreateSnapshot={() => void viewer.createCameraSnapshot()}
      />
      <TouchViewerControls
        notice={viewer.cameraNotice}
        onResetCamera={viewer.resetCamera}
        onCreateSnapshot={() => void viewer.createCameraSnapshot()}
      />
      <SpeedIndicator speed={viewer.moveSpeed} visible={viewer.showSpeedIndicator} />
      <MobileControls
        onMove={viewer.setMobileMove}
        onLook={viewer.setMobileLook}
        onVerticalChange={viewer.setMobileVertical}
      />
    </>
  );
}
