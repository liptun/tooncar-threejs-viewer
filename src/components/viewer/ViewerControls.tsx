import { CircleHelp, Image, Maximize, RotateCcw, Video } from "lucide-react";
import { ExpandableIconButton } from "@/components/common/ExpandableIconButton";
import { RtxIcon } from "@/components/common/RtxIcon";
import type { CameraLinkType } from "@/hooks/useTrackViewer";
import { cn } from "@/lib/cn";

type Props = {
  copiedCameraLink: CameraLinkType | null;
  enhancedGraphics: boolean;
  onToggleEnhancedGraphics: () => void;
  onResetCamera: () => void;
  onCopyInteractiveView: () => void;
  onCopyStaticView: () => void;
  onShowControlsTutorial: () => void;
};

export function ViewerControls({
  copiedCameraLink,
  enhancedGraphics,
  onToggleEnhancedGraphics,
  onResetCamera,
  onCopyInteractiveView,
  onCopyStaticView,
  onShowControlsTutorial,
}: Props) {
  return (
    <div
      className={cn(
        "desktop-ui pointer-events-auto absolute right-7 top-7 z-20",
        "flex items-center justify-end gap-2",
        "max-sm:right-4 max-sm:top-4",
      )}
    >
      <ExpandableIconButton
        icon={<RtxIcon enabled={enhancedGraphics} className="h-7 w-9" />}
        label="Ulepszona grafika"
        expandedClassName="hover:w-48"
        labelClassName="group-hover:max-w-32"
        onClick={onToggleEnhancedGraphics}
        aria-pressed={enhancedGraphics}
        aria-label="Przełącz ulepszone cieniowanie"
        title="Ulepszone cieniowanie GTAO"
      />
      <ExpandableIconButton
        icon={<RotateCcw size={17} />}
        label="Resetuj kamerę"
        expandedClassName="hover:w-40"
        labelClassName="group-hover:max-w-28"
        onClick={onResetCamera}
        aria-label="Resetuj kamerę"
        title="Resetuj kamerę"
      />
      <ExpandableIconButton
        icon={<Video size={17} />}
        label={copiedCameraLink === "interactive" ? "Skopiowano" : "Skopiuj widok"}
        onClick={onCopyInteractiveView}
        aria-label="Skopiuj link do widoku kamery"
        title={copiedCameraLink === "interactive" ? "Skopiowano link" : "Skopiuj link do widoku"}
        className={cn(
          copiedCameraLink === "interactive" && "border-primary/60 bg-primary/25 text-primary",
        )}
      />
      <ExpandableIconButton
        icon={<Image size={17} />}
        label={copiedCameraLink === "static" ? "Skopiowano" : "Skopiuj tło"}
        onClick={onCopyStaticView}
        aria-label="Skopiuj link do animowanego tła"
        title={
          copiedCameraLink === "static" ? "Skopiowano link" : "Skopiuj link do animowanego tła"
        }
        className={cn(
          copiedCameraLink === "static" && "border-primary/60 bg-primary/25 text-primary",
        )}
      />
      <ExpandableIconButton
        icon={<CircleHelp size={18} />}
        label="Sterowanie"
        onClick={onShowControlsTutorial}
        aria-label="Pokaż sterowanie"
        title="Pokaż sterowanie"
      />
      <ExpandableIconButton
        icon={<Maximize size={17} />}
        label="Pełny ekran"
        onClick={() => document.documentElement.requestFullscreen?.()}
        aria-label="Tryb pełnoekranowy"
        title="Pełny ekran"
      />
    </div>
  );
}
