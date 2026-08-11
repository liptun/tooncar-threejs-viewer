import type { Track } from "../../tracks";
import { useTrackViewer } from "../../hooks/useTrackViewer";
import { SpeedIndicator } from "./SpeedIndicator";
import { ViewerControls } from "./ViewerControls";

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
      <SpeedIndicator speed={viewer.moveSpeed} visible={viewer.showSpeedIndicator} />
    </>
  );
}
