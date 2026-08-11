import { SpeedIndicator } from "@/components/viewer/SpeedIndicator";
import { MobileControls } from "@/components/viewer/MobileControls";
import { TouchViewerControls } from "@/components/viewer/TouchViewerControls";
import { ViewerControls } from "@/components/viewer/ViewerControls";
import { useTrackViewer } from "@/hooks/useTrackViewer";
import type { Track } from "@/tracks";

type Props = {
  track: Track;
  controlsEnabled: boolean;
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
      {props.controlsEnabled === true && (
        <>
          <ViewerControls
            copiedCameraLink={viewer.copiedCameraLink}
            enhancedGraphics={viewer.enhancedGraphics}
            onToggleEnhancedGraphics={viewer.toggleEnhancedGraphics}
            onResetCamera={viewer.resetCamera}
            onCopyInteractiveView={() => void viewer.createCameraSnapshot("interactive")}
            onCopyStaticView={() => void viewer.createCameraSnapshot("static")}
          />
          <TouchViewerControls
            copiedCameraLink={viewer.copiedCameraLink}
            enhancedGraphics={viewer.enhancedGraphics}
            notice={viewer.cameraNotice}
            onToggleEnhancedGraphics={viewer.toggleEnhancedGraphics}
            onResetCamera={viewer.resetCamera}
            onCopyInteractiveView={() => void viewer.createCameraSnapshot("interactive")}
            onCopyStaticView={() => void viewer.createCameraSnapshot("static")}
          />
          <SpeedIndicator speed={viewer.moveSpeed} visible={viewer.showSpeedIndicator} />
          <MobileControls
            onMove={viewer.setMobileMove}
            onLook={viewer.setMobileLook}
            onVerticalChange={viewer.setMobileVertical}
          />
        </>
      )}
    </>
  );
}
